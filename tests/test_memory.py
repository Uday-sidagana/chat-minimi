from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from minimi.memory import context, date_scope
from minimi.store import Store, Conflict, Limited


def seed(store, sid, cid, text, when=None, zone="UTC"):
    if when:
        store.clock = lambda: when
    turn, _ = store.begin(sid, cid, str(uuid4()), text, zone, "off", max_daily=1000)
    store.finish(turn, "Recorded.", {})
    return turn


@pytest.mark.parametrize(
    "query,start,end",
    [
        ("What did I ask yesterday?", "2026-11-18", "2026-11-19"),
        ("What did I ask last week?", "2026-11-09", "2026-11-16"),
        ("What did I ask this week?", "2026-11-16", "2026-11-20"),
        ("What did I ask last Tuesday?", "2026-11-17", "2026-11-18"),
        ("What did I ask on 2026-10-02?", "2026-10-02", "2026-10-03"),
        ("What did I ask in the last 3 days?", "2026-11-17", "2026-11-20"),
    ],
)
def test_calendar_windows(query, start, end, clock):
    scope = date_scope(query, clock[0], "UTC")
    assert (
        scope["start"].date().isoformat() == start
        and scope["end"].date().isoformat() == end
    )


def test_timezone_day_and_daylight_saving_are_not_fixed_24_hours():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    scope = date_scope("yesterday", now, "America/New_York")
    assert (scope["end"] - scope["start"]).total_seconds() == 25 * 3600
    scope = date_scope("today", now, "Asia/Kolkata")
    assert scope["start"].isoformat() == "2026-11-01T18:30:00+00:00"


@pytest.mark.parametrize(
    "q",
    ["2026-13-40", "2026-01-02 to 2026-01-03", "today and yesterday", "last 0 days"],
)
def test_ambiguous_or_invalid_dates_are_not_guessed(q, clock):
    assert date_scope(q, clock[0], "UTC") is None


def test_cross_chat_recall_uses_real_records_and_excludes_other_users(store, clock):
    sid, _ = store.space()
    other, _ = store.space()
    chat = store.create_chat(sid)
    foreign = store.create_chat(other)
    seed(
        store,
        sid,
        chat["id"],
        "I am building an observability project named Firefly.",
        clock[0] - timedelta(days=1),
    )
    seed(
        store,
        other,
        foreign["id"],
        "Secret Firefly budget is ten million.",
        clock[0] - timedelta(days=1),
    )
    store.clock = lambda: clock[0]
    new = store.create_chat(sid)
    seed(store, sid, new["id"], "What did I ask about Firefly yesterday?")
    packet, recent = context(
        store,
        sid,
        new["id"],
        "What did I ask about Firefly yesterday?",
        "Asia/Kolkata",
        clock[0],
    )
    assert len(packet["memory_records"]) == 1
    assert "observability" in packet["memory_records"][0]["content"]
    assert packet["memory_records"][0]["asked_at_utc"].startswith("2026-11-18")
    assert "Secret" not in str(packet)


def test_pinned_record_survives_bounded_recent_window(store, clock):
    sid, _ = store.space()
    chat = store.create_chat(sid)
    now = clock[0]
    first = seed(
        store,
        sid,
        chat["id"],
        "Remember that I prefer short practical explanations.",
        now - timedelta(hours=2),
    )
    for i in range(20):
        seed(
            store,
            sid,
            chat["id"],
            f"Trivia question number {i}",
            now - timedelta(minutes=100 - i * 3),
        )
    store.clock = lambda: now
    packet, recent = context(
        store, sid, chat["id"], "Explain how a web socket works.", "UTC", now
    )
    assert any(r["id"] == first["user_id"] for r in packet["memory_records"])
    assert len(recent) <= 15
    assert all("short practical" not in r["content"] for r in recent)


def test_memory_coverage_is_explicit_for_bounded_window(store, clock):
    sid, _ = store.space()
    chat = store.create_chat(sid)
    now = clock[0]
    for i in reversed(range(23)):
        seed(
            store,
            sid,
            chat["id"],
            f"History question {i}",
            now - timedelta(days=1, minutes=i * 2),
        )
    store.clock = lambda: now
    new = store.create_chat(sid)
    seed(store, sid, new["id"], "What did I ask yesterday?")
    packet, _ = context(store, sid, new["id"], "What did I ask yesterday?", "UTC", now)
    assert (
        packet["matched_record_count"] == 23
        and packet["matched_records_included"] == 20
    )


def test_reopen_preserves_workspace_token_and_original_timestamps(tmp_path, clock):
    path = tmp_path / "persist.sqlite"
    one = Store(path, clock=lambda: clock[0])
    sid, token = one.space()
    chat = one.create_chat(sid)
    turn = seed(one, sid, chat["id"], "What about Postgres?", zone="Asia/Kolkata")
    two = Store(path)
    same, newtoken = two.space(token)
    assert same == sid and newtoken is None
    record = two.get_message(sid, turn["user_id"])
    assert (
        record["created_at"].startswith("2026-11-19T12:00")
        and record["timezone"] == "Asia/Kolkata"
    )


def test_request_idempotency_conflicts_and_expired_lease(store, clock):
    sid, _ = store.space()
    chat = store.create_chat(sid)
    key = str(uuid4())
    a, created = store.begin(sid, chat["id"], key, "Hello", "UTC", "auto")
    assert created
    b, created = store.begin(sid, chat["id"], key, "Hello", "UTC", "auto")
    assert not created and a["id"] == b["id"]
    with pytest.raises(Conflict):
        store.begin(sid, chat["id"], key, "Different", "UTC", "auto")
    with pytest.raises(Conflict):
        store.begin(sid, chat["id"], str(uuid4()), "Another", "UTC", "auto")
    store.clock = lambda: clock[0] + timedelta(seconds=160)
    store.expire(sid, chat["id"])
    assert store.get_message(sid, a["assistant_id"])["status"] == "interrupted"


def test_daily_budget_is_global_and_not_reset_by_deleting_history(store, clock):
    sid, _ = store.space()
    chat = store.create_chat(sid)
    seed(store, sid, chat["id"], "One")
    store.forget(sid)
    other, _ = store.space()
    new = store.create_chat(other)
    with pytest.raises(Limited):
        store.begin(other, new["id"], str(uuid4()), "Two", "UTC", "auto", max_daily=1)


def test_clear_history_cannot_be_resurrected_by_late_completion(store):
    sid, _ = store.space()
    chat = store.create_chat(sid)
    turn, _ = store.begin(sid, chat["id"], str(uuid4()), "Hello", "UTC", "off")
    store.forget(sid)
    store.finish(turn, "Late answer", {})
    assert not store.chats(sid) and store.get_message(sid, turn["assistant_id"]) is None
