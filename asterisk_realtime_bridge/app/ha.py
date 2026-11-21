"""Minimal Home Assistant client (REST API through the Supervisor proxy)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class HomeAssistant:
    """Sends natural-language requests to a Home Assistant conversation agent.

    The built-in Assist agent only acts on the entities exposed to Assist,
    which is exactly the safety boundary we want.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        urls: tuple[str, ...],
        token: str,
        agent_id: str,
        language: str,
        fallback_agent: str = "",
    ) -> None:
        self._http = http
        self._urls = list(urls)
        self._token = token
        self._agent_id = agent_id
        self._fallback_agent = fallback_agent
        self._language = language

    async def ask(self, text: str) -> dict[str, Any]:
        """Ask the main agent first; if it cannot handle the request and a
        fallback agent is configured (e.g. an LLM), hand the request to it."""
        result = await self._converse(self._agent_id, text)
        if result["type"] == "error" and self._fallback_agent:
            log.info(
                "Agent %s could not handle the request; trying %s",
                self._agent_id,
                self._fallback_agent,
            )
            result = await self._converse(self._fallback_agent, text)
        return result

    async def _converse(self, agent_id: str, text: str) -> dict[str, Any]:
        payload = {"text": text, "language": self._language, "agent_id": agent_id}
        data = await self._post("conversation/process", payload)
        response = data.get("response") or {}
        speech = ((response.get("speech") or {}).get("plain") or {}).get("speech")
        return {
            "type": response.get("response_type", "unknown"),
            "response": speech or "",
        }

    async def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event on the Home Assistant bus (call results for automations)."""
        await self._post(f"events/{event_type}", data)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = aiohttp.ClientTimeout(total=20)
        last_error: Exception | None = None

        for base in list(self._urls):
            try:
                async with self._http.post(
                    f"{base}/{path}",
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except aiohttp.ClientConnectorError as err:
                # No route to this URL: try the next one.
                last_error = err
                continue
            # Remember the URL that works for the following requests.
            self._urls.remove(base)
            self._urls.insert(0, base)
            return data if isinstance(data, dict) else {}
        raise ConnectionError(f"Could not reach Home Assistant: {last_error}")
