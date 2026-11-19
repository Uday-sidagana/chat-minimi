import asyncio
from types import SimpleNamespace
import pytest
from minimi.agent import Agent, extract, search_policy, AgentError
from minimi.config import Settings


@pytest.mark.parametrize(
    "text,mode,expected",
    [
        ("Hello", "auto", "auto"),
        ("Latest Python release?", "auto", "required"),
        ("What did I ask yesterday?", "auto", "none"),
        ("What did I ask today?", "auto", "none"),
        ("What did I ask and what is the latest version?", "auto", "required"),
        ("Who won?", "always", "required"),
        ("Latest news", "off", "none"),
    ],
)
def test_search_is_targeted_not_unconditionally_paid(text, mode, expected):
    assert search_policy(text, mode) == expected


def test_exactly_one_model_tool_and_bounded_request():
    agent = Agent(Settings(api_key="fake"), client=object())
    r = agent.request(
        {"timezone": "UTC"}, [{"role": "user", "content": "Hello"}], "none"
    )
    assert [t["type"] for t in r["tools"]] == ["web_search"]
    assert r["max_tool_calls"] == 2 and r["store"] is False
    assert "fake" not in str(r)


def test_citations_come_from_provider_annotations_and_unsafe_urls_are_dropped():
    text = "🌱 A factual statement [1]"
    start = text.index("[1]")
    r = extract(
        {
            "model": "test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/a",
                                    "title": "Source",
                                    "start_index": start,
                                    "end_index": len(text),
                                },
                                {
                                    "type": "url_citation",
                                    "url": "javascript:alert(1)",
                                    "title": "Bad",
                                    "start_index": 0,
                                    "end_index": 1,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    )
    assert (
        len(r["citations"]) == 1
        and r["text"][r["citations"][0]["start"] : r["citations"][0]["end"]] == "[1]"
    )


class Stream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self.events()

    async def events(self):
        yield SimpleNamespace(
            type="response.output_text.delta", delta="Unverified claim"
        )
        yield SimpleNamespace(
            type="response.completed",
            response={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Unverified claim",
                                "annotations": [],
                            }
                        ],
                    }
                ]
            },
        )


def test_required_search_without_observed_tool_call_is_not_completed():
    client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **kwargs: Stream())
    )
    agent = Agent(Settings(api_key="fake"), client=client)

    async def collect():
        return [
            x
            async for x in agent.events(
                {}, [{"role": "user", "content": "Latest news"}], "required"
            )
        ]

    with pytest.raises(AgentError, match="web check did not complete"):
        asyncio.run(collect())


def test_personal_history_is_not_forced_into_web_search():
    assert search_policy("What did I ask you yesterday?", "always") == "none"


def test_web_followup_inherits_freshness_but_personal_recall_stays_private():
    recent = [
        {"role": "assistant", "metadata": {"searches": [{"status": "completed"}]}}
    ]
    assert search_policy("What about India?", "auto", recent) == "required"
    assert search_policy("Has that changed?", "auto", recent) == "required"
    assert search_policy("What did I ask yesterday?", "auto", recent) == "none"
    assert search_policy("What about India?", "off", recent) == "none"
    assert (
        search_policy(
            "What about India?", "auto", [{"role": "assistant", "metadata": {}}]
        )
        == "auto"
    )


def test_memory_references_allow_only_supplied_original_user_messages(store, clock):
    from minimi.agent import memory_references
    from minimi.memory import context
    from test_memory import seed

    sid, _ = store.space()
    old = store.create_chat(sid)
    first = seed(store, sid, old["id"], "Remember that I prefer tea.")
    new = store.create_chat(sid)
    turn = seed(store, sid, new["id"], "What do I prefer?")
    packet, recent = context(
        store, sid, new["id"], "What do I prefer?", "UTC", clock[0]
    )
    text = f"Tea [M:{first['user_id']}] 🌱 [M:{turn['user_id']}] [M:{first['assistant_id']}] [M:forged] [1]"
    clean, refs = memory_references(text, packet, recent)
    assert len(refs) == 1 and refs[0]["id"] == first["user_id"]
    assert clean[refs[0]["start"] : refs[0]["end"]] == f"[M:{first['user_id']}]"
    assert "forged" not in clean and first["assistant_id"] not in clean
    assert len(clean) == len(text) and clean.index("[1]") == text.index("[1]")


def test_short_memory_reference_tokens_resolve_without_invented_sources(store, clock):
    from minimi.agent import memory_references
    from minimi.memory import context
    from test_memory import seed

    sid, _ = store.space()
    old = store.create_chat(sid)
    seed(store, sid, old["id"], "Remember that the answer is tea.")
    new = store.create_chat(sid)
    seed(store, sid, new["id"], "What is my drink?")
    packet, recent = context(
        store, sid, new["id"], "What is my drink?", "UTC", clock[0]
    )
    assert packet["memory_records"][0]["reference"] == "[M1]"
    clean, refs = memory_references("Tea [M1] [M999]", packet, recent)
    assert len(refs) == 1 and clean[refs[0]["start"] : refs[0]["end"]] == "[M1]"
    assert "M999" not in clean


def test_required_web_checks_have_verification_budget():
    agent = Agent(Settings(api_key="fake"), client=object())
    r = agent.request(
        {"timezone": "UTC"},
        [{"role": "user", "content": "Latest Python version?"}],
        "required",
    )
    assert r["max_tool_calls"] == 4
    assert r["reasoning"]["effort"] == "low"
    assert [t["type"] for t in r["tools"]] == ["web_search"]
