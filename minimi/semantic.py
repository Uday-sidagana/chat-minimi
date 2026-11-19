"""Host-managed semantic retrieval; never exposed as an agent tool."""

import asyncio
import math
import time


def cosine(left, right):
    if len(left) != len(right) or not left:
        return 0.0
    if not all(math.isfinite(x) for x in (*left, *right)):
        return 0.0
    norm = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(a * b for a, b in zip(left, right)) / norm if norm else 0.0


async def retrieve(store, sid, current_id, query, client, model, *, timeout=2.5):
    """Bound work, reuse saved vectors, and fail open to ordinary recall.

    Only the latest 128 user records are semantic candidates. At most 24 missing
    records are embedded per request. Current question doubles as tomorrow's index
    entry. The first turn needs no semantic call; a later lookup backfills it.
    """
    started = time.monotonic()
    stats = {"status": "unavailable", "model": model, "candidate_limit": 128}
    try:
        rows, total = store.recall(sid, limit=129)
        rows = [r for r in rows if r["id"] != current_id][:128]
        stats.update(candidates=len(rows), workspace_records=total)
        if not rows:
            return [], {
                **stats,
                "status": "empty",
                "seconds": time.monotonic() - started,
            }
        saved = store.embeddings(sid, model)
        missing = [r for r in rows if r["id"] not in saved][:24]
        inputs = [query[:6000]] + [r["content"][:6000] for r in missing]
        async with asyncio.timeout(timeout):
            response = await client.embeddings.create(
                model=model, input=inputs, dimensions=512, timeout=timeout
            )
        vectors = {item.index: item.embedding for item in response.data}
        if set(vectors) != set(range(len(inputs))):
            raise ValueError("Incomplete embedding batch")
        query_vector = vectors[0]
        if len(query_vector) != 512 or not all(
            len(v) == 512 and all(math.isfinite(x) for x in v) for v in vectors.values()
        ):
            raise ValueError("Invalid embedding batch")
        # Store only through scoped INSERT ... SELECT. A deletion during the
        # provider call cannot recreate either a message or its embedding.
        entries = [(current_id, query_vector)]
        for index, row in enumerate(missing, 1):
            entries.append((row["id"], vectors[index]))
            saved[row["id"]] = vectors[index]
        store.save_embeddings(sid, model, entries)
        ranked = []
        for row in rows:
            if row["id"] not in saved:
                continue
            score = cosine(query_vector, saved[row["id"]])
            if score >= 0.25:
                ranked.append({**row, "semantic_score": round(score, 4)})
        ranked.sort(key=lambda r: (r["semantic_score"], r["seq"]), reverse=True)
        usage = getattr(response, "usage", None)
        stats.update(
            status="completed",
            indexed_candidates=sum(r["id"] in saved for r in rows),
            embedded_records=len(inputs),
            input_tokens=getattr(usage, "total_tokens", None),
        )
        return ranked[:6], {**stats, "seconds": time.monotonic() - started}
    except Exception:
        # Deliberately no raw provider errors, credentials, or retry waterfall.
        return [], {
            **stats,
            "status": "fallback",
            "seconds": time.monotonic() - started,
        }
