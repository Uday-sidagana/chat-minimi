"""Real backend contract test; CI supplies a disposable Postgres service."""

import os
from uuid import uuid4
import pytest
from minimi.store import Store


def test_postgres_memory_is_durable_and_scoped():
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("Set POSTGRES_TEST_URL to run the real Postgres integration test.")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "minimi_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        db_url = make_conninfo(url, options=f"-c search_path={schema}")
        store = Store(url=db_url)
        sid, token = store.space()
        other, _ = store.space()
        chat = store.create_chat(sid)
        turn, _ = store.begin(
            sid,
            chat["id"],
            str(uuid4()),
            "Remember that the project is Amber Finch.",
            "UTC",
            "off",
        )
        store.finish(turn, "Understood.", {"source": "contract test"})
        reopened = Store(url=db_url)
        assert reopened.space(token) == (sid, None)
        records, total = reopened.recall(sid, "project")
        assert total == 1 and records[0]["content"].endswith("Amber Finch.")
        assert reopened.recall(other, "project")[1] == 0
        assert reopened.get_message(sid, turn["assistant_id"])["status"] == "completed"
        reopened.save_embeddings(
            sid,
            "test",
            [(turn["user_id"], [1.0, 0.0]), (turn["assistant_id"], [0.0, 1.0])],
        )
        assert reopened.embeddings(sid, "test") == {turn["user_id"]: [1.0, 0.0]}
        assert reopened.embeddings(other, "test") == {}
        reopened.forget(sid)
        reopened.save_embedding(sid, turn["user_id"], "test", [1.0, 0.0])
        assert reopened.embeddings(sid, "test") == {}
        assert reopened.chats(sid) == []
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
