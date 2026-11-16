"""AudioSocket server: accepts calls from Asterisk and connects them to OpenAI."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import uuid

import aiohttp
from websockets.asyncio.client import connect

from . import __version__
from .audiosocket import KIND_HANGUP, KIND_UUID, pack_frame, read_frame
from .config import DEPRECATED_MODELS, Config, ConfigError, load
from .ha import HomeAssistant
from .session import CallSession

log = logging.getLogger("bridge")


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
        self._active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), 5)
            if kind != KIND_UUID or len(payload) != 16:
                log.warning("Connection from %s without a valid UUID; closing it", peer)
                return
            call_id = str(uuid.UUID(bytes=payload))[:8]

            if self._active >= self.cfg.max_concurrent_calls:
                log.warning("[%s] rejected: %d call(s) already active", call_id, self._active)
                writer.write(pack_frame(KIND_HANGUP))
                await writer.drain()
                return

            self._active += 1
            log.info("[%s] incoming call (%s)", call_id, peer)
            try:
                url = f"{self.cfg.realtime_url}?model={self.cfg.model}"
                async with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
                    open_timeout=10,
                    max_size=None,
                ) as ws:
                    await CallSession(self.cfg, call_id, reader, writer, ws, self.home).run()
            except Exception as err:  # noqa: BLE001
                log.error("[%s] call failed: %r", call_id, err)
            finally:
                self._active -= 1
                log.info("[%s] call ended", call_id)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            log.warning("Connection from %s closed before the call started", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


async def serve(cfg: Config) -> None:
    async with aiohttp.ClientSession() as http:
        bridge = Bridge(cfg, http)
        server = await asyncio.start_server(bridge.handle, cfg.bind_address, cfg.port)
        if not cfg.pin:
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
            "enabled" if cfg.pin else "DISABLED",
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        async with server:
            await stop.wait()
        log.info("Stopping...")


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
