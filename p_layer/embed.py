"""Embedding provider abstraction. Stdlib-only, no numpy.

Providers are versioned: a provider's `embedding_version` is stored with every
vector, so swapping models never silently corrupts the vector space (the old
version stays queryable; a re-embed job re-fills the new version).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import urllib.error
import urllib.request

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_HOST = "http://localhost:11434"


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    name = "base"
    model = ""
    embedding_version = "base-1"
    dimensions = 0
    # Whether embeddings carry real semantics. Fake/fallback embedders
    # (e.g. deterministic random vectors) must set False so hybrid recall
    # defaults to FTS-only instead of fusing noise (see HashEmbedder).
    semantic = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def available(self) -> bool:
        return True


class OllamaEmbedder(Embedder):
    """Local embeddings via Ollama /api/embeddings."""

    name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None, timeout: int = 30):
        self.host = host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
        self.model = model or os.environ.get("EMBED_MODEL", DEFAULT_MODEL)
        self.embedding_version = f"ollama-{self.model}"
        self.dimensions = 0  # set from first response
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            data = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.host}/api/embeddings",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    vec = json.loads(resp.read())["embedding"]
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, KeyError, json.JSONDecodeError) as exc:
                raise EmbeddingError(f"ollama embedding failed: {exc}") from exc
            vec = [float(x) for x in vec]
            if not self.dimensions:
                self.dimensions = len(vec)
            out.append(vec)
        return out


class HashEmbedder(Embedder):
    """Deterministic pseudo-random embeddings. NOT semantic — offline/tests only.

    Same text → same vector; different text → decorrelated unit vectors.
    Lets the full hybrid pipeline run with zero network and zero deps.
    """

    name = "hash"
    semantic = False  # fusing these vectors hurts recall; FTS-only by default

    def __init__(self, dimensions: int = 64):
        self.model = "hash-fallback"
        self.embedding_version = f"hash-{dimensions}"
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            rng = random.Random(seed)
            vec = [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class NoopEmbedder(Embedder):
    """Semantic search explicitly disabled."""

    name = "none"

    def __init__(self):
        self.model = "none"
        self.embedding_version = "none"
        self.dimensions = 0

    def available(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("embeddings disabled")


def load_embedder(name: str | None = None) -> Embedder:
    name = name or os.environ.get("P_LAYER_EMBED", "ollama")
    if name == "ollama":
        return OllamaEmbedder()
    if name == "hash":
        return HashEmbedder()
    if name == "none":
        return NoopEmbedder()
    raise ValueError(f"unknown embedder: {name}")
