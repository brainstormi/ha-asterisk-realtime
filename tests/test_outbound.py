import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "asterisk_realtime_bridge"))

from app import config  # noqa: E402
from app.audiosocket import KIND_DTMF, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.outbound import (  # noqa: E402
    CommandError, CommandParser, OutboundCall, Registry, parse_command, read_commands,
)
from app.session import CallSession, build_session_update  # noqa: E402

from tests.test_session import FakeHome, FakeWriter, FakeWS, call  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class FakeQuery(FakeHome):
    async def ask(self, text):
        self.calls.append(text)
        return {"type": "query_answer", "response": "The garage door is closed."}


# --------------------------------------------------------------- config
class PinConfigTests(unittest.TestCase):
    def _load(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(data, fh)
        try:
            return config.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_personal_pins_alone_are_enough(self):
        cfg = self._load({"openai_api_key": "k", "pins": [{"name": "Sonia", "pin": "4321"}]})
        self.assertEqual(cfg.pin, "")
        self.assertEqual(cfg.pin_entries, (("Sonia", "4321"),))

    def test_global_and_personal_pins(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1111",
                          "pins": [{"name": "Sonia", "pin": "4321"}, {"name": "Raul", "pin": "8765"}]})
        self.assertEqual(cfg.pin_entries, (("", "1111"), ("Sonia", "4321"), ("Raul", "8765")))

    def test_invalid_personal_pins(self):
        bad = [
            [{"name": "", "pin": "1234"}],
            [{"name": "Sonia", "pin": "12a4"}],
            [{"name": "Sonia", "pin": "1234"}, {"name": "sonia", "pin": "5678"}],
            [{"name": "Sonia", "pin": "1234"}, {"name": "Raul", "pin": "1234"}],
            "1234",
        ]
        for pins in bad:
            with self.assertRaises(config.ConfigError, msg=pins):
                self._load({"openai_api_key": "k", "pins": pins})
        with self.assertRaises(config.ConfigError):
            self._load({"openai_api_key": "k", "pin": "1234", "pins": [{"name": "Sonia", "pin": "1234"}]})

    def test_pins_ignored_when_pin_disabled(self):
        cfg = self._load({"openai_api_key": "k", "pin_enabled": False,
                          "pins": [{"name": "Sonia", "pin": "4321"}]})
        self.assertEqual(cfg.pin_entries, ())

    def test_query_agent_option(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1", "query_agent": "conversation.consulta"})
        self.assertEqual(cfg.query_agent, "conversation.consulta")

    def test_outbound_instructions(self):
        cfg = Config(openai_api_key="k", pin="1234")
        inbound = cfg.instructions_for(None)
        self.assertEqual(inbound, cfg.instructions)
        self.assertIn("Before doing anything else", inbound)

        only_query = cfg.instructions_for("soloconsulta")
        self.assertNotIn("Before doing anything else", only_query)
        self.assertIn("OUTBOUND", only_query)
        self.assertIn("no PIN", only_query)

        manage = cfg.instructions_for("gestion")
        self.assertIn("verify_pin", manage)
        self.assertNotIn("Before doing anything else", manage)

        open_manage = Config(openai_api_key="k", pin_enabled=False).instructions_for("gestion")
        self.assertNotIn("verify_pin", open_manage)

    def test_outbound_rules_are_added_to_a_custom_prompt(self):
        cfg = Config(openai_api_key="k", pin="1234", prompt="Short custom prompt.")
        self.assertIn("confirm_notice", cfg.instructions_for("soloconsulta"))
        self.assertEqual(cfg.instructions, "Short custom prompt.\n")


# ------------------------------------------------------------- commands
class CommandTests(unittest.TestCase):
    def test_parse_valid_command(self):
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID.upper(), "message": " Alarm ",
                             "profile": "Gestion", "person": "Sonia", "timeout": "45"})
        self.assertEqual(cmd.call_id, CALL_ID)
        self.assertEqual(cmd.message, "Alarm")
        self.assertEqual(cmd.profile, "gestion")
        self.assertEqual(cmd.person, "Sonia")
        self.assertEqual(cmd.timeout, 45)

    def test_parse_defaults_and_limits(self):
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                             "profile": "soloconsulta", "timeout": 99999})
        self.assertEqual(cmd.timeout, 600)
        self.assertEqual(cmd.person, "")

    def test_parse_invalid_commands(self):
        base = {"action": "outbound", "call_id": CALL_ID, "message": "x", "profile": "gestion"}
        for change in ({"action": "dance"}, {"call_id": "not-a-uuid"}, {"message": ""},
                       {"profile": "admin"}, {"timeout": "soon"}):
            with self.assertRaises(CommandError, msg=change):
                parse_command({**base, **change})
        with self.assertRaises(CommandError):
            parse_command(["outbound"])

    def test_parser_handles_objects_strings_and_split_chunks(self):
        parser = CommandParser()
        obj = {"action": "outbound", "call_id": CALL_ID}
        # An object, as hassio.addon_stdin sends a mapping.
        self.assertEqual(parser.feed(json.dumps(obj) + "\n"), [obj])
        # A JSON string containing the command (input given as text).
        self.assertEqual(parser.feed(json.dumps(json.dumps(obj))), [obj])
        # Split across reads, without a trailing newline.
        text = json.dumps(obj)
        self.assertEqual(parser.feed(text[:10]), [])
        self.assertEqual(parser.feed(text[10:]), [obj])
        # Two commands in one read.
        self.assertEqual(parser.feed(text + text), [obj, obj])

    def test_parser_skips_garbage_lines(self):
        parser = CommandParser()
        obj = {"a": 1}
        self.assertEqual(parser.feed("hello there\n" + json.dumps(obj)), [obj])
        self.assertEqual(parser.feed('"not json inside"\n'), [])


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_take_claims_the_call_once(self):
        registry = Registry()
        registry.add(OutboundCall(CALL_ID, "msg", "gestion"))
        self.assertEqual(registry.take(CALL_ID).message, "msg")
        self.assertIsNone(registry.take(CALL_ID))
        await registry.close()

    async def test_unanswered_call_expires_and_is_reported(self):
        missed = []

        async def on_expired(c):
            missed.append(c.call_id)

        registry = Registry(on_expired=on_expired)
        call_ = OutboundCall(CALL_ID, "msg", "gestion", timeout=10)
        call_.timeout = 0.05  # shorter than the parser allows, for the test
        registry.add(call_)
        await asyncio.sleep(0.12)
        self.assertEqual(missed, [CALL_ID])
        self.assertIsNone(registry.take(CALL_ID))
        await registry.close()

    async def test_read_commands_from_a_stream(self):
        reader = asyncio.StreamReader()
        got = []

        async def handle(cmd):
            got.append(cmd)
            if cmd.get("boom"):
                raise RuntimeError("bad command")

        reader.feed_data(b'{"boom": true}\n{"ok": 1}\n')
        reader.feed_eof()
        await asyncio.wait_for(read_commands(reader, handle), 1)
        self.assertEqual(got, [{"boom": True}, {"ok": 1}])


# -------------------------------------------------------------- session
class OutboundSessionTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, profile, cfg=None, query=True, message="The garage door opened at 08:57."):
        self.cfg = cfg or Config(openai_api_key="k", pin="1111",
                                 pins=(("Sonia", "4321"),), max_call_seconds=5)
        self.ws, self.writer = FakeWS(), FakeWriter()
        self.home, self.query = FakeHome(), (FakeQuery() if query else None)
        self.reader = asyncio.StreamReader()
        outbound = OutboundCall(CALL_ID, message, profile, person="Sonia")
        self.session = CallSession(self.cfg, "out", self.reader, self.writer, self.ws, self.home,
                                   outbound=outbound, query_home=self.query)
        self.task = asyncio.create_task(self.session.run())
        await asyncio.sleep(0.01)
        self.ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def tool_names(self):
        return [t["name"] for t in self.ws.sent[0]["session"]["tools"]]

    async def tool(self, name, args, call_id):
        self.ws.push(call(name, args, call_id))
        await asyncio.sleep(0.05)
        return self.ws.tool_outputs()[-1]

    async def test_greeting_reads_the_notice(self):
        await self.start("soloconsulta")
        hint = self.ws.of_type("response.create")[0]["response"]["instructions"]
        self.assertIn("The garage door opened at 08:57.", hint)
        self.assertIn("confirm", hint)
        self.assertNotIn("PIN", hint)
        self.assertTrue(self.session.connected)

    async def test_soloconsulta_only_reads_and_has_no_pin(self):
        await self.start("soloconsulta")
        self.assertEqual(self.tool_names(), ["home", "hang_up", "confirm_notice"])
        self.assertIn("OUTBOUND", self.ws.sent[0]["session"]["instructions"])

        out = await self.tool("home", {"request": "open the garage door"}, "q1")
        self.assertEqual(self.query.calls, ["open the garage door"])
        self.assertEqual(self.home.calls, [], "the full agent is never used")
        self.assertIn("nothing can be changed", out["note"])

        # Even the right PIN does not unlock anything in this profile.
        out = await self.tool("verify_pin", {"pin": "1111"}, "p1")
        self.assertFalse(out["ok"])
        for digit in "1111":
            self.reader.feed_data(pack_frame(KIND_DTMF, digit.encode()))
        await asyncio.sleep(0.05)
        await self.tool("home", {"request": "open the garage door"}, "q2")
        self.assertEqual(self.home.calls, [])
        self.assertIsNone(self.session.pin_person)

    async def test_gestion_reads_first_and_acts_after_a_personal_pin(self):
        await self.start("gestion")
        self.assertEqual(self.tool_names(), ["verify_pin", "home", "hang_up", "confirm_notice"])

        out = await self.tool("home", {"request": "is the garage door open?"}, "q1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.query.calls, ["is the garage door open?"])
        self.assertIn("PIN", out["note"])

        self.assertFalse((await self.tool("verify_pin", {"pin": "9999"}, "p1"))["ok"])
        self.assertTrue((await self.tool("verify_pin", {"pin": "cuatro tres dos uno"}, "p2"))["ok"])
        self.assertEqual(self.session.pin_person, "Sonia")

        out = await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, ["close the garage door"])
        self.assertNotIn("note", out)

    async def test_gestion_global_pin_by_keypad(self):
        await self.start("gestion")
        for digit in "1111":
            self.reader.feed_data(pack_frame(KIND_DTMF, digit.encode()))
        await asyncio.sleep(0.05)
        self.assertEqual(self.session.pin_person, "")
        await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, ["close the garage door"])

    async def test_questions_unavailable_without_query_agent(self):
        await self.start("soloconsulta", query=False)
        out = await self.tool("home", {"request": "is anyone outside?"}, "q1")
        self.assertFalse(out["ok"])
        self.assertEqual(self.home.calls, [])

    async def test_confirm_notice_records_the_confirmation(self):
        await self.start("soloconsulta")
        self.assertFalse(self.session.confirmed)
        out = await self.tool("confirm_notice", {}, "c1")
        self.assertTrue(out["ok"])
        self.assertTrue(self.session.confirmed)

    async def test_gestion_without_any_pin_can_act(self):
        await self.start("gestion", cfg=Config(openai_api_key="k", pin_enabled=False, max_call_seconds=5))
        self.assertNotIn("verify_pin", self.tool_names())
        await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, ["close the garage door"])


class InboundPersonalPinTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_accepts_a_personal_pin_and_has_no_confirm_tool(self):
        cfg = Config(openai_api_key="k", pins=(("Raul", "8765"),), max_call_seconds=5)
        update = build_session_update(cfg)
        self.assertEqual([t["name"] for t in update["session"]["tools"]], ["verify_pin", "home", "hang_up"])

        ws, home = FakeWS(), FakeHome()
        session = CallSession(cfg, "in", asyncio.StreamReader(), FakeWriter(), ws, home, query_home=FakeQuery())
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.01)
        ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)
        self.assertIn("PIN", ws.of_type("response.create")[0]["response"]["instructions"])
        ws.push(call("home", {"request": "status"}, "h1"))
        await asyncio.sleep(0.05)
        self.assertFalse(ws.tool_outputs()[-1]["ok"], "inbound: nothing before the PIN, not even questions")
        ws.push(call("verify_pin", {"pin": "8765"}, "p1"))
        await asyncio.sleep(0.05)
        self.assertEqual(session.pin_person, "Raul")
        ws.push(call("home", {"request": "status"}, "h2"))
        await asyncio.sleep(0.05)
        self.assertEqual(home.calls, ["status"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
