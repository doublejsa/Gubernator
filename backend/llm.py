"""
LLM provider abstraction — Anthropic (native) + OpenAI-compatible providers
(OpenAI, xAI/Grok, Google Gemini, OpenRouter).

One interface for the chat supervisor: stream / complete / count_tokens, with
provider errors normalised so ws_handlers maps them to friendly UI states once.
Claude is the default and recommended provider — the action system is tuned on
it; others are offered as experimental.
"""
from __future__ import annotations
import asyncio
from typing import Callable, Awaitable, Optional

import anthropic as _anthropic
try:
    import openai as _openai
except ImportError:          # keep Anthropic-only deploys working
    _openai = None


# ── Normalised errors ─────────────────────────────────────────────────────────
class LLMRateLimit(Exception): pass
class LLMTimeout(Exception): pass
class LLMCredits(Exception):
    """Out of credits / quota — message carries the provider's own wording."""
class LLMStatusError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ── Claude model ladder (supervisor quality tiers, plain-English) ─────────────
# Prices: USD per 1M tokens. cache_read ≈ 0.1× input, cache_write ≈ 1.25× input.
MODEL_GUIDE: list[dict] = [
    {
        "id": "claude-opus-4-8", "label": "Best (recommended)",
        "desc": "Claude Opus — excellent judgement, rarely gets stuck. The default.",
        "cost_hint": "typical session: a few dollars",
        "context_window": 1_000_000, "recommended": True,
        "price": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    },
    {
        "id": "claude-fable-5", "label": "Maximum",
        "desc": "Claude Fable — Anthropic's most capable model, for when things are really stuck. ~2× cost.",
        "cost_hint": "~2× Best",
        "context_window": 1_000_000, "recommended": False,
        "price": {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    },
    {
        "id": "claude-sonnet-5", "label": "Balanced",
        "desc": "Claude Sonnet — near-Best quality at about half the cost.",
        "cost_hint": "~half of Best",
        "context_window": 1_000_000, "recommended": False,
        "price": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    },
    {
        "id": "claude-haiku-4-5-20251001", "label": "Budget",
        "desc": "Claude Haiku — cheapest and fastest, but noticeably weaker on tricky problems.",
        "cost_hint": "typical session: under a dollar",
        "context_window": 200_000, "recommended": False,
        "price": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    },
]

def model_info(model_id: str) -> Optional[dict]:
    for m in MODEL_GUIDE:
        # match exact ids, dated variants, and bare aliases (claude-haiku-4-5)
        if m["id"] == model_id or model_id.startswith(m["id"]) or m["id"].startswith(model_id):
            return m
    return None


# ── Provider registry ─────────────────────────────────────────────────────────
# price: USD per 1M tokens for the DEFAULT model (0 = don't estimate cost).
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Claude (Anthropic)", "cred": "_anthropic_key",
        "default_model": "claude-opus-4-8",
        "context_window": 1_000_000, "experimental": False,
        "key_hint": "sk-ant-…", "key_url": "https://console.anthropic.com/settings/api-keys",
        "price": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    },
    "openai": {
        "label": "ChatGPT (OpenAI)", "cred": "_openai_key",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5-mini",
        "context_window": 272_000, "experimental": True,
        "key_hint": "sk-…", "key_url": "https://platform.openai.com/api-keys",
        "price": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    },
    "xai": {
        "label": "Grok (xAI)", "cred": "_xai_key",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4-fast",
        "context_window": 256_000, "experimental": True,
        "key_hint": "xai-…", "key_url": "https://console.x.ai",
        "price": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    },
    "gemini": {
        "label": "Gemini (Google)", "cred": "_gemini_key",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "context_window": 1_000_000, "experimental": True,
        "key_hint": "AIza…", "key_url": "https://aistudio.google.com/apikey",
        "price": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    },
    "openrouter": {
        "label": "OpenRouter (many models)", "cred": "_openrouter_key",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-haiku-4.5",
        "context_window": 200_000, "experimental": True,
        "key_hint": "sk-or-…", "key_url": "https://openrouter.ai/keys",
        "price": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    },
}
DEFAULT_PROVIDER = "anthropic"


def normalize_provider(provider: Optional[str]) -> str:
    return provider if provider in PROVIDERS else DEFAULT_PROVIDER


def _estimate_tokens(system: str, messages: list[dict]) -> int:
    chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 4


class LLMClient:
    """Unified async client. `messages` use the Anthropic shape
    [{"role": "user"|"assistant", "content": str}] — converted per provider."""

    def __init__(self, provider: str, api_key: str, model: Optional[str] = None):
        self.provider = normalize_provider(provider)
        cfg = PROVIDERS[self.provider]
        self.model          = (model or "").strip() or cfg["default_model"]
        self.context_window = cfg["context_window"]
        self.price          = cfg["price"]
        self.label          = cfg["label"]
        if self.provider == "anthropic":
            info = model_info(self.model)
            if info:   # accurate cost/context for whichever Claude tier is chosen
                self.context_window = info["context_window"]
                self.price          = info["price"]
        if self.provider == "anthropic":
            self._a = _anthropic.AsyncAnthropic(api_key=api_key)
        else:
            if _openai is None:
                raise RuntimeError("openai package not installed — cannot use this provider")
            self._o = _openai.AsyncOpenAI(api_key=api_key, base_url=cfg["base_url"])

    # ── token counting ────────────────────────────────────────────────────────
    async def count_tokens(self, system: str, messages: list[dict]) -> int:
        if self.provider == "anthropic":
            try:
                r = await self._a.messages.count_tokens(
                    model=self.model, system=system, messages=messages)
                return r.input_tokens
            except Exception:
                return _estimate_tokens(system, messages)
        return _estimate_tokens(system, messages)

    # ── one-shot completion (compression, summaries) ──────────────────────────
    async def complete(self, system: str, messages: list[dict], max_tokens: int = 2048) -> str:
        if self.provider == "anthropic":
            resp = await self._a.messages.create(
                model=self.model, max_tokens=max_tokens, system=system, messages=messages)
            return resp.content[0].text
        resp = await self._o.chat.completions.create(
            model=self.model, max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, *messages])
        return resp.choices[0].message.content or ""

    # ── streaming ─────────────────────────────────────────────────────────────
    async def stream(self, system: str, messages: list[dict], max_tokens: int,
                     on_chunk: Callable[[str], Awaitable[None]]) -> tuple[str, dict]:
        """Stream a response; call on_chunk per text delta. Returns (full_text, usage)
        where usage = {input, output, cache_read, cache_write} (0s when unknown).
        Raises the normalised LLM* errors."""
        if self.provider == "anthropic":
            return await self._stream_anthropic(system, messages, max_tokens, on_chunk)
        return await self._stream_openai(system, messages, max_tokens, on_chunk)

    async def _stream_anthropic(self, system, messages, max_tokens, on_chunk):
        try:
            full = ""
            kwargs = dict(model=self.model, max_tokens=max_tokens,
                          system=system, messages=messages)
            if self.model.startswith("claude-fable"):
                # Fable's safety classifiers can false-positive on legitimate
                # server-admin work — fall back to Opus inside the same request.
                stream_cm = self._a.beta.messages.stream(
                    **kwargs, betas=["server-side-fallback-2026-06-01"],
                    fallbacks=[{"model": "claude-opus-4-8"}])
            else:
                stream_cm = self._a.messages.stream(**kwargs)
            async with stream_cm as stream:
                async for chunk in stream.text_stream:
                    full += chunk
                    await on_chunk(chunk)
                final = await stream.get_final_message()
                if getattr(final, "stop_reason", None) == "refusal":
                    raise LLMStatusError(
                        200, "The AI declined this request for safety reasons — "
                             "try rephrasing, or switch model in Settings.")
                u = final.usage
                usage = {
                    "input":       u.input_tokens,
                    "output":      u.output_tokens,
                    "cache_read":  getattr(u, "cache_read_input_tokens", 0) or 0,
                    "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
                }
            return full, usage
        except LLMStatusError:
            raise
        except _anthropic.RateLimitError as e:
            raise LLMRateLimit(str(e)) from e
        except (_anthropic.APITimeoutError, _anthropic.APIConnectionError) as e:
            raise LLMTimeout(str(e)) from e
        except _anthropic.APIStatusError as e:
            body = e.body if isinstance(e.body, dict) else {}
            msg  = (body.get("error", {}) or {}).get("message", str(e))
            if "credit" in msg.lower():
                raise LLMCredits(msg) from e
            raise LLMStatusError(e.status_code, msg) from e

    async def _stream_openai(self, system, messages, max_tokens, on_chunk):
        try:
            full = ""
            usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            kwargs = dict(
                model=self.model, max_completion_tokens=max_tokens, stream=True,
                messages=[{"role": "system", "content": system}, *messages],
            )
            try:
                stream = await self._o.chat.completions.create(
                    **kwargs, stream_options={"include_usage": True})
            except _openai.BadRequestError:
                # some OpenAI-compatible endpoints don't accept stream_options
                stream = await self._o.chat.completions.create(**kwargs)
            async for ev in stream:
                if ev.choices and ev.choices[0].delta and ev.choices[0].delta.content:
                    chunk = ev.choices[0].delta.content
                    full += chunk
                    await on_chunk(chunk)
                if getattr(ev, "usage", None):
                    usage["input"]  = ev.usage.prompt_tokens or 0
                    usage["output"] = ev.usage.completion_tokens or 0
            if not usage["input"]:
                usage["input"]  = _estimate_tokens(system, messages)
                usage["output"] = len(full) // 4
            return full, usage
        except _openai.RateLimitError as e:
            # OpenAI signals exhausted credits as a 429 insufficient_quota
            if "insufficient_quota" in str(e) or "quota" in str(e).lower():
                raise LLMCredits(str(e)) from e
            raise LLMRateLimit(str(e)) from e
        except (_openai.APITimeoutError, _openai.APIConnectionError) as e:
            raise LLMTimeout(str(e)) from e
        except _openai.APIStatusError as e:
            msg = str(e)
            if e.status_code == 402 or "quota" in msg.lower() or "credit" in msg.lower():
                raise LLMCredits(msg) from e
            raise LLMStatusError(e.status_code, msg) from e
