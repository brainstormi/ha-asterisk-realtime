"""Add-on configuration loading and validation (/data/options.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_INTRO = """\
You are {name}, the voice assistant of this home. You are speaking on the \
phone, so keep your replies short and natural: no lists, no long explanations.

Language: speak the language with ISO code "{language}" (for example "es" is \
Spanish and "en" is English), unless the caller clearly speaks another one.

Security:
"""

_PIN_RULES = """\
- Before doing anything else, the caller must verify their PIN. Ask for it at \
the start and use the verify_pin tool with the digits they say. Until the PIN \
is correct, do nothing else and reveal nothing about the home.
- If the PIN is wrong, say so and ask again. Never reveal or hint at the PIN.
"""

_CONFIRM_RULE = """\
- For locks, alarms, doors, gates and garage doors always ask for explicit \
confirmation ("do you really want me to open it?") before acting.
"""

_REST = """
Home:
- For any action or question about the home, use the home tool with ONE clear \
request in natural language, for example "turn on the living room light" or \
"is the garage door open?". Write the request in the language with ISO code \
"{language}", because that is the language Home Assistant understands.
- Tell the caller the result in your own words. If the tool returns an error, \
say so honestly. Never invent states or actions.

Call:
- If the caller says goodbye or needs nothing else, say a brief goodbye and \
call the hang_up tool.
- If you are interrupted, stop talking and listen.
"""

# Retired by OpenAI on 2027-01-20.
DEPRECATED_MODELS = {"gpt-realtime", "gpt-realtime-mini"}

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """Invalid configuration (the add-on stops with a clear message)."""


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    language: str = "en"
    assistant_name: str = "Assistant"
    extra_instructions: str = ""
    pin_enabled: bool = True
    pin: str = ""
    max_pin_attempts: int = 3
    max_call_seconds: int = 300
    conversation_agent: str = "conversation.home_assistant"
    fallback_agent: str = ""
    vad: str = "server_vad"
    vad_threshold: float = 0.5
    vad_silence_ms: int = 500
    noise_reduction: str = "near_field"
    max_concurrent_calls: int = 1
    port: int = 9092
    bind_address: str = "127.0.0.1"
    log_level: str = "info"
    log_transcripts: bool = False
    # Advanced settings, only through environment variables (useful for tests).
    realtime_url: str = "wss://api.openai.com/v1/realtime"
    ha_api_urls: tuple[str, ...] = (
        "http://supervisor/core/api",
        "http://172.30.32.2/core/api",
    )
    ha_token: str = ""

    @property
    def instructions(self) -> str:
        text = _INTRO.format(name=self.assistant_name, language=self.language)
        if self.pin:
            text += _PIN_RULES
        text += _CONFIRM_RULE + _REST.format(language=self.language)
        if self.extra_instructions.strip():
            text += "\nAdditional instructions:\n" + self.extra_instructions.strip()
        return text


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def load(path: str | None = None) -> Config:
    options_path = Path(path or os.environ.get("BRIDGE_OPTIONS", "/data/options.json"))
    try:
        raw = json.loads(options_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as err:
        raise ConfigError(f"{options_path} is not valid JSON: {err}") from err

    def text(key: str, default: str) -> str:
        value = raw.get(key)
        return default if value is None else str(value).strip()

    defaults = Config(openai_api_key="")
    api_key = text("openai_api_key", "")
    if not api_key:
        raise ConfigError("openai_api_key is missing from the add-on configuration.")

    pin_enabled = _as_bool(raw.get("pin_enabled"), True)
    pin = text("pin", "") if pin_enabled else ""
    if pin_enabled:
        if not pin:
            raise ConfigError(
                "pin_enabled is on but no PIN is configured. Set a PIN "
                "(digits only) or turn pin_enabled off."
            )
        if not pin.isdigit():
            raise ConfigError("The PIN must contain digits only.")

    model = text("model", defaults.model)
    if model.lower().startswith("gpt-live"):
        raise ConfigError(
            f"{model} uses the GPT-Live API (/v1/live/sessions), which has a "
            "different protocol and is not implemented by this bridge yet. Use a "
            "gpt-realtime-* model, for example gpt-realtime-2.1 or "
            "gpt-realtime-2.1-mini."
        )
    if not model.lower().startswith("gpt-realtime"):
        raise ConfigError(
            f"Unsupported model: {model!r}. It must be a Realtime API model "
            "(gpt-realtime-2.1, gpt-realtime-2.1-mini, gpt-realtime-2, ...)."
        )

    vad = text("vad", defaults.vad)
    if vad not in ("server_vad", "semantic_vad"):
        raise ConfigError("vad must be server_vad or semantic_vad.")
    noise = text("noise_reduction", defaults.noise_reduction)
    if noise not in ("near_field", "far_field", "off"):
        raise ConfigError("noise_reduction must be near_field, far_field or off.")

    ha_urls = defaults.ha_api_urls
    if os.environ.get("HA_API_URL"):
        ha_urls = (os.environ["HA_API_URL"].rstrip("/"),)

    return Config(
        openai_api_key=api_key,
        model=model,
        voice=text("voice", defaults.voice),
        language=text("language", defaults.language),
        assistant_name=text("assistant_name", defaults.assistant_name),
        extra_instructions=text("extra_instructions", ""),
        pin_enabled=pin_enabled,
        pin=pin,
        max_pin_attempts=int(raw.get("max_pin_attempts", defaults.max_pin_attempts)),
        max_call_seconds=int(raw.get("max_call_seconds", defaults.max_call_seconds)),
        conversation_agent=text("conversation_agent", defaults.conversation_agent),
        fallback_agent=text("fallback_agent", ""),
        vad=vad,
        vad_threshold=float(raw.get("vad_threshold", defaults.vad_threshold)),
        vad_silence_ms=int(raw.get("vad_silence_ms", defaults.vad_silence_ms)),
        noise_reduction=noise,
        max_concurrent_calls=int(
            raw.get("max_concurrent_calls", defaults.max_concurrent_calls)
        ),
        port=int(raw.get("port", defaults.port)),
        bind_address=text("bind_address", defaults.bind_address),
        log_level=text("log_level", defaults.log_level).lower(),
        log_transcripts=_as_bool(raw.get("log_transcripts"), False),
        realtime_url=os.environ.get("OPENAI_REALTIME_URL", defaults.realtime_url),
        ha_api_urls=ha_urls,
        ha_token=os.environ.get("SUPERVISOR_TOKEN", ""),
    )
