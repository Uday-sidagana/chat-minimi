"""Small, reproducible live smoke evaluation. --live makes paid model/search calls."""

import argparse, asyncio, json, statistics, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from minimi.agent import Agent, search_policy, safe_url
from minimi.config import Settings
from minimi.memory import context
from minimi.store import Store

ROOT = Path(__file__).resolve().parents[1]


def seed(store, sid, cid, text, when):
    store.clock = lambda: when
    turn, _ = store.begin(sid, cid, str(uuid4()), text, "UTC", "off", max_daily=1000)
    store.finish(turn, "Acknowledged.", {})


async def main():
    settings = Settings.load()
    if not settings.api_key:
        raise SystemExit("Configure OPENAI_API_KEY in the ignored .env first.")
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    cases = [
        {
            "id": "direct-answer",
            "question": "What is 17 plus 25? Reply with the number.",
            "expected": "42",
            "history": [],
        },
        {
            "id": "cross-chat-memory",
            "question": "What is my project codename?",
            "expected": "Paper Kite",
            "history": [
                (yesterday, "Remember that my project codename is Paper Kite.")
            ],
        },
        {
            "id": "timestamp-recall",
            "question": f"What did I ask on {yesterday.date().isoformat()}, and when?",
            "expected": "Lisbon",
            "history": [
                (yesterday, "How can I get from Lisbon airport to the city center?")
            ],
        },
        {
            "id": "memory-correction",
            "question": "What is my project name now?",
            "expected": "Amber Finch",
            "history": [
                (yesterday, "Remember that my project is named Paper Kite."),
                (
                    now - timedelta(hours=1),
                    "Remember that I renamed my project to Amber Finch.",
                ),
            ],
        },
        {
            "id": "missing-memory",
            "question": "What birthday did I previously tell you for my mother?",
            "expected": None,
            "history": [],
        },
        {
            "id": "live-web",
            "question": "Find the current stable Python release on python.org. Cite the official release source and include its date.",
            "expected": None,
            "history": [],
        },
    ]
    results = []
    agent = Agent(settings)
    try:
        for case in cases:
            with tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "eval.sqlite", clock=lambda: now)
                sid, _ = store.space()
                old = store.create_chat(sid)
                for when, text in case["history"]:
                    seed(store, sid, old["id"], text, when)
                store.clock = lambda: now
                chat = store.create_chat(sid)
                store.begin(
                    sid,
                    chat["id"],
                    str(uuid4()),
                    case["question"],
                    "UTC",
                    "auto",
                    max_daily=1000,
                )
                packet, recent = context(
                    store, sid, chat["id"], case["question"], "UTC", now
                )
                started = time.monotonic()
                first = None
                result = None
                error = None
                try:
                    async with asyncio.timeout(110):
                        async for event in agent.events(
                            packet, recent, search_policy(case["question"], "auto")
                        ):
                            if event["type"] == "delta" and first is None:
                                first = time.monotonic() - started
                            if event["type"] == "result":
                                result = event
                except Exception as e:
                    error = type(e).__name__
                elapsed = time.monotonic() - started
                checks = {}
                if result:
                    if case["expected"]:
                        checks["expected_phrase_present"] = (
                            case["expected"].casefold() in result["text"].casefold()
                        )
                    if case["id"] == "timestamp-recall":
                        checks["recorded_date_present"] = (
                            yesterday.date().isoformat() in result["text"]
                            or yesterday.strftime("%B %-d") in result["text"]
                            or yesterday.strftime("%b %-d") in result["text"]
                        )
                    if case["id"] == "live-web":
                        checks["search_observed"] = any(
                            s["status"] == "completed" for s in result["searches"]
                        )
                        checks["official_citation_present"] = any(
                            safe_url(c["url"])
                            and (
                                ".python.org/" in c["url"]
                                or "://python.org/" in c["url"]
                            )
                            for c in result["citations"]
                        )
                    else:
                        checks["no_unnecessary_web_search"] = (
                            len(result["searches"]) == 0
                        )
                row = {
                    "id": case["id"],
                    "question": case["question"],
                    "status": "completed" if result else "failed",
                    "checks": checks,
                    "first_token_seconds": first,
                    "total_seconds": elapsed,
                    "search_calls": len(result["searches"]) if result else None,
                    "model": result["model"] if result else settings.model,
                    "usage": result["usage"] if result else {},
                    "answer": result["text"] if result else None,
                    "citations": result["citations"] if result else [],
                    "error_type": error,
                }
                results.append(row)
                print(
                    json.dumps(
                        {
                            "case": case["id"],
                            "status": row["status"],
                            "first_token_seconds": first,
                            "seconds": elapsed,
                            "checks": checks,
                        }
                    ),
                    flush=True,
                )
    finally:
        await agent.close()
    report = {
        "run_at_utc": now.isoformat(),
        "configuration": {
            "model": settings.model,
            "reasoning": settings.reasoning,
            "only_tool": "web_search",
            "max_tool_calls": 2,
            "max_output_tokens": 2400,
        },
        "notice": "Six authored smoke cases, one sample each. Phrase/source/date checks are not a general factual-accuracy or semantic-grounding benchmark. Missing-memory response requires manual review. Latency is local/provider dependent. Synthetic history is seeded only in isolated temporary evaluation databases; the shipped chat UI has no fabricated personal history.",
        "summary": {
            "completed": sum(r["status"] == "completed" for r in results),
            "cases": len(results),
            "mean_first_token_seconds": statistics.mean(
                r["first_token_seconds"]
                for r in results
                if r["first_token_seconds"] is not None
            ),
            "mean_total_seconds": statistics.mean(r["total_seconds"] for r in results),
            "all_automated_checks_passed": all(
                all(r["checks"].values()) and r["status"] == "completed"
                for r in results
            ),
        },
        "cases": results,
    }
    output = ROOT / "evaluation/live-results.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print("Saved evaluation/live-results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Allow paid model and search requests"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Use --live to explicitly enable paid requests; use pytest for free tests."
        )
    asyncio.run(main())
