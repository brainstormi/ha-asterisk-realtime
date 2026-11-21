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
from .fallback import Fallback
from .ha import HomeAssistant
from .health import OpenAIHealth, describe_error, realtime_probe
from .outbound import CommandError, OutboundCall, Registry, parse_command, read_commands
from .session import CallSession, keepalive
from .status import HomeStatus

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
        # Read-only view of the home for outbound calls without a PIN.
        self.status = HomeStatus(self.home, cfg.query_include, cfg.query_exclude)
        self.registry = Registry(on_expired=self._report_missed)
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        # Fallback when OpenAI is unavailable (Home Assistant TTS, e.g. Piper).
        self.fallback = Fallback(self.home, cfg.fallback_tts, cfg.language, cfg.fallback_tts_voice)
        self.health = OpenAIHealth(self.home, self._probe_openai, cfg.openai_check_minutes)

    @property
    def realtime_url(self) -> str:
        return f"{self.cfg.realtime_url}?model={self.cfg.model}"

    async def _probe_openai(self) -> tuple[bool, str]:
        return await realtime_probe(connect, self.realtime_url, self.cfg.openai_api_key)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.registry.close()

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
        if self.fallback.enabled:
            # Ready before the phone is answered, in case OpenAI fails then.
            self.spawn(self.fallback.prepare_call(call))

    async def _report_missed(self, call: OutboundCall) -> None:
        self.fallback.forget(call)
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
            fallback: dict[str, Any] | None = None
            # Asterisk drops the call after 2 s without activity, so stream
            # silence right away, while we connect to OpenAI.
            silence = asyncio.create_task(keepalive(writer))
            try:
                try:
                    async with connect(
                        self.realtime_url,
                        additional_headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
                        open_timeout=10,
                        max_size=None,
                    ) as ws:
                        silence.cancel()
                        await asyncio.gather(silence, return_exceptions=True)
                        session = CallSession(
                            self.cfg, call_id, reader, writer, ws, self.home,
                            outbound=outbound, status=self.status,
                        )
                        await session.run(terminate=False)
                except Exception as err:  # noqa: BLE001
                    log.error("[%s] call failed: %r", call_id, err)
                    self.spawn(self.health.report(False, describe_error(err)))
                else:
                    if session.connected:
                        self.spawn(self.health.report(True))
                    elif session.openai_failed:
                        self.spawn(self.health.report(False, session.openai_error or "no session"))
                silence.cancel()
                await asyncio.gather(silence, return_exceptions=True)
                if self._needs_fallback(session, outbound):
                    fallback = await self._run_fallback(call_id, reader, writer, outbound)
            finally:
                await self._hang_up(writer)
                self._active -= 1
                log.info("[%s] call ended", call_id)
                if outbound:
                    self.fallback.forget(outbound)
                await self._fire(
                    self._ended(full_id, outbound, session, time.monotonic() - started, fallback)
                )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            log.warning("Connection from %s closed before the call started", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


    def _needs_fallback(self, session: CallSession | None, outbound: OutboundCall | None) -> bool:
        if not self.fallback.enabled:
            return False
        if session is None:
            return True  # OpenAI could not even be reached
        if session.end_reason in ("asterisk", "pacer"):
            return False  # the phone side is gone
        if not session.connected:
            return True  # OpenAI never confirmed the session (or failed while starting)
        if not session.openai_failed:
            return False  # ended normally (goodbye, time limit)
        if outbound is not None:
            # Mid-call drop: repeat the notice unless it was already confirmed.
            return not session.confirmed
        return True  # incoming call: say that the assistant is not available

    async def _run_fallback(
        self, call_id: str, reader: asyncio.StreamReader, writer: Any, outbound: OutboundCall | None
    ) -> dict[str, Any]:
        log.info("[%s] OpenAI unavailable: using the fallback (%s)", call_id, self.fallback.engine)
        try:
            if outbound is not None:
                confirmed = await self.fallback.outbound(reader, writer, outbound)
                log.info("[%s] fallback notice %s", call_id, "confirmed" if confirmed else "not confirmed")
                return {"confirmed": confirmed}
            await self.fallback.inbound(reader, writer)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] fallback failed: %r", call_id, err)
        return {"confirmed": False}

    @staticmethod
    async def _hang_up(writer: Any) -> None:
        try:
            writer.write(pack_frame(KIND_HANGUP))
            await writer.drain()
        except Exception:  # noqa: BLE001 - the connection may already be closed
            pass

    def _ended(
        self,
        full_id: str,
        outbound: OutboundCall | None,
        session: CallSession | None,
        duration: float,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if fallback is not None and outbound is not None:
            result = "confirmed" if fallback.get("confirmed") else "answered"
        elif session is None or not session.connected:
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
        if fallback is not None and fallback.get("confirmed"):
            data["confirmed_by"] = "dtmf"
        elif session and session.confirmed_by:
            data["confirmed_by"] = session.confirmed_by
        if fallback is not None:
            data["fallback"] = True
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
        if bridge.fallback.enabled:
            bridge.spawn(bridge.fallback.prepare_fixed())
        else:
            log.info("No fallback_tts: if OpenAI is unavailable, calls cannot be answered")
        bridge.spawn(bridge.health.run())
        commands = None
        stdin = await _stdin_reader()
        if stdin is not None:
            commands = asyncio.create_task(read_commands(stdin, bridge.on_command))
            log.info("Outbound calls: listening for commands from Home Assistant "
                     "(read-only scope without PIN: %s%s)", bridge.status.scope,
                     f"; excluding {cfg.query_exclude}" if cfg.query_exclude else "")
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
        await bridge.close()


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
