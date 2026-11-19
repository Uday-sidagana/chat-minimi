# Minimi

A chat agent with **one tool—web search**—and a timestamped memory you can inspect.

**[Design and tradeoffs](design.md)** · [Live evaluation](evaluation/live-results.json) · [Browser checks](evaluation/browser-results.json)

![Minimi chat interface](docs/screenshot.png)

Minimi answers directly when it can, searches for changing facts, and streams its response. Web answers include clickable source citations. Each response has a details panel showing search activity, the model, timing, token usage and retrieved memory.

Conversations persist across reloads. Within the same browser workspace, the agent can recall earlier chats and answer questions such as “What did I ask yesterday?” using server-recorded timestamps and your selected timezone. A memory notebook lets you inspect questions, pin important context, filter by date, export history and delete it. Start a message with “Remember that…” to pin it automatically. Retrieval is bounded and lexical; this is not unlimited or semantic recall.

## Run locally

Requires Python 3.11+ (3.12 recommended) and an OpenAI API key with access to the Responses API and web search. API usage is billed separately from ChatGPT Plus.

```sh
git clone https://github.com/Uday-sidagana/chat-minimi.git
cd chat-minimi
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`, then:

```sh
minimi --port 8010
```

Open **http://127.0.0.1:8010**. The default model is `gpt-5.4-mini`, with reasoning effort `none`. Change `OPENAI_MODEL` and `REASONING_EFFORT` together if your model supports different settings. There is no simulated-answer fallback: a missing key or provider error is reported explicitly.

Local data is stored in `.data/minimi.sqlite`. `.env` and `.data/` are ignored by Git. No Node build, Docker or external database is required locally.

## Try it

1. Say: “Remember that my project is called Paper Kite.”
2. Start a new chat: “What is my project called, and when did I tell you?”
3. Ask: “Find the current stable Python release. Cite python.org and include its release date.” Inspect the sources and response details.
4. Ask: “What did I ask today?” Then open the memory notebook to check the original wording and timestamps.

The interface starts empty. Demonstration history is not planted in the application.

## Verification

```sh
pytest -q
# Optional: six real, paid model/search requests; overwrites the evaluation snapshot.
python scripts/evaluate.py --live
```

The committed live smoke run completed six authored cases: direct answer, cross-chat recall, timestamp recall, correction, missing memory and live web search. Median time to first text was **1.29 s**, mean **2.22 s**, slowest **5.44 s**. These are single local samples, not a general accuracy benchmark or latency guarantee. Full responses, checks and usage are in the evaluation JSON.

The browser check exercised real chat, cross-chat memory, reload persistence, workspace isolation, safe Markdown rendering, Unicode citations and a 390 px mobile layout, with no JavaScript errors. To repeat it with Node 20+ and the app running:

```sh
npm install --prefix runs/browser-check playwright@1.58.2
runs/browser-check/node_modules/.bin/playwright install chromium
node scripts/browser-check.cjs --live
```

GitHub Actions also runs a real Postgres persistence/isolation contract test. Locally, that test is skipped unless `POSTGRES_TEST_URL` points to a disposable database where the test may create and remove a schema. The remaining tests need no API key.

## Deploy on Vercel

Import this repository using the **FastAPI** preset, root directory `./`, with default build/output settings. `app.py` and `vercel.json` supply the entry point. Add:

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | Your API key |
| `DATABASE_URL` | A Postgres connection string, e.g. a Neon pooled URL with `sslmode=require` |
| `OPENAI_MODEL` | `gpt-5.4-mini` |
| `REASONING_EFFORT` | `none` |
| `DEMO_PASSWORD` | Optional shared reviewer password; username is `reviewer` |
| `MAX_DAILY_TURNS` | Optional global UTC-day budget, default `200` |

Hosted storage requires Postgres; the app refuses to store conversations in Vercel's temporary filesystem. Tables are created on first use. Keep this database separate from other projects. This repository includes deployment configuration, but does not claim a verified live deployment.

Memory is server-side, scoped to an opaque browser cookie; clearing that cookie loses access to its workspace. Selected history and the question are sent to OpenAI. Use the notebook to export or delete application history. See [design.md](design.md) for privacy, limits and failure handling.
