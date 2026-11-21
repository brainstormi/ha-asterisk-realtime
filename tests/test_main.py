"""Bridge wiring: commands from Home Assistant, call lookup and result events."""

import asyncio
import os
import sys
import types
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "asterisk_realtime_bridge"))

# Stand-ins for the network libraries (not installed in the test environment).
if "aiohttp" not in sys.modules:
    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.ClientConnectorError = type("ClientConnectorError", (Exception,), {})
    fake_aiohttp.ClientTimeout = lambda total=None: ("timeout", total)
    fake_aiohttp.ClientSession = object
    sys.modules["aiohttp"] = fake_aiohttp
for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
    sys.modules.setdefault(name, types.ModuleType(name))


def _no_openai(*args, **kwargs):
    raise ConnectionError("OpenAI unreachable (test)")


sys.modules["websockets.asyncio.client"].connect = _no_openai

from app import main  # noqa: E402
from app.audiosocket import KIND_UUID, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.outbound import OutboundCall  # noqa: E402

from tests.test_session import FakeWriter  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class EventWriter(FakeWriter):
    def get_extra_info(self, name):
        return ("127.0.0.1", 40000)

    def close(self):
        pass

    async def wait_closed(self):
        pass


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.connect = _no_openai
        self.bridge = main.Bridge(Config(openai_api_key="k", pin="1234"), http=None)
        self.events = []

        async def fire_event(event_type, data):
            self.events.append((event_type, data))

        self.bridge.home.fire_event = fire_event

    async def asyncTearDown(self):
        await self.bridge.registry.close()

    async def connect(self, call_id):
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, uuid.UUID(call_id).bytes))
        await self.bridge.handle(reader, EventWriter())

    async def test_command_registers_an_outbound_call(self):
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "Alarm",
                                      "profile": "QueryOnly"})
        self.assertEqual(len(self.bridge.registry), 1)
        self.assertEqual(self.events, [])

    async def test_invalid_command_is_reported_as_failed(self):
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "",
                                      "profile": "QueryOnly"})
        self.assertEqual(len(self.bridge.registry), 0)
        event_type, data = self.events[0]
        self.assertEqual(event_type, "asterisk_realtime_bridge_call")
        self.assertEqual((data["call_id"], data["stage"], data["result"]), (CALL_ID, "ended", "failed"))

    async def test_answered_outbound_call_reports_answered_then_failed_when_openai_is_down(self):
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "Management", person="Sonia"))
        await self.connect(CALL_ID)
        stages = [(d["stage"], d.get("result")) for _, d in self.events]
        self.assertEqual(stages, [("answered", None), ("ended", "failed")])
        ended = self.events[-1][1]
        self.assertEqual(ended["direction"], "outbound")
        self.assertEqual(ended["person"], "Sonia")
        self.assertFalse(ended["pin_verified"])

    async def test_unknown_uuid_is_an_incoming_call(self):
        await self.connect(str(uuid.uuid4()))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["direction"], "inbound")
        self.assertEqual(self.events[0][1]["profile"], "InboundCall")

    async def test_missed_outbound_call_reports_no_answer(self):
        call = OutboundCall(CALL_ID, "Alarm", "Management")
        call.timeout = 0.05
        self.bridge.registry.add(call)
        await asyncio.sleep(0.12)
        data = self.events[0][1]
        self.assertEqual((data["call_id"], data["result"]), (CALL_ID, "no_answer"))

    def test_result_values(self):
        class S:
            connected, confirmed, confirmed_by, pin_person = True, False, None, None

        session = S()
        self.assertEqual(self.bridge._ended(CALL_ID, None, session, 3.2)["result"], "answered")
        session.confirmed, session.confirmed_by, session.pin_person = True, "dtmf", "Raul"
        data = self.bridge._ended(CALL_ID, None, session, 3.2)
        self.assertEqual((data["result"], data["confirmed_by"], data["pin_person"], data["duration"]),
                         ("confirmed", "dtmf", "Raul", 3))
        self.assertEqual(data["profile"], "InboundCall")
        self.assertEqual(self.bridge._ended(CALL_ID, None, None, 0)["result"], "failed")


if __name__ == "__main__":
    unittest.main()
