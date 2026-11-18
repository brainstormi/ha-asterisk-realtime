"""One phone call: Asterisk audio <-> OpenAI Realtime API, with Home Assistant tools.

Design
------
* Uplink: 20 ms slin frames from Asterisk -> µ-law -> ``input_audio_buffer.append``.
* Downlink: ``response.output_audio.delta`` (µ-law, delivered faster than real
  time) -> slin -> local queue -> a "pacer" sends it to Asterisk at 20 ms per
  frame. Because the queue lives on our side, interrupting is instantaneous:
  we just empty the queue.
* Barge-in: on ``input_audio_buffer.speech_started`` we empty the queue and send
  ``conversation.item.truncate`` with the milliseconds the caller actually
  heard, so the model knows what was really said.
* Security: until the PIN is verified, the ``home`` tool is rejected in code
  (it does not depend on the model following its instructions).
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hmac
import json
import logging
from typing import Any, Protocol

from .audiosocket import (
    FRAME_BYTES,
    FRAME_MS,
    KIND_AUDIO,
    KIND_DTMF,
    KIND_ERROR,
    KIND_HANGUP,
    pack_frame,
    read_frame,
)
from .config import Config
from .g711 import slin_to_ulaw, ulaw_to_slin

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 10.0
FLUSH_GRACE_S = 0.6

# 20 ms of digital silence. Asterisk's AudioSocket application hangs up if it
# sees no activity (from the channel or from the server) for 2000 ms, and some
# carriers only start sending media once they receive some. So we must send
# audio continuously, from the very first moment, even when the assistant is
# not talking.
SILENCE_FRAME = bytes(FRAME_BYTES)


async def keepalive(writer: Any, frame_seconds: float = FRAME_MS / 1000) -> None:
    """Send silence to Asterisk until cancelled (used while connecting to OpenAI)."""
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    while True:
        writer.write(pack_frame(KIND_AUDIO, SILENCE_FRAME))
        await writer.drain()
        next_tick += frame_seconds
        delay = next_tick - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_tick = loop.time()


class _WebSocket(Protocol):
    async def send(self, message: str) -> None: ...
    def __aiter__(self): ...


class _Home(Protocol):
    async def ask(self, text: str) -> dict[str, Any]: ...


def build_tools(cfg: Config) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "home",
            "description": (
                "Control or query the home through Home Assistant. Pass ONE "
                "request in natural language, for example 'turn on the living "
                "room light' or 'is the garage door open?'. Returns Home "
                "Assistant's response."
            ),
            "parameters": {
                "type": "object",
                "properties": {"request": {"type": "string"}},
                "required": ["request"],
            },
        },
        {
            "type": "function",
            "name": "hang_up",
            "description": (
                "End the call. Use it after saying goodbye, when the caller "
                "needs nothing else."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    if cfg.pin:
        tools.insert(
            0,
            {
                "type": "function",
                "name": "verify_pin",
                "description": (
                    "Check the security PIN spoken by the caller. Pass only "
                    "the digits, without spaces."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"pin": {"type": "string"}},
                    "required": ["pin"],
                },
            },
        )
    return tools


def build_session_update(cfg: Config) -> dict[str, Any]:
    if cfg.vad == "semantic_vad":
        turn_detection: dict[str, Any] = {
            "type": "semantic_vad",
            "eagerness": "auto",
            "create_response": True,
            "interrupt_response": True,
        }
    else:
        turn_detection = {
            "type": "server_vad",
            "threshold": cfg.vad_threshold,
            "prefix_padding_ms": 300,
            "silence_duration_ms": cfg.vad_silence_ms,
            "create_response": True,
            "interrupt_response": True,
        }
    audio_in: dict[str, Any] = {
        "format": {"type": "audio/pcmu"},
        "turn_detection": turn_detection,
    }
    if cfg.noise_reduction != "off":
        audio_in["noise_reduction"] = {"type": cfg.noise_reduction}
    if cfg.log_transcripts:
        audio_in["transcription"] = {
            "model": "gpt-4o-mini-transcribe",
            "language": cfg.language,
        }
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": cfg.instructions,
            "audio": {
                "input": audio_in,
                "output": {"format": {"type": "audio/pcmu"}, "voice": cfg.voice},
            },
            "tools": build_tools(cfg),
            "tool_choice": "auto",
        },
    }


class CallSession:
    def __init__(
        self,
        cfg: Config,
        call_id: str,
        reader: asyncio.StreamReader,
        writer: Any,
        ws: _WebSocket,
        home: _Home,
        frame_seconds: float = FRAME_MS / 1000,
    ) -> None:
        self.cfg = cfg
        self.call_id = call_id
        self.reader = reader
        self.writer = writer
        self.ws = ws
        self.home = home
        self.frame_seconds = frame_seconds

        # Audio queue towards Asterisk: (item_id, 20 ms frame).
        self._out: collections.deque[tuple[str, bytes]] = collections.deque()
        self._carry = bytearray()
        self._carry_item: str | None = None
        self._played_ms: dict[str, int] = collections.defaultdict(int)

        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self._response_active = False
        self._hangup_pending = False
        self._seen_calls: set[str] = set()
        self._tool_tasks: set[asyncio.Task] = set()

        self._authed = not cfg.pin
        self._pin_attempts = 0
        self._dtmf = ""

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        await self._send(build_session_update(self.cfg))
        workers = {
            "asterisk": asyncio.create_task(self._from_asterisk()),
            "openai": asyncio.create_task(self._from_openai()),
            "pacer": asyncio.create_task(self._pacer()),
            "ready": asyncio.create_task(self._watch_ready()),
            "finished": asyncio.create_task(self._finished.wait()),
        }
        try:
            done, _ = await asyncio.wait(
                workers.values(),
                timeout=self.cfg.max_call_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                log.info(
                    "[%s] maximum call duration reached (%ss)",
                    self.call_id,
                    self.cfg.max_call_seconds,
                )
            for name, task in workers.items():
                if task in done and not task.cancelled() and task.exception():
                    log.warning("[%s] %s worker failed: %r", self.call_id, name, task.exception())
        finally:
            for task in (*workers.values(), *self._tool_tasks):
                task.cancel()
            await asyncio.gather(*workers.values(), *self._tool_tasks, return_exceptions=True)
            await self._terminate()

    async def _terminate(self) -> None:
        try:
            self.writer.write(pack_frame(KIND_HANGUP))
            await self.writer.drain()
        except Exception:  # noqa: BLE001 - the connection may already be closed
            pass

    async def _watch_ready(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError("OpenAI did not confirm the session in time") from None
        await asyncio.Event().wait()  # ready: sleep until cancelled

    async def _send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    # ------------------------------------------------- Asterisk -> OpenAI
    async def _from_asterisk(self) -> None:
        while True:
            try:
                kind, payload = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                log.info("[%s] Asterisk closed the connection", self.call_id)
                return
            if kind == KIND_AUDIO:
                if self._ready.is_set():
                    audio = base64.b64encode(slin_to_ulaw(payload)).decode("ascii")
                    await self._send({"type": "input_audio_buffer.append", "audio": audio})
            elif kind == KIND_DTMF:
                await self._on_dtmf(payload.decode("ascii", errors="ignore")[:1])
            elif kind == KIND_HANGUP:
                log.info("[%s] caller hung up", self.call_id)
                return
            elif kind == KIND_ERROR:
                log.warning("[%s] AudioSocket error: %r", self.call_id, payload)
                return

    # ------------------------------------------------- OpenAI -> Asterisk
    async def _from_openai(self) -> None:
        async for raw in self.ws:
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            await self._on_event(event)
        log.info("[%s] OpenAI closed the connection", self.call_id)

    async def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.updated":
            if not self._ready.is_set():
                self._ready.set()
                await self._greet()
        elif kind == "input_audio_buffer.speech_started":
            await self._interrupt()
        elif kind == "response.created":
            self._response_active = True
        elif kind == "response.output_audio.delta":
            self._deliver_audio(event.get("item_id", ""), base64.b64decode(event.get("delta", "")))
        elif kind == "response.output_audio.done":
            self._flush_carry()
        elif kind == "response.done":
            await self._on_response_done(event.get("response") or {})
        elif kind == "error":
            err = event.get("error") or {}
            log.warning("[%s] OpenAI error: %s %s", self.call_id, err.get("code"), err.get("message"))
        elif self.cfg.log_transcripts:
            if kind == "conversation.item.input_audio_transcription.completed":
                log.info("[%s] caller: %s", self.call_id, event.get("transcript", "").strip())
            elif kind == "response.output_audio_transcript.done":
                log.info("[%s] assistant: %s", self.call_id, event.get("transcript", "").strip())

    async def _greet(self) -> None:
        if self.cfg.pin:
            hint = "Greet the caller in one short sentence and ask for the security PIN."
        else:
            hint = "Greet the caller in one short sentence and ask how you can help."
        await self._send({"type": "response.create", "response": {"instructions": hint}})

    async def _on_response_done(self, response: dict[str, Any]) -> None:
        self._response_active = False
        calls = [
            item
            for item in response.get("output", [])
            if item.get("type") == "function_call" and item.get("call_id") not in self._seen_calls
        ]
        if calls:
            self._seen_calls.update(c["call_id"] for c in calls)
            self._spawn(self._run_tools(calls))
        elif self._hangup_pending:
            self._spawn(self._finish_after_drain())

    # ------------------------------------------------------------- audio
    def _deliver_audio(self, item_id: str, ulaw: bytes) -> None:
        if item_id != self._carry_item:
            self._flush_carry()
            self._carry_item = item_id
        self._carry += ulaw_to_slin(ulaw)
        while len(self._carry) >= FRAME_BYTES:
            self._out.append((item_id, bytes(self._carry[:FRAME_BYTES])))
            del self._carry[:FRAME_BYTES]

    def _flush_carry(self) -> None:
        if self._carry and self._carry_item is not None:
            padded = bytes(self._carry) + bytes(FRAME_BYTES - len(self._carry))
            self._out.append((self._carry_item, padded))
        self._carry.clear()

    async def _pacer(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._out:
                item_id, frame = self._out.popleft()
                self.writer.write(pack_frame(KIND_AUDIO, frame))
                await self.writer.drain()
                self._played_ms[item_id] += FRAME_MS
                next_tick += self.frame_seconds
                if next_tick < loop.time() - 0.1:  # fell behind: do not burst
                    next_tick = loop.time()
            else:
                # Nothing to say: keep the media flowing with silence.
                self.writer.write(pack_frame(KIND_AUDIO, SILENCE_FRAME))
                await self.writer.drain()
                next_tick += self.frame_seconds
                if next_tick < loop.time() - 0.1:
                    next_tick = loop.time()

    async def _interrupt(self) -> None:
        """The caller starts talking: go silent now and tell the model what was heard."""
        pending: list[str] = []
        for item_id, _ in self._out:
            if item_id not in pending:
                pending.append(item_id)
        if self._carry_item and self._carry and self._carry_item not in pending:
            pending.append(self._carry_item)
        self._out.clear()
        self._carry.clear()
        for item_id in pending:
            await self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": 0,
                    "audio_end_ms": self._played_ms.get(item_id, 0),
                }
            )
        if pending:
            log.debug("[%s] barge-in: truncated %d item(s)", self.call_id, len(pending))

    async def _finish_after_drain(self) -> None:
        while self._out or self._carry:
            await asyncio.sleep(0.05)
        await asyncio.sleep(FLUSH_GRACE_S)  # let the last audio reach the phone
        self._finished.set()

    # ------------------------------------------------------------- tools
    async def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        only_hangup = all(c.get("name") == "hang_up" for c in calls)
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await self._dispatch(call.get("name", ""), args)
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    },
                }
            )
        if self._hangup_pending and only_hangup:
            await self._finish_after_drain()
        else:
            await self._send({"type": "response.create"})

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "verify_pin":
            return self._check_pin(str(args.get("pin", "")))
        if name == "hang_up":
            self._hangup_pending = True
            return {"ok": True}
        if name == "home":
            if not self._authed:
                return {"ok": False, "error": "PIN not verified. Ask for the PIN before continuing."}
            request = str(args.get("request", "")).strip()
            if not request:
                return {"ok": False, "error": "The request is missing."}
            log.info("[%s] home: %s", self.call_id, request)
            try:
                return {"ok": True, **await self.home.ask(request)}
            except Exception as err:  # noqa: BLE001 - report it to the model
                log.warning("[%s] failed to talk to Home Assistant: %r", self.call_id, err)
                return {"ok": False, "error": "Could not reach Home Assistant."}
        return {"ok": False, "error": f"Unknown tool: {name}"}

    # --------------------------------------------------------------- PIN
    def _check_pin(self, candidate: str) -> dict[str, Any]:
        digits = "".join(ch for ch in candidate if ch.isdigit())
        if self._authed:
            return {"ok": True, "message": "The PIN was already verified."}
        if digits and hmac.compare_digest(digits, self.cfg.pin):
            self._authed = True
            log.info("[%s] PIN verified", self.call_id)
            return {"ok": True, "message": "PIN correct."}
        return self._pin_failed()

    def _pin_failed(self) -> dict[str, Any]:
        self._pin_attempts += 1
        log.warning(
            "[%s] wrong PIN (%d/%d)", self.call_id, self._pin_attempts, self.cfg.max_pin_attempts
        )
        if self._pin_attempts >= self.cfg.max_pin_attempts:
            self._hangup_pending = True
            return {
                "ok": False,
                "error": "Wrong PIN. No attempts left: say a brief goodbye without giving any hints.",
            }
        return {"ok": False, "error": "Wrong PIN. Ask for it again."}

    async def _on_dtmf(self, digit: str) -> None:
        """The PIN can also be typed on the phone keypad (more reliable than speaking it)."""
        if self._authed or not self.cfg.pin or not digit:
            return
        if digit.isdigit():
            self._dtmf = (self._dtmf + digit)[-16:]
            if hmac.compare_digest(self._dtmf[-len(self.cfg.pin):], self.cfg.pin):
                self._authed = True
                self._dtmf = ""
                log.info("[%s] PIN verified by keypad", self.call_id)
                await self._system_note(
                    "The caller typed the correct PIN on the keypad. Confirm it and ask how you can help."
                )
            return
        if digit == "#" and self._dtmf:
            self._dtmf = ""
            result = self._pin_failed()
            await self._system_note(result["error"])
        elif digit == "*":
            self._dtmf = ""

    async def _system_note(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if not self._response_active:
            await self._send({"type": "response.create"})
