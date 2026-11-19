import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from minimi.semantic import cosine, retrieve
from minimi.memory import context
from test_memory import seed


def vector(axis):
    return [float(i == axis) for i in range(512)]


class Embeddings:
    def __init__(self, *, fail=False, delay=0):
        self.calls = []
        self.fail, self.delay = fail, delay

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider secret must not escape")
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    index=i,
                    embedding=vector(
                        0 if ("relocating" in text or "moving" in text) else 1
                    ),
                )
                for i, text in enumerate(kwargs["input"])
            ],
            usage=SimpleNamespace(total_tokens=30),
        )


def setup(store, clock):
    sid, _ = store.space()
    old = store.create_chat(sid)
    target = seed(
        store,
        sid,
        old["id"],
        "I'm relocating to Bengaluru.",
        clock[0] - timedelta(days=2),
    )
    # Make recency-only retrieval insufficient.
    for i in range(8):
        seed(
            store,
            sid,
            old["id"],
            f"Discuss graph traversal example {i}.",
            clock[0] - timedelta(hours=20 - i),
        )
    store.clock = lambda: clock[0]
    new = store.create_chat(sid)
    turn, _ = store.begin(
        sid, new["id"], str(uuid4()), "Which city am I moving to?", "UTC", "auto"
    )
    return sid, new["id"], target, turn


def test_semantic_paraphrase_retrieval_reuses_persisted_vectors(store, clock):
    sid, cid, target, turn = setup(store, clock)
    embeddings = Embeddings()
    rows, stats = asyncio.run(
        retrieve(
            store,
            sid,
            turn["user_id"],
            "Which city am I moving to?",
            SimpleNamespace(embeddings=embeddings),
            "test",
        )
    )
    assert rows[0]["id"] == target["user_id"]
    assert stats["status"] == "completed" and stats["indexed_candidates"] == 9
    packet, _ = context(
        store, sid, cid, "Which city am I moving to?", "UTC", clock[0], semantic=rows
    )
    assert any(
        r["id"] == target["user_id"] and "semantic" in r["retrieval_reasons"]
        for r in packet["memory_records"]
    )
    asyncio.run(
        retrieve(
            store,
            sid,
            turn["user_id"],
            "Which city am I moving to?",
            SimpleNamespace(embeddings=embeddings),
            "test",
        )
    )
    assert len(embeddings.calls[1]["input"]) == 1  # only query; no re-indexing
    assert embeddings.calls[0]["dimensions"] == 512


@pytest.mark.parametrize("fail,delay,timeout", [(True, 0, 1), (False, 0.05, 0.001)])
def test_embedding_failure_is_bounded_and_falls_back_without_leaking(
    store, clock, fail, delay, timeout
):
    sid, cid, _, turn = setup(store, clock)
    rows, stats = asyncio.run(
        retrieve(
            store,
            sid,
            turn["user_id"],
            "moving",
            SimpleNamespace(embeddings=Embeddings(fail=fail, delay=delay)),
            "test",
            timeout=timeout,
        )
    )
    assert rows == [] and stats["status"] == "fallback"
    assert "secret" not in str(stats)
    packet, _ = context(store, sid, cid, "moving", "UTC", clock[0], semantic=rows)
    assert packet["memory_records"]  # recency fallback remains available


def test_embedding_storage_is_scoped_and_cascades_on_delete(store, clock):
    sid, _, target, _ = setup(store, clock)
    other, _ = store.space()
    store.save_embedding(other, target["user_id"], "test", vector(0))
    assert store.embeddings(sid, "test") == {}
    store.save_embedding(sid, target["user_id"], "test", vector(0))
    assert target["user_id"] in store.embeddings(sid, "test")
    assert store.embeddings(other, "test") == {}
    store.forget(sid)
    store.save_embedding(sid, target["user_id"], "test", vector(0))
    assert store.embeddings(sid, "test") == {}


def test_foreign_semantic_candidates_are_reauthorized(store, clock):
    sid, cid, _, _ = setup(store, clock)
    other, _ = store.space()
    old = store.create_chat(other)
    foreign = seed(store, other, old["id"], "Secret relocation destination.")
    packet, _ = context(
        store,
        sid,
        cid,
        "moving",
        "UTC",
        clock[0],
        semantic=[{"id": foreign["user_id"], "semantic_score": 1}],
    )
    assert "Secret relocation" not in str(packet)


@pytest.mark.parametrize(
    "a,b",
    [([], []), ([0], [0]), ([1], [1, 2]), ([float("nan")], [1]), ([float("inf")], [1])],
)
def test_invalid_vectors_do_not_create_matches(a, b):
    assert cosine(a, b) == 0
