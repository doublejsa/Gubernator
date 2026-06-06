"""
Local text embeddings via fastembed (BAAI/bge-small-en-v1.5, 384 dims).

No API key, no external service, runs on CPU. The model is lazy-loaded on
first use so app startup isn't blocked by the one-time model download.
"""
from __future__ import annotations
import asyncio
from typing import Optional

EMBED_DIM   = 384
_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_model = None   # lazy singleton


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(_MODEL_NAME)
    return _model


def _embed_sync(text: str) -> list[float]:
    model = _get_model()
    vecs  = list(model.embed([text]))
    return [float(x) for x in vecs[0]]


async def embed(text: str) -> Optional[list[float]]:
    """Embed a single string → 384-float vector. Returns None on failure
    so callers can store the record without an embedding rather than crash."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _embed_sync, text)
    except Exception:
        return None
