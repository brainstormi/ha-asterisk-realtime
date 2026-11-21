"""AudioSocket server: accepts calls from Asterisk and connects them to OpenAI."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
import uuid
from typing import Any

import aiohttp
from websockets.asyncio.client import connect

from . import __version__
from .audiosocket import KIND_HANGUP, KIND_UUID, pack_frame, read_frame
from .config import DEPRECATED_MODELS, PROFILE_INBOUND, Config, ConfigError, load
from .ha import HomeAssistant
from .outbound import CommandError, OutboundCall, Registry, parse_command, read_commands
from .session import CallSession, keepalive

log = logging.getLogger("bridge")

# Event fired on the Home Assistant bus for call results.
EVENT_TYPE = "asterisk_realtime_bridge_call"


class Bridge:
    def __init__(self, cfg: Config, http: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.home = HomeAssistant(
            http,
            cfg.ha_api_urls,
            cfg.ha_token,
            cfg.conversation_agent,
            cfg.language,
            cfg.fallback_agent,
        )
        # Read-only agent for questions in outbound calls (no fallback: a
        # fallback agent might be able to act).
        self.query_home = (
            HomeAssistant(http, cfg.ha_api_urls, cfg.ha_token, cfg.query_agent, cfg.language)
            if cfg.query_agent
            else None
        )
        self.registry = Registry(on_expired=self._report_missed)
        self._active = 0

    # ------------------------------------------------------- HA commands
    async def on_command(self, data: Any) -> None:
        """A command from Home Assistant (add-on stdin)."""
        try:
            call = parse_command(data)
        except CommandError as err:
            log.warning("Rejected command from Home Assistant: %s", err)
            call_id = data.get("call_id") if isinstance(data, dict) else None
            if call_id:
                await self._fire(
                    {"call_id": str(call_id), "direction": "outbound", "stage": "ended",
                     "result": "failed", "error": str(err)}
                )
            return
        self.registry.add(call)

    async def _report_missed(self, call: OutboundCall) -> None:
        await self._fire(self._event(call, "ended", "no_answer"))

    def _event(self, call: OutboundCall, stage: str, result: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "call_id": call.call_id,
            "direction": "outbound",
            "stage": stage,
            "profile": call.profile,
            "person": call.person,
        }
        if result:
            data["result"] = result
        return data

    async def _fire(self, data: dict[str, Any]) -> None:
        try:
            await self.home.fire_event(EVENT_TYPE, data)
            log.debug("Event %s: %s", EVENT_TYPE, data)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not send the call event to Home Assistant: %r", err)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), 5)
            if kind != KIND_UUID or len(payload) != 16:
                log.warning("Connection from %s without a valid UUID; closing it", peer)
                return
            full_id = str(uuid.UUID(bytes=payload))
            call_id = full_id[:8]
            outbound = self.registry.take(full_id)

            if self._active >= self.cfg.max_concurrent_calls:
                log.warning("[%s] rejected: %d call(s) already active", call_id, self._active)
                writer.write(pack_frame(KIND_HANGUP))
                await writer.drain()
                if outbound:
                    await self._fire({**self._event(outbound, "ended", "failed"),
                                      "error": "too many calls"})
                return

            self._active += 1
            if outbound:
                log.info("[%s] outbound call answered (profile %s)", call_id, outbound.profile)
                await self._fire(self._event(outbound, "answered"))
            else:
                log.info("[%s] incoming call (%s)", call_id, peer)
            started = time.monotonic()
            session: CallSession | None = None
            # Asterisk drops the call after 2 s without activity, so stream
            # silence right away, while we connect to OpenAI.
            silence = asyncio.create_task(keepalive(writer))
            try:
                url = f"{self.cfg.realtime_url}?model={self.cfg.model}"
                async with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
                    open_timeout=10,
                    max_size=None,
                ) as ws:
                    silence.cancel()
                    await asyncio.gather(silence, return_exceptions=True)
                    session = CallSession(
                        self.cfg, call_id, reader, writer, ws, self.home,
                        outbound=outbound, query_home=self.query_home,
                    )
                    await session.run()
            except Exception as err:  # noqa: BLE001
                log.error("[%s] call failed: %r", call_id, err)
            finally:
                silence.cancel()
                await asyncio.gather(silence, return_exceptions=True)
                self._active -= 1
                log.info("[%s] call ended", call_id)
                await self._fire(self._ended(full_id, outbound, session, time.monotonic() - started))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            log.warning("Connection from %s closed before the call started", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


    def _ended(
        self,
        full_id: str,
        outbound: OutboundCall | None,
        session: CallSession | None,
        duration: float,
    ) -> dict[str, Any]:
        if session is None or not session.connected:
            result = "failed"  # the assistant never got to speak (e.g. OpenAI down)
        elif session.confirmed:
            result = "confirmed"
        else:
            result = "answered"
        if outbound:
            data = self._event(outbound, "ended", result)
        else:
            data = {"call_id": full_id, "direction": "inbound", "stage": "ended",
                    "profile": PROFILE_INBOUND, "result": result}
        if session and session.confirmed_by:
            data["confirmed_by"] = session.confirmed_by
        data["duration"] = round(duration)
        data["pin_verified"] = bool(session and session.pin_person is not None)
        if session and session.pin_person:
            data["pin_person"] = session.pin_person
        return data


async def _stdin_reader() -> asyncio.StreamReader | None:
    """The add-on's stdin, where Home Assistant sends commands (hassio.addon_stdin)."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (OSError, ValueError) as err:
        log.debug("stdin is not available for commands: %r", err)
        return None
    return reader


async def serve(cfg: Config) -> None:
    async with aiohttp.ClientSession() as http:
        bridge = Bridge(cfg, http)
        server = await asyncio.start_server(bridge.handle, cfg.bind_address, cfg.port)
        if not cfg.pin_entries:
            log.warning(
                "PIN DISABLED: any call that reaches the bridge can control the home. "
                "Your Asterisk dialplan is the only barrier."
            )
        if cfg.bind_address not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                "AudioSocket is listening on %s. It has no authentication: restrict "
                "access to the Asterisk host with a firewall.",
                cfg.bind_address,
            )
        if cfg.model in DEPRECATED_MODELS:
            log.warning("Model %s is deprecated and retires on 2027-01-20.", cfg.model)
        log.info(
            "Asterisk Realtime Bridge %s listening on %s:%d (model %s, PIN %s)",
            __version__,
            cfg.bind_address,
            cfg.port,
            cfg.model,
            "enabled" if cfg.pin_entries else "DISABLED",
        )
        if cfg.pins:
            log.info("Personal PINs configured for: %s", ", ".join(n for n, _ in cfg.pins))
        commands = None
        stdin = await _stdin_reader()
        if stdin is not None:
            commands = asyncio.create_task(read_commands(stdin, bridge.on_command))
            log.info("Outbound calls: listening for commands from Home Assistant%s",
                     "" if cfg.query_agent else " (no query_agent: questions disabled in outbound calls)")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        async with server:
            await stop.wait()
        log.info("Stopping...")
        if commands:
            commands.cancel()
            await asyncio.gather(commands, return_exceptions=True)
        await bridge.registry.close()


def main() -> None:
    try:
        cfg = load()
    except ConfigError as err:
        print(f"Configuration ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
