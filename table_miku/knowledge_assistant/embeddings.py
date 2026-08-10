from __future__ import annotations

import hashlib
import math
import re
import struct
from collections import Counter
from typing import Iterable


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
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)

    def pack(self, vector: Iterable[float]) -> bytes:
        values = tuple(float(value) for value in vector)
        if len(values) != self.dimension:
            raise ValueError("embedding dimension mismatch")
        return struct.pack(f"<{self.dimension}f", *values)

    @staticmethod
    def unpack(blob: bytes, dimension: int) -> tuple[float, ...]:
        expected = struct.calcsize(f"<{dimension}f")
        if len(blob) != expected:
            raise ValueError("stored embedding has an invalid byte length")
        return struct.unpack(f"<{dimension}f", blob)

    @staticmethod
    def cosine(left: Iterable[float], right: Iterable[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))
