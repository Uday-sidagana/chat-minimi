"""Append-only timestamped transcript. Every read is scoped to an opaque workspace."""

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib, json, re, secrets, sqlite3
from pathlib import Path
from uuid import uuid4


def stamp(value=None):
    return (
        (value or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Conflict(ValueError):
    pass


class Limited(ValueError):
    pass


class Connection:
    def __init__(self, db, postgres=False):
        self.db, self.postgres = db, postgres

    def execute(self, sql, args=()):
        if self.postgres:
            if sql == "BEGIN IMMEDIATE":
                return self.db.execute("SELECT pg_advisory_xact_lock(873014290)")
            sql = sql.replace("?", "%s")
        return self.db.execute(sql, args)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mm_spaces (id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mm_chats (id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES mm_spaces(id),title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mm_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,chat_id TEXT NOT NULL REFERENCES mm_chats(id) ON DELETE CASCADE,space_id TEXT NOT NULL REFERENCES mm_spaces(id),role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL,timezone TEXT NOT NULL,status TEXT NOT NULL,pinned INTEGER NOT NULL DEFAULT 0,metadata TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS mm_requests (id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES mm_spaces(id),chat_id TEXT NOT NULL REFERENCES mm_chats(id) ON DELETE CASCADE,request_key TEXT NOT NULL,fingerprint TEXT NOT NULL,user_id TEXT NOT NULL,assistant_id TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,lease_until TEXT NOT NULL,UNIQUE(space_id,request_key))""",
    """CREATE TABLE IF NOT EXISTS mm_daily (day TEXT PRIMARY KEY,turns INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS mm_message_space_time ON mm_messages(space_id,created_at,seq)",
    "CREATE INDEX IF NOT EXISTS mm_message_chat_seq ON mm_messages(chat_id,seq)",
]


class Store:
    def __init__(self, path: Path | None = None, url="", clock=None):
        self.path = Path(path) if path else None
        self.url = url
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not url:
            self.path = self.path or Path(".data/minimi.sqlite")
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for query in SCHEMA:
                db.execute(
                    query.replace(
                        "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
                    )
                    if url
                    else query
                )

    @contextmanager
    def connect(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(
                self.url,
                connect_timeout=8,
                row_factory=dict_row,
                prepare_threshold=None,
            ) as db:
                db.execute("SET LOCAL statement_timeout='10s'")
                db.execute("SET LOCAL lock_timeout='5s'")
                yield Connection(db, True)
        else:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                with db:
                    yield Connection(db)
            finally:
                db.close()

    def space(self, token=None):
        with self.connect() as db:
            if token and len(token) <= 100:
                row = db.execute(
                    "SELECT id FROM mm_spaces WHERE token_hash=?", (digest(token),)
                ).fetchone()
                if row:
                    return row["id"], None
            token = secrets.token_urlsafe(32)
            sid = str(uuid4())
            db.execute(
                "INSERT INTO mm_spaces(id,token_hash,created_at) VALUES(?,?,?)",
                (sid, digest(token), stamp(self.clock())),
            )
            return sid, token

    def chats(self, sid):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,title,created_at,updated_at FROM mm_chats WHERE space_id=? ORDER BY updated_at DESC LIMIT 100",
                    (sid,),
                )
            ]

    def create_chat(self, sid):
        cid = str(uuid4())
        now = stamp(self.clock())
        with self.connect() as db:
            db.execute(
                "INSERT INTO mm_chats VALUES(?,?,?,?,?)",
                (cid, sid, "New conversation", now, now),
            )
        return {"id": cid, "title": "New conversation", "created_at": now}

    def chat(self, sid, cid):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM mm_chats WHERE id=? AND space_id=?", (cid, sid)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def message(row):
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"])
        item["pinned"] = bool(item["pinned"])
        return item

    def messages(self, sid, cid, limit=100, before=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM mm_messages WHERE space_id=? AND chat_id=?"
                + (" AND seq<?" if before else "")
                + " ORDER BY seq DESC LIMIT ?",
                (sid, cid) + ((before,) if before else ()) + (limit,),
            ).fetchall()
        return [self.message(r) for r in reversed(rows)]

    def get_message(self, sid, mid):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM mm_messages WHERE id=? AND space_id=?", (mid, sid)
            ).fetchone()
        return self.message(row) if row else None

    def begin(self, sid, cid, key, text, zone, mode, max_daily=200):
        fingerprint = digest(json.dumps([cid, text, zone, mode]))
        now = self.clock()
        current = stamp(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM mm_requests WHERE space_id=? AND request_key=?",
                (sid, key),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("This request key was used for different content.")
                if old["status"] == "running" and old["lease_until"] < current:
                    db.execute(
                        "UPDATE mm_requests SET status='interrupted' WHERE id=?",
                        (old["id"],),
                    )
                    db.execute(
                        "UPDATE mm_messages SET status='interrupted' WHERE id=?",
                        (old["assistant_id"],),
                    )
                    return dict(old, status="interrupted"), False
                return dict(old), False
            chat = db.execute(
                "SELECT * FROM mm_chats WHERE id=? AND space_id=?", (cid, sid)
            ).fetchone()
            if not chat:
                raise KeyError(cid)
            running = db.execute(
                "SELECT id FROM mm_requests WHERE space_id=? AND chat_id=? AND status='running' AND lease_until>?",
                (sid, cid, current),
            ).fetchone()
            if running:
                raise Conflict("This conversation already has a reply in progress.")
            count = db.execute(
                "SELECT turns FROM mm_daily WHERE day=?", (current[:10],)
            ).fetchone()
            if count and count["turns"] >= max_daily:
                raise Limited(
                    "The demo has reached its daily request budget. Please try again tomorrow."
                )
            recent = db.execute(
                "SELECT COUNT(*) AS count FROM mm_requests WHERE space_id=? AND created_at>?",
                (sid, stamp(now - timedelta(minutes=1))),
            ).fetchone()["count"]
            active = db.execute(
                "SELECT COUNT(*) AS count FROM mm_requests WHERE status='running' AND lease_until>?",
                (current,),
            ).fetchone()["count"]
            if recent >= 12 or active >= 4:
                raise Limited("Too many requests right now. Please wait a moment.")
            rid, uid, aid = [str(uuid4()) for _ in range(3)]
            pinned = bool(
                re.match(
                    r"^\s*(?:please\s+)?remember\s+(?:that\b|this\b|my\b|i\b)",
                    text,
                    re.I,
                )
            )
            for mid, role, body, status, pin in [
                (uid, "user", text, "completed", int(pinned)),
                (aid, "assistant", "", "streaming", 0),
            ]:
                db.execute(
                    "INSERT INTO mm_messages(id,chat_id,space_id,role,content,created_at,timezone,status,pinned) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mid, cid, sid, role, body, current, zone, status, pin),
                )
            db.execute(
                "INSERT INTO mm_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    sid,
                    cid,
                    key,
                    fingerprint,
                    uid,
                    aid,
                    "running",
                    current,
                    stamp(now + timedelta(seconds=150)),
                ),
            )
            db.execute(
                "INSERT INTO mm_daily(day,turns) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET turns=mm_daily.turns+1",
                (current[:10],),
            )
            title = (
                text.strip().split("\n")[0][:65]
                if chat["title"] == "New conversation"
                else chat["title"]
            )
            db.execute(
                "UPDATE mm_chats SET title=?,updated_at=? WHERE id=?",
                (title, current, cid),
            )
        return {
            "id": rid,
            "space_id": sid,
            "chat_id": cid,
            "user_id": uid,
            "assistant_id": aid,
            "status": "running",
        }, True

    def expire(self, sid, cid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,assistant_id FROM mm_requests WHERE space_id=? AND chat_id=? AND status='running' AND lease_until<?",
                (sid, cid, stamp(self.clock())),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE mm_requests SET status='interrupted' WHERE id=?",
                    (row["id"],),
                )
                db.execute(
                    "UPDATE mm_messages SET status='interrupted' WHERE id=? AND status='streaming'",
                    (row["assistant_id"],),
                )

    def checkpoint(self, sid, aid, text):
        with self.connect() as db:
            db.execute(
                "UPDATE mm_messages SET content=? WHERE id=? AND space_id=? AND status='streaming'",
                (text, aid, sid),
            )

    def finish(self, request, text, metadata, status="completed"):
        with self.connect() as db:
            db.execute(
                "UPDATE mm_messages SET content=?,metadata=?,status=? WHERE id=? AND space_id=? AND status='streaming'",
                (
                    text,
                    json.dumps(metadata),
                    status,
                    request["assistant_id"],
                    request["space_id"],
                ),
            )
            db.execute(
                "UPDATE mm_requests SET status=? WHERE id=? AND status='running'",
                (status, request["id"]),
            )

    def pin(self, sid, mid, value):
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE mm_messages SET pinned=? WHERE id=? AND space_id=? AND role='user'",
                    (int(value), mid, sid),
                ).rowcount
            )

    def forget(self, sid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM mm_chats WHERE space_id=?", (sid,))

    def recall(
        self, sid, query="", start=None, end=None, limit=20, pinned=False, exclude=None
    ):
        args = [sid]
        where = "space_id=? AND role='user' AND status='completed'"
        if start:
            where += " AND created_at>=?"
            args.append(stamp(start))
        if end:
            where += " AND created_at<?"
            args.append(stamp(end))
        if pinned:
            where += " AND pinned=1"
        if exclude:
            where += " AND id<>?"
            args.append(exclude)
        # Bound lexical query work and bind every user value. No model/tool round trip.
        stop = {
            "what",
            "when",
            "where",
            "which",
            "have",
            "about",
            "that",
            "this",
            "with",
            "from",
            "please",
            "remember",
            "asked",
            "ask",
            "did",
            "you",
            "the",
            "and",
            "for",
            "was",
            "were",
            "today",
            "yesterday",
            "last",
            "week",
            "time",
            "told",
            "say",
            "said",
            "mentioned",
            "discuss",
            "discussed",
            "earlier",
            "before",
            "me",
            "my",
            "can",
            "could",
            "would",
            "how",
            "i",
        }
        words = list(
            dict.fromkeys(
                w.casefold()
                for w in re.findall(r"[\w]+", query)
                if len(w) > 2 and w.casefold() not in stop
            )
        )[:8]
        if words and not (start or end or pinned):
            where += (
                " AND (" + " OR ".join("LOWER(content) LIKE ?" for _ in words) + ")"
            )
            args.extend("%" + w.replace("%", "").replace("_", "") + "%" for w in words)
        order = " ORDER BY seq DESC"
        ranking = []
        if words and not (start or end or pinned):
            order = (
                " ORDER BY ("
                + "+".join(
                    "CASE WHEN LOWER(content) LIKE ? THEN 1 ELSE 0 END" for _ in words
                )
                + ") DESC,seq DESC"
            )
            ranking = ["%" + w.replace("%", "").replace("_", "") + "%" for w in words]
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) AS count FROM mm_messages WHERE " + where, tuple(args)
            ).fetchone()["count"]
            rows = db.execute(
                "SELECT * FROM mm_messages WHERE " + where + order + " LIMIT ?",
                tuple(args) + tuple(ranking) + (limit,),
            ).fetchall()
        records = [self.message(r) for r in rows]
        if words and not (start or end or pinned):
            records.sort(
                key=lambda r: (
                    sum(w in r["content"].casefold() for w in words),
                    r["seq"],
                ),
                reverse=True,
            )
        return records, total
