from __future__ import annotations

import hashlib
import math
import re
import struct
from collections import Counter
from typing import Iterable, Protocol, runtime_checkable


_LATIN_OR_NUMBER = re.compile(r"[a-zA-Z0-9_+#.-]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def text_tokens(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = [match.group(0) for match in _LATIN_OR_NUMBER.finditer(lowered)]
    for match in _CJK_RUN.finditer(lowered):
        run = match.group(0)
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return tokens


def estimate_tokens(text: str) -> int:
    return len(text_tokens(text))


@runtime_checkable
class EmbeddingProvider(Protocol):
    dimension: int
    name: str

    def embed(self, text: str) -> tuple[float, ...]: ...

    def pack(self, vector: Iterable[float]) -> bytes: ...

    @staticmethod
    def unpack(blob: bytes, dimension: int) -> tuple[float, ...]: ...

    @staticmethod
    def cosine(left: Iterable[float], right: Iterable[float]) -> float: ...


def _l2_normalize(vector: list[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        return tuple(value / norm for value in vector)
    return tuple(vector)


def _pack_vector(vector: Iterable[float], dimension: int) -> bytes:
    values = tuple(float(value) for value in vector)
    if len(values) != dimension:
        raise ValueError("embedding dimension mismatch")
    return struct.pack(f"<{dimension}f", *values)


def _unpack_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    expected = struct.calcsize(f"<{dimension}f")
    if len(blob) != expected:
        raise ValueError("stored embedding has an invalid byte length")
    return struct.unpack(f"<{dimension}f", blob)


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class HashingEmbedding:
    """Deterministic local embedding suitable for offline and privacy-first deployments.

    It uses signed feature hashing over Latin tokens plus CJK uni/bi-grams.  The
    implementation is intentionally dependency-free and exposes a provider-like
    interface so a semantic embedding backend can be introduced later without a
    storage schema change.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 64 or dimension > 4096:
            raise ValueError("embedding dimension must be between 64 and 4096")
        self.dimension = dimension
        self.name = f"local-hash-v1-{dimension}"

    def embed(self, text: str) -> tuple[float, ...]:
        counts = Counter(text_tokens(text))
        vector = [0.0] * self.dimension
        for token, frequency in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dimension
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[bucket] += sign * (1.0 + math.log(float(frequency)))
        return _l2_normalize(vector)

    def pack(self, vector: Iterable[float]) -> bytes:
        return _pack_vector(vector, self.dimension)

    @staticmethod
    def unpack(blob: bytes, dimension: int) -> tuple[float, ...]:
        return _unpack_vector(blob, dimension)

    @staticmethod
    def cosine(left: Iterable[float], right: Iterable[float]) -> float:
        return _cosine(left, right)


class BowEmbedding:
    """Deterministic bag-of-tokens projection for offline A/B harnesses.

    This is not a semantic model. It exists so CI can compare two versioned
    providers without downloading neural weights. Do not switch the product
    default to this provider based on harness results alone.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 64 or dimension > 4096:
            raise ValueError("embedding dimension must be between 64 and 4096")
        self.dimension = dimension
        self.name = f"local-bow-v1-{dimension}"

    def embed(self, text: str) -> tuple[float, ...]:
        counts = Counter(text_tokens(text))
        vector = [0.0] * self.dimension
        for token, frequency in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[8:12], "big") % self.dimension
            sign = -1.0 if digest[12] & 1 else 1.0
            vector[bucket] += sign * math.sqrt(1.0 + float(frequency))
        return _l2_normalize(vector)

    def pack(self, vector: Iterable[float]) -> bytes:
        return _pack_vector(vector, self.dimension)

    @staticmethod
    def unpack(blob: bytes, dimension: int) -> tuple[float, ...]:
        return _unpack_vector(blob, dimension)

    @staticmethod
    def cosine(left: Iterable[float], right: Iterable[float]) -> float:
        return _cosine(left, right)


class LocalSemanticEmbedding:
    """Optional local sentence-transformer provider (384-d MiniLM family).

    Requires `requirements-ka2-semantic.txt`. Never used as the product default
    in this slice; intended for offline A/B only when extras are installed.
    """

    def __init__(
        self,
        *,
        model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        dimension: int = 384,
    ) -> None:
        if dimension != 384:
            raise ValueError("LocalSemanticEmbedding currently requires dimension=384")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "local semantic embedding requires extras from requirements-ka2-semantic.txt"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.dimension = dimension
        self.name = f"local-minilm-l12-v1-{dimension}"
        self.model_name = model_name

    def embed(self, text: str) -> tuple[float, ...]:
        vector = self._model.encode(text, normalize_embeddings=True)
        values = [float(item) for item in vector]
        if len(values) != self.dimension:
            raise ValueError(
                f"semantic embedding returned {len(values)} dims; expected {self.dimension}"
            )
        return tuple(values)

    def pack(self, vector: Iterable[float]) -> bytes:
        return _pack_vector(vector, self.dimension)

    @staticmethod
    def unpack(blob: bytes, dimension: int) -> tuple[float, ...]:
        return _unpack_vector(blob, dimension)

    @staticmethod
    def cosine(left: Iterable[float], right: Iterable[float]) -> float:
        return _cosine(left, right)


def create_embedding(provider: str = "hash", *, dimension: int = 384) -> EmbeddingProvider:
    key = str(provider).strip().casefold()
    if key in {"hash", "local-hash", "local-hash-v1"}:
        return HashingEmbedding(dimension)
    if key in {"bow", "local-bow", "local-bow-v1"}:
        return BowEmbedding(dimension)
    if key in {"semantic", "local-semantic", "minilm"}:
        return LocalSemanticEmbedding(dimension=dimension)
    raise ValueError(f"unknown embedding provider: {provider}")
