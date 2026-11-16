import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "asterisk_realtime_bridge"))

# aiohttp is not needed to test the agent logic: use a minimal stand-in.
fake_aiohttp = types.ModuleType("aiohttp")


class ClientConnectorError(Exception):
    pass


fake_aiohttp.ClientConnectorError = ClientConnectorError
fake_aiohttp.ClientTimeout = lambda total=None: ("timeout", total)
fake_aiohttp.ClientSession = object
sys.modules["aiohttp"] = fake_aiohttp

from app.ha import HomeAssistant  # noqa: E402


class FakeResponse:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class FakeHttp:
    """Answers per agent_id; URLs listed in `down` are unreachable."""

    def __init__(self, replies, down=()):
        self.replies = replies
        self.down = set(down)
        self.requests = []

    def post(self, url, json, headers, timeout):
        self.requests.append((url, json["agent_id"], headers["Authorization"]))
        base = url.rsplit("/conversation/process", 1)[0]
        if base in self.down:
            raise ClientConnectorError()
        return FakeResponse(self.replies[json["agent_id"]])


def reply(kind, speech):
    return {"response": {"response_type": kind, "speech": {"plain": {"speech": speech}}}}


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_agent_answer(self):
        http = FakeHttp({"conversation.home_assistant": reply("action_done", "Done")})
        ha = HomeAssistant(http, ("http://a",), "tok", "conversation.home_assistant", "en")
        self.assertEqual(await ha.ask("turn on the light"), {"type": "action_done", "response": "Done"})
        self.assertEqual(http.requests[0][2], "Bearer tok")

    async def test_fallback_only_when_primary_errors(self):
        replies = {
            "conversation.home_assistant": reply("error", "I did not understand"),
            "conversation.openai_conversation": reply("action_done", "Ready"),
        }
        http = FakeHttp(replies)
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en",
                           "conversation.openai_conversation")
        self.assertEqual((await ha.ask("set the movie scene"))["response"], "Ready")
        self.assertEqual([r[1] for r in http.requests],
                         ["conversation.home_assistant", "conversation.openai_conversation"])

        http = FakeHttp({**replies, "conversation.home_assistant": reply("action_done", "Done")})
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en",
                           "conversation.openai_conversation")
        await ha.ask("turn on the light")
        self.assertEqual([r[1] for r in http.requests], ["conversation.home_assistant"])

    async def test_no_fallback_configured_returns_error_result(self):
        http = FakeHttp({"conversation.home_assistant": reply("error", "No match")})
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en")
        self.assertEqual((await ha.ask("x"))["type"], "error")

    async def test_falls_back_to_second_url_and_remembers_it(self):
        http = FakeHttp({"a": reply("action_done", "ok")}, down={"http://supervisor"})
        ha = HomeAssistant(http, ("http://supervisor", "http://172.30.32.2"), "t", "a", "en")
        await ha.ask("one")
        await ha.ask("two")
        urls = [r[0] for r in http.requests]
        self.assertEqual(urls[0], "http://supervisor/conversation/process")
        self.assertEqual(urls[1], "http://172.30.32.2/conversation/process")
        self.assertEqual(urls[2], "http://172.30.32.2/conversation/process")  # no retry of the dead URL

    async def test_all_urls_down_raises(self):
        http = FakeHttp({}, down={"http://a"})
        ha = HomeAssistant(http, ("http://a",), "t", "x", "en")
        with self.assertRaises(ConnectionError):
            await ha.ask("x")


if __name__ == "__main__":
    unittest.main()
