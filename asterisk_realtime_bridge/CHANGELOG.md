# Changelog

## 0.2.1
- Project made English-only and generic: code, comments, logs, default prompts, tool names (`home`, `verify_pin`, `hang_up`) and documentation. The spoken language is controlled by the `language` option. A Spanish UI translation is kept.
- New `bind_address` option (default `127.0.0.1`) for setups where Asterisk runs on another machine; it logs a warning when it is not loopback.
- Neutral defaults: `assistant_name: Assistant`, `language: en`.

## 0.2.0
- Configurable model: `gpt-realtime-2.1` (default), `gpt-realtime-2.1-mini`, `gpt-realtime-2`... any `gpt-realtime-*` Realtime API model. `gpt-live-*` is rejected with a clear message (different API, not supported yet). Warning for models retiring on 2027-01-20.
- New `pin_enabled` option to require or disable the PIN. With the PIN disabled the assistant never mentions a PIN and a warning is logged at startup.
- New `fallback_agent` option: if the main Home Assistant conversation agent cannot handle a request, another one (e.g. `conversation.openai_conversation`) is tried, like "prefer local intents" in an Assist pipeline.
- Repository ready to be added to the Home Assistant add-on store.

## 0.1.0
- First version: AudioSocket <-> OpenAI Realtime bridge (`audio/pcmu`, no resampling).
- Barge-in: local audio queue, instant flush and `conversation.item.truncate`.
- Tools to control the home, verify a PIN and hang up.
- PIN by voice or DTMF keypad, lockout after failed attempts.
- Listens on 127.0.0.1 only; only outbound connections to OpenAI.
