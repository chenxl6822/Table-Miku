from __future__ import annotations

import math

import pytest

from table_miku.knowledge_assistant.embeddings import (
    BowEmbedding,
    HashingEmbedding,
    LocalSemanticEmbedding,
    create_embedding,
)


def test_hash_and_bow_providers_roundtrip_and_differ():
    hash_provider = HashingEmbedding(384)
    bow_provider = BowEmbedding(384)
    text = "constructor injection and awaiting_approval receipt"
    left = hash_provider.embed(text)
    right = bow_provider.embed(text)
    assert len(left) == 384
    assert len(right) == 384
    assert abs(math.sqrt(sum(value * value for value in left)) - 1.0) < 1e-5
    packed = hash_provider.pack(left)
    unpacked = hash_provider.unpack(packed, 384)
    assert all(abs(a - b) < 1e-6 for a, b in zip(unpacked, left, strict=True))
    assert left != right
    assert create_embedding("hash").name == "local-hash-v1-384"
    assert create_embedding("bow").name == "local-bow-v1-384"


def test_semantic_provider_fails_closed_without_extras():
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="requirements-ka2-semantic"):
            LocalSemanticEmbedding()
        with pytest.raises(RuntimeError, match="requirements-ka2-semantic"):
            create_embedding("semantic")
    else:
        pytest.skip("semantic extras installed in this environment")
