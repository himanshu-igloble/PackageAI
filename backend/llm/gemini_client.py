"""Gemini-only LLM client with role-split methods.

- `intake(...)` uses `GEMINI_INTAKE_MODEL` (default 2.5 Flash) for conversation,
  classification, and user-facing dialogue.
- `reason(...)` uses `GEMINI_REASONING_MODEL` (default 3 Pro) for engineering
  reasoning, design exploration, and the self-check verification pass.

Each role has TWO failover axes:

  1. Multiple API keys. `GEMINI_API_KEY` is the primary; `GEMINI_API_KEY2`
     (when set in .env) is tried automatically on quota / auth errors.
  2. A model fallback chain per role. Within each key, models are tried in
     order until one returns a response.

Returns a deterministic stub object when no key is present so the app stays
runnable in fully-offline development.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def _force_json(raw: str) -> dict[str, Any]:
    raw = _strip_code_fences(raw)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {"_parse_error": True, "raw": raw[:500]}


# Errors that should trigger an API-key rotation rather than just a model
# fallback (the same call can succeed on a different account).
_KEY_ROTATE_HINTS = ("429", "quota", "rate", "permission", "invalid_api_key",
                     "api key", "unauthorized", "forbidden", "auth")


def _err_should_rotate_key(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in _KEY_ROTATE_HINTS)


def _err_should_try_next_model(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in (
        "not found", "404", "unsupported", "model", "timeout", "deadline", "5xx",
    ))


class GeminiClient:
    """Single SDK client family, two role-tagged methods, with key rotation."""

    def __init__(self) -> None:
        self._clients: list[Any] = []
        self._labels: list[str] = []
        self._init_clients()

    def _init_clients(self) -> None:
        try:
            from google import genai
        except Exception as exc:  # noqa: BLE001
            print(f"[GeminiClient] google-genai import failed: {exc!r}; running in stub mode")
            return
        keys = []
        if settings.GEMINI_API_KEY:
            keys.append(("primary", settings.GEMINI_API_KEY))
        if settings.GEMINI_API_KEY2:
            keys.append(("secondary", settings.GEMINI_API_KEY2))
        for label, key in keys:
            try:
                self._clients.append(genai.Client(api_key=key))
                self._labels.append(label)
            except Exception as exc:  # noqa: BLE001
                print(f"[GeminiClient] init {label} key failed: {exc!r}")
        if not self._clients:
            print("[GeminiClient] no usable Gemini API key — running in stub mode")

    @property
    def available(self) -> bool:
        return bool(self._clients)

    def _generate(self, model_chain: list[str], system: str, user: str, *,
                  temperature: float, json_mode: bool) -> str | None:
        if not self._clients:
            return None
        prompt = f"{system}\n\n{user}"
        if json_mode:
            prompt = (
                f"{system}\n\n"
                "Return STRICTLY a single JSON object — no prose, no markdown fences.\n\n"
                f"Input:\n{user}"
            )
        cfg: dict[str, Any] = {"temperature": float(temperature)}
        if json_mode:
            cfg["response_mime_type"] = "application/json"

        last_err: Exception | None = None
        # Outer loop: API keys. Inner loop: model chain for the chosen key.
        # On a quota / auth-like error we abandon the current key and try the
        # next one (still with the preferred model first).
        for client, label in zip(self._clients, self._labels):
            for model in model_chain:
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[{"role": "user", "parts": [{"text": prompt}]}],
                        config=cfg,
                    )
                    return (resp.text or "").strip()
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if _err_should_rotate_key(exc):
                        print(f"[GeminiClient] {label}/{model} key-level failure ({exc!r}); rotating key")
                        break  # break inner loop → try next key from the top
                    if _err_should_try_next_model(exc):
                        print(f"[GeminiClient] {label}/{model} unavailable; next model")
                        continue
                    # Unknown error: be conservative — try next model.
                    print(f"[GeminiClient] {label}/{model} error ({exc!r}); next model")
                    continue
        print(f"[GeminiClient] all keys/models exhausted; last error: {last_err!r}")
        return None

    # ---- Role: intake / conversation (Gemini 2.5 Flash) ------------------
    # Default temperature raised to 0.6 — Flash needs latitude to classify
    # informal user replies confidently instead of stalling on confirmation
    # questions. Callers can override per-call.

    def intake_text(self, system: str, user: str, *, temperature: float = 0.6) -> str:
        out = self._generate(settings.intake_model_chain, system, user,
                             temperature=temperature, json_mode=False)
        return out if out is not None else self._stub_text("intake")

    def intake_json(self, system: str, user: str, *, temperature: float = 0.5) -> dict[str, Any]:
        out = self._generate(settings.intake_model_chain, system, user,
                             temperature=temperature, json_mode=True)
        return _force_json(out) if out else self._stub_json("intake")

    # ---- Role: reasoning / verification (Gemini 3 Pro) -------------------
    # Default temperature stays low for verification self-checks. Design
    # exploration callers (e.g. optimisation alternatives) override up to 1.0
    # so the LLM proposes a genuinely diverse set of variants.

    def reason_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        out = self._generate(settings.reasoning_model_chain, system, user,
                             temperature=temperature, json_mode=False)
        return out if out is not None else self._stub_text("reasoning")

    def reason_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict[str, Any]:
        out = self._generate(settings.reasoning_model_chain, system, user,
                             temperature=temperature, json_mode=True)
        return _force_json(out) if out else self._stub_json("reasoning")

    # ---- Stubs (so the app still runs without a key) ---------------------

    def _stub_text(self, role: str) -> str:
        return f"[stub-mode {role}] no Gemini key configured or all models unavailable."

    def _stub_json(self, role: str) -> dict[str, Any]:
        return {"_stub": True, "role": role, "ok": True, "issues": [], "warnings": [], "narrative": ""}


_singleton: GeminiClient | None = None


def get_gemini() -> GeminiClient:
    global _singleton
    if _singleton is None:
        _singleton = GeminiClient()
    return _singleton
