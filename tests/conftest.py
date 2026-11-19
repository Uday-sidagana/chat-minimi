from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from minimi.store import Store
from minimi.config import Settings
from minimi.server import create_app


class FakeAgent:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def events(self, packet, recent, policy):
        self.calls.append((packet, recent, policy))
        if policy == "required":
            yield {"type": "status", "phase": "searching", "text": "Searching the web"}
        yield {"type": "delta", "text": "A useful "}
        if self.fail:
            raise RuntimeError("upstream credential secret-must-not-leak")
        yield {"type": "delta", "text": "answer."}
        yield {
            "type": "result",
            "text": "A useful answer.",
            "citations": [],
            "searches": [{"status": "completed", "id": "search-1"}]
            if policy == "required"
            else [],
            "usage": {"total_tokens": 22},
            "model": "fake-test-model",
            "provider_status": "completed",
        }

    async def close(self):
        pass


@pytest.fixture
def clock():
    return [datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)]


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "memory.sqlite", clock=lambda: clock[0])


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def client(store, agent):
    with TestClient(
        create_app(Settings(api_key="not-a-real-key"), store=store, agent=agent)
    ) as c:
        c.get("/api/session")
        yield c
