"""Date-aware retrieval is host-side context construction, not an agent tool."""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

WEEKDAYS = {
    day: i
    for i, day in enumerate(
        ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    )
}


def date_scope(query, now, zone):
    """Conservative supported windows. Return None rather than guess ambiguous ranges."""
    current = now.astimezone(ZoneInfo(zone))
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    text = query.casefold()
    start = end = None
    label = None
    relative = set(
        re.findall(
            r"\b(?:yesterday|today|last week|this week|last \d{1,2} days|last (?:"
            + "|".join(WEEKDAYS)
            + r"))\b",
            text,
        )
    )
    if len(relative) > 1:
        return None
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if len(dates) > 1 or (dates and relative):
        return None
    if dates:
        try:
            start = datetime.fromisoformat(dates[0]).replace(tzinfo=ZoneInfo(zone))
        except ValueError:
            return None
        end = start + timedelta(days=1)
        label = dates[0]
    elif "yesterday" in text:
        start = day - timedelta(days=1)
        end = day
        label = "yesterday"
    elif "today" in text:
        start = day
        end = day + timedelta(days=1)
        label = "today"
    elif "last week" in text:
        end = day - timedelta(days=day.weekday())
        start = end - timedelta(days=7)
        label = "last calendar week"
    elif "this week" in text:
        start = day - timedelta(days=day.weekday())
        end = day + timedelta(days=1)
        label = "this calendar week so far"
    elif match := re.search(r"\blast (\d{1,2}) days\b", text):
        days = int(match[1])
        if not 1 <= days <= 90:
            return None
        start = day - timedelta(days=days - 1)
        end = day + timedelta(days=1)
        label = f"last {days} calendar days including today"
    elif match := re.search(r"\blast (" + "|".join(WEEKDAYS) + r")\b", text):
        distance = (day.weekday() - WEEKDAYS[match[1]]) % 7 or 7
        start = day - timedelta(days=distance)
        end = start + timedelta(days=1)
        label = "last " + match[1]
    if start is None:
        return None
    return {
        "label": label,
        "start": start.astimezone(timezone.utc),
        "end": end.astimezone(timezone.utc),
        "timezone": zone,
    }


def recall_intent(text):
    return bool(
        re.search(
            r"\b(what did i|when did i|what have i|did i ask|i asked|i mentioned|i told you|previous (?:question|conversation)|my history|we discussed|we talk|remember|earlier|last time)\b",
            text,
            re.I,
        )
    )


def record(row, zone):
    when = datetime.fromisoformat(row["created_at"])
    body = row["content"]
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "role": row["role"],
        "content": body[:1600],
        "excerpt_truncated": len(body) > 1600,
        "asked_at_utc": row["created_at"],
        "asked_in_timezone": row["timezone"],
        "asked_at_local": when.astimezone(ZoneInfo(row["timezone"])).isoformat(),
        "display_time": when.astimezone(ZoneInfo(zone)).isoformat(),
        "pinned": row["pinned"],
    }


def context(store, sid, cid, text, zone, now, semantic=None):
    recent = store.messages(sid, cid, limit=15)
    # Exclude the newly created empty assistant placeholder and unsuccessful partial replies.
    recent = [r for r in recent if r["status"] == "completed"]
    current = next((r for r in reversed(recent) if r["role"] == "user"), None)
    current_id = current["id"] if current else None
    scope = date_scope(text, now, zone) if recall_intent(text) else None
    found, total = store.recall(
        sid,
        text,
        start=scope["start"] if scope else None,
        end=scope["end"] if scope else None,
        limit=20 if scope else 6,
        exclude=current_id,
    )
    pinned, _ = store.recall(sid, pinned=True, limit=6, exclude=current_id)
    fallback, _ = (
        store.recall(sid, limit=4, exclude=current_id) if not scope else ([], 0)
    )
    # Semantic candidates are re-authorized here. Even an accidental foreign
    # record supplied by a retriever cannot escape the workspace boundary.
    semantic_rows = []
    if not scope:
        for candidate in semantic or []:
            row = store.get_message(sid, candidate["id"])
            if row and row["role"] == "user" and row["status"] == "completed":
                semantic_rows.append(
                    {**row, "semantic_score": candidate.get("semantic_score")}
                )
    ranked = {}
    for reason, candidates in (("lexical", found), ("semantic", semantic_rows)):
        for rank, row in enumerate(candidates):
            item = ranked.setdefault(row["id"], {"row": row, "score": 0, "reasons": []})
            item["score"] += 1 / (60 + rank)
            item["reasons"].append(reason if not scope else "date_window")
    combined = sorted(
        ranked.values(), key=lambda x: (x["score"], x["row"]["seq"]), reverse=True
    )
    candidates = [(r["row"], r["reasons"]) for r in combined]
    candidates += [(r, ["pinned"]) for r in pinned]
    candidates += [(r, ["recent_history"]) for r in fallback]
    recent_ids = {r["id"] for r in recent}
    retrieved = []
    seen = set()
    remaining = 16000
    for row, reasons in candidates:
        if row["id"] in seen or row["id"] in recent_ids or row["id"] == current_id:
            continue
        length = min(len(row["content"]), 1600)
        if length > remaining:
            continue
        seen.add(row["id"])
        remaining -= length
        retrieved.append({**record(row, zone), "retrieval_reasons": reasons})
    # Reserve 20k characters for recent context, including the full latest input.
    budget = 36000 - sum(len(r["content"]) for r in retrieved)
    while len(recent) > 1 and sum(len(r["content"]) for r in recent) > budget:
        recent.pop(0)
    included = {r["id"] for r in recent} | seen
    date_mentioned = bool(
        re.search(
            r"\b(?:yesterday|today|last week|this week|last \d+ days|\d{4}-\d{2}-\d{2})\b",
            text,
            re.I,
        )
    )
    ambiguous = recall_intent(text) and date_mentioned and scope is None
    if ambiguous:
        # Do not let unrelated records tempt a confident dated answer when the
        # host could not resolve the requested range.
        retrieved = []
        recent = [r for r in recent if r["id"] == current_id]
        included = set()
    packet = {
        "current_user_message_id": current_id,
        "now_utc": now.astimezone(timezone.utc).isoformat(),
        "now_local": now.astimezone(ZoneInfo(zone)).isoformat(),
        "timezone": zone,
        "memory_records": retrieved,
        "date_window_status": "resolved"
        if scope
        else "ambiguous_or_unsupported"
        if recall_intent(text) and date_mentioned
        else "not_requested",
        "recent_message_times": [
            {
                "id": r["id"],
                "role": r["role"],
                "chat_id": r["chat_id"],
                "asked_at_utc": r["created_at"],
                "display_time": datetime.fromisoformat(r["created_at"])
                .astimezone(ZoneInfo(zone))
                .isoformat(),
            }
            for r in recent
        ],
        "recall_scope": {
            **scope,
            "start": scope["start"].isoformat(),
            "end": scope["end"].isoformat(),
        }
        if scope
        else None,
        "matched_record_count": total,
        "matched_records_included": sum(r["id"] in included for r in found),
        "coverage_note": "Complete transcript is stored, but this model sees a bounded recent window and selected historical records. Never imply exhaustive recall when matches exceed included records.",
    }
    counter = 1
    for item in packet["memory_records"]:
        item["reference"] = f"[M{counter}]"
        counter += 1
    for item in packet["recent_message_times"]:
        if item["role"] == "user" and item["id"] != current_id:
            item["reference"] = f"[M{counter}]"
            counter += 1
    return packet, recent
