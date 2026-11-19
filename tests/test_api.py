import json
from uuid import uuid4
from fastapi.testclient import TestClient
from minimi.server import create_app
from minimi.config import Settings
from conftest import FakeAgent


def frames(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def chat(client):
    return client.post("/api/chats").json()["id"]


def payload(**changes):
    return {
        "text": "Explain something clearly.",
        "request_id": str(uuid4()),
        "timezone": "Asia/Kolkata",
        "web": "auto",
        **changes,
    }


def test_stream_records_timing_and_only_one_tool(client, agent):
    cid = chat(client)
    response = client.post("/api/chats/" + cid + "/messages", json=payload())
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "text/event-stream"
    )
    events = frames(response)
    assert events[0]["type"] == "accepted" and events[-1]["type"] == "done"
    result = events[-1]["message"]
    assert result["content"] == "A useful answer." and result["status"] == "completed"
    assert result["metadata"]["first_token_seconds"] >= 0 and len(agent.calls) == 1
    assert client.get("/healthz").json()["tools"] == ["web_search"]
    stored = client.get("/api/chats/" + cid).json()["messages"]
    assert len(stored) == 2
    assert stored[0]["timezone"] == "Asia/Kolkata"


def test_identical_request_replays_without_second_model_call(client, agent):
    cid = chat(client)
    body = payload()
    one = client.post("/api/chats/" + cid + "/messages", json=body)
    two = client.post("/api/chats/" + cid + "/messages", json=body)
    assert frames(two)[0]["replayed"] and len(agent.calls) == 1
    assert frames(one)[-1]["message"]["id"] == frames(two)[-1]["message"]["id"]
    assert (
        client.post(
            "/api/chats/" + cid + "/messages", json={**body, "text": "different"}
        ).status_code
        == 409
    )


def test_browser_workspaces_are_isolated(client, store):
    cid = chat(client)
    client.post(
        "/api/chats/" + cid + "/messages",
        json=payload(text="A private birthday surprise"),
    )
    with TestClient(
        create_app(Settings(api_key="fake"), store=store, agent=FakeAgent())
    ) as other:
        other.get("/api/session")
        assert other.get("/api/chats/" + cid).status_code == 404
        assert other.get("/api/memory?q=private").json()["total"] == 0
        assert (
            other.post("/api/chats/" + cid + "/messages", json=payload()).status_code
            == 404
        )


def test_invalid_timezone_spoofed_timestamp_and_blank_input_are_rejected(client, agent):
    cid = chat(client)
    for body in [
        payload(timezone="../../etc/passwd"),
        payload(text="  "),
        payload(created_at="1999-01-01"),
        payload(web="shell"),
    ]:
        assert (
            client.post("/api/chats/" + cid + "/messages", json=body).status_code == 422
        )
    assert not agent.calls


def test_failed_model_stream_preserves_question_and_partial_without_secret_leak(store):
    agent = FakeAgent(fail=True)
    with TestClient(
        create_app(Settings(api_key="fake"), store=store, agent=agent)
    ) as client:
        cid = chat(client)
        response = client.post("/api/chats/" + cid + "/messages", json=payload())
        assert frames(response)[-1]["type"] == "error"
        assert "secret-must-not-leak" not in response.text
        rows = client.get("/api/chats/" + cid).json()["messages"]
        assert rows[-1]["status"] == "failed" and rows[-1]["content"] == "A useful "
        assert rows[0]["content"] == "Explain something clearly."


def test_pin_export_and_delete_are_real_storage_actions(client):
    cid = chat(client)
    data = frames(client.post("/api/chats/" + cid + "/messages", json=payload()))
    uid = data[0]["user_id"]
    assert client.patch("/api/messages/" + uid, json={"pinned": True}).json()["pinned"]
    assert client.get("/api/memory").json()["pinned_total"] == 1
    assert "Explain something clearly." in client.get("/api/export").text
    assert client.delete("/api/data").status_code == 204
    assert (
        client.get("/api/chats").json() == []
        and client.get("/api/memory").json()["total"] == 0
    )


def test_cross_site_writes_rejected_before_model_call(client, agent):
    cid = chat(client)
    assert (
        client.post(
            "/api/chats/" + cid + "/messages",
            json=payload(),
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert not agent.calls


def test_unconfigured_chat_is_honest_and_does_not_fake_answers(store):
    with TestClient(create_app(Settings(), store=store)) as client:
        cid = chat(client)
        r = client.post("/api/chats/" + cid + "/messages", json=payload())
        assert r.status_code == 503 and "OPENAI_API_KEY" in r.text
        assert client.get("/api/chats/" + cid).json()["messages"] == []


def test_optional_private_demo_password(store):
    with TestClient(
        create_app(Settings(demo_password="a-private-example-password"), store=store)
    ) as client:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        assert (
            client.get("/", auth=("reviewer", "a-private-example-password")).status_code
            == 200
        )


def test_cookie_is_httponly_and_security_headers_present(client):
    response = client.get("/")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.cookies.get("minimi_workspace")
    with TestClient(create_app(Settings(), store=client.app.state.storage())) as other:
        r = other.get("/api/session")
        assert "httponly" in r.headers["set-cookie"].lower()
