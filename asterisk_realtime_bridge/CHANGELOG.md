# Changelog

## 0.2.6
- Fix: spoken PINs were rejected when the model passed them as words or grouped numbers. The `verify_pin` tool now asks for individual numerals, and spelled-out digit words (English/Spanish) are accepted.
- With `log_level: debug`, PIN checks log the digit count and number of matching positions (never the PIN itself).

## 0.2.5
- Added `icon.png` and `logo.png` for the add-on.

## 0.2.4
- The assistant prompt is now configurable (`prompt` option), pre-filled with the default template. Placeholders pull the assistant name, language and accent from their own options (`{assistant_name}`, `{language}`, `{language_name}`, `{accent_line}`) and the PIN rules from `pin_enabled` (`{pin_rules}`). An empty value uses the built-in template. `extra_instructions` is still appended after it.

## 0.2.3
- Fix: the assistant greeted in English until the caller spoke. The greeting instruction was in English and did not name a language. It now states the configured language explicitly, and the instructions use the language name ("Spanish") instead of only the ISO code.
- New optional `accent` setting to steer the accent/dialect (e.g. Castilian Spanish).

## 0.2.2
- Fix: calls were dropped about 2 seconds after being answered. Asterisk's `AudioSocket()` application hangs up when it sees no activity for 2000 ms, and some carriers only start sending media after receiving some. The bridge now streams silence to Asterisk continuously from the first moment (also while connecting to OpenAI and whenever the assistant is not speaking).

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
