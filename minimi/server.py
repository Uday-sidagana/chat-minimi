import asyncio, base64, hmac, json, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .config import ROOT, Settings
from .store import Store, Conflict, Limited
from .memory import context, date_scope, record, recall_intent
from .semantic import retrieve as semantic_retrieve
from .agent import Agent, AgentError, search_policy, memory_references


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=12000)
    request_id: UUID
    timezone: str = Field(default="UTC", max_length=80)
    web: Literal["auto", "always", "off"] = "auto"

    @field_validator("text")
    @classmethod
    def not_blank(cls, v):
        if not v.strip():
            raise ValueError("Write a message first.")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone, such as Asia/Kolkata.")
        return v


class Pin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool


def event(kind, **data):
    return "data: " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n\n"


def create_app(settings=None, store=None, agent=None):
    config = settings or Settings.load()
    db = store
    brain = agent

    @asynccontextmanager
    async def lifespan(app):
        yield
        if brain and hasattr(brain, "close"):
            await brain.close()

    app = FastAPI(
        title="Chat Minimi",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    def storage():
        nonlocal db
        if db is None:
            if config.hosted and not config.database_url:
                raise HTTPException(
                    503,
                    "Connect a Postgres database and set DATABASE_URL for persistent hosted memory.",
                )
            try:
                db = Store(config.db_path, url=config.database_url)
            except Exception:
                raise HTTPException(
                    503,
                    "Conversation storage is unavailable. Check the database configuration.",
                ) from None
        return db

    def engine():
        nonlocal brain
        if brain is None:
            if not config.api_key:
                raise HTTPException(
                    503, "Add OPENAI_API_KEY to the server environment to enable chat."
                )
            brain = Agent(config)
        return brain

    def identity(request, response):
        sid, token = storage().space(request.cookies.get("minimi_workspace"))
        if token:
            response.set_cookie(
                "minimi_workspace",
                token,
                max_age=31536000,
                httponly=True,
                secure=config.hosted,
                samesite="lax",
                path="/",
            )
        return sid

    @app.middleware("http")
    async def protect(request, call_next):
        if config.demo_password and request.url.path != "/healthz":
            valid = False
            try:
                kind, encoded = request.headers.get("authorization", "").split(" ", 1)
                username, password = (
                    base64.b64decode(encoded, validate=True).decode().split(":", 1)
                )
                valid = (
                    kind.lower() == "basic"
                    and username == "reviewer"
                    and hmac.compare_digest(
                        password.encode(), config.demo_password.encode()
                    )
                )
            except (ValueError, UnicodeError):
                pass
            if not valid:
                return Response(
                    "Private Minimi demo",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Minimi"'},
                )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            from urllib.parse import urlsplit

            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse(
                    {"detail": "Cross-site writes are not allowed."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store, no-transform"
        return response

    @app.get("/healthz")
    def health():
        return {
            "status": "ok",
            "chat_configured": bool(config.api_key or agent),
            "storage": "postgres"
            if config.database_url
            else ("required" if config.hosted else "sqlite"),
            "model": config.model,
            "tools": ["web_search"],
        }

    @app.get("/api/session")
    def session(request: Request, response: Response):
        identity(request, response)
        return {
            "model": config.model,
            "configured": bool(config.api_key or agent),
            "tools": ["web_search"],
            "now_utc": storage().clock().isoformat(),
            "storage": "postgres" if config.database_url else "sqlite",
        }

    @app.get("/api/chats")
    def chats(request: Request, response: Response):
        return storage().chats(identity(request, response))

    @app.post("/api/chats", status_code=201)
    def new_chat(request: Request, response: Response):
        return storage().create_chat(identity(request, response))

    @app.get("/api/chats/{cid}")
    def conversation(
        cid: UUID, request: Request, response: Response, before: int | None = None
    ):
        sid = identity(request, response)
        chat = storage().chat(sid, str(cid))
        if not chat:
            raise HTTPException(404, "Conversation not found.")
        storage().expire(sid, str(cid))
        return {
            **chat,
            "messages": storage().messages(sid, str(cid), limit=100, before=before),
        }

    @app.get("/api/memory")
    def memory(
        request: Request,
        response: Response,
        q: str = "",
        day: str | None = None,
        timezone: str = "UTC",
    ):
        sid = identity(request, response)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Invalid timezone.") from None
        scope = date_scope(day or q, storage().clock(), timezone)
        if day and (not scope or scope["label"] != day):
            raise HTTPException(422, "Use a valid YYYY-MM-DD date.")
        records, total = storage().recall(
            sid,
            q,
            start=scope["start"] if scope else None,
            end=scope["end"] if scope else None,
            limit=50,
        )
        pins, pin_total = storage().recall(sid, pinned=True, limit=50)
        return {
            "records": [record(r, timezone) for r in records],
            "total": total,
            "pinned": [record(r, timezone) for r in pins],
            "pinned_total": pin_total,
            "timezone": timezone,
        }

    @app.patch("/api/messages/{mid}")
    def pin(mid: UUID, body: Pin, request: Request, response: Response):
        if not storage().pin(identity(request, response), str(mid), body.pinned):
            raise HTTPException(404, "User message not found.")
        return {"pinned": body.pinned}

    @app.delete("/api/data", status_code=204)
    def forget(request: Request, response: Response):
        storage().forget(identity(request, response))
        response.status_code = 204
        return response

    @app.get("/api/export")
    def export(request: Request, response: Response):
        sid = identity(request, response)

        def lines():
            for chat in storage().chats(sid, all_chats=True):
                # Pages are emitted newest first; seq supplies original chronology.
                before = None
                while True:
                    rows = storage().messages(sid, chat["id"], limit=100, before=before)
                    if not rows:
                        break
                    for row in rows:
                        yield (
                            json.dumps(
                                {"conversation": chat, **row}, ensure_ascii=False
                            )
                            + "\n"
                        )
                    before = rows[0]["seq"]

        return StreamingResponse(
            lines(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": 'attachment; filename="minimi-history.ndjson"'
            },
        )

    @app.post("/api/chats/{cid}/messages")
    async def send(cid: UUID, body: Send, request: Request, response: Response):
        model = engine()
        sid = identity(request, response)
        cid = str(cid)
        database = storage()
        try:
            turn, created = database.begin(
                sid,
                cid,
                str(body.request_id),
                body.text,
                body.timezone,
                body.web,
                config.max_daily_turns,
            )
        except KeyError:
            raise HTTPException(404, "Conversation not found.") from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        except Limited as e:
            raise HTTPException(429, str(e)) from None
        if not created and turn["status"] == "running":
            raise HTTPException(409, "This request is already generating a reply.")

        async def stream():
            yield event(
                "accepted",
                user_id=turn["user_id"],
                assistant_id=turn["assistant_id"],
                replayed=not created,
            )
            if not created:
                saved = database.get_message(sid, turn["assistant_id"])
                yield event("done", message=saved)
                return
            started = time.monotonic()
            first = None
            text = ""
            last_flush = started
            final = None
            metadata = {
                "search_policy": search_policy(body.text, body.web),
                "model": config.model,
            }
            iterator = None
            pending = None
            try:
                semantic = []
                retrieval = {"status": "disabled"}
                dated = recall_intent(body.text) and date_scope(
                    body.text, database.clock(), body.timezone
                )
                if config.semantic_memory and hasattr(model, "client") and not dated:
                    yield event(
                        "status", phase="memory", text="Finding relevant memories"
                    )
                    semantic, retrieval = await semantic_retrieve(
                        database,
                        sid,
                        turn["user_id"],
                        body.text,
                        model.client,
                        config.embedding_model,
                    )
                elif dated:
                    retrieval = {"status": "date_window"}
                packet, recent = context(
                    database,
                    sid,
                    cid,
                    body.text,
                    body.timezone,
                    database.clock(),
                    semantic=semantic,
                )
                metadata["search_policy"] = search_policy(body.text, body.web, recent)
                metadata["retrieval"] = retrieval
                selected = [*packet["memory_records"]]
                metadata.update(
                    {
                        "memory": selected,
                        "scope": packet["recall_scope"],
                        "recent_context_messages": len(recent),
                    }
                )
                yield event(
                    "memory",
                    records=selected,
                    scope=packet["recall_scope"],
                    total=packet["matched_record_count"],
                )
                iterator = model.events(
                    packet, recent, metadata["search_policy"]
                ).__aiter__()
                pending = asyncio.create_task(anext(iterator))
                async with asyncio.timeout(110):
                    while True:
                        ready, _ = await asyncio.wait({pending}, timeout=10)
                        if not ready:
                            yield ": keepalive\n\n"
                            continue
                        try:
                            item = pending.result()
                        except StopAsyncIteration:
                            break
                        if item["type"] == "delta":
                            if first is None:
                                first = time.monotonic() - started
                            text += item["text"]
                            yield event("delta", text=item["text"])
                            if time.monotonic() - last_flush > 1:
                                database.checkpoint(sid, turn["assistant_id"], text)
                                last_flush = time.monotonic()
                        elif item["type"] == "status":
                            yield event(
                                "status", text=item["text"], phase=item.get("phase")
                            )
                        elif item["type"] == "result":
                            final = item
                        pending = asyncio.create_task(anext(iterator))
                if not final:
                    raise AgentError(
                        "The answer stream ended before completion. Your question is saved."
                    )
                metadata.update(
                    {k: v for k, v in final.items() if k not in ("type", "text")}
                )
                metadata.update(
                    {
                        "first_token_seconds": first,
                        "total_seconds": time.monotonic() - started,
                        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                )
                clean, references = memory_references(final["text"], packet, recent)
                metadata["memory_citations"] = references
                database.finish(turn, clean, metadata)
                yield event(
                    "done", message=database.get_message(sid, turn["assistant_id"])
                )
            except asyncio.CancelledError:
                metadata["total_seconds"] = time.monotonic() - started
                database.finish(turn, text, metadata, "cancelled")
                raise
            except Exception as e:
                message = (
                    str(e)
                    if isinstance(e, AgentError)
                    else (
                        "The request timed out. Your question is saved."
                        if isinstance(e, TimeoutError)
                        else "The answer could not be completed. Check provider access or try again; your question is saved."
                    )
                )
                metadata.update(
                    {"error": message, "total_seconds": time.monotonic() - started}
                )
                database.finish(turn, text, metadata, "failed")
                yield event("error", message=message, message_id=turn["assistant_id"])
            finally:
                if pending and not pending.done():
                    pending.cancel()
                if pending:
                    try:
                        await pending
                    except (asyncio.CancelledError, StopAsyncIteration, Exception):
                        pass
                if iterator and hasattr(iterator, "aclose"):
                    await iterator.aclose()

        headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
        for key, value in response.headers.items():
            if key.lower() == "set-cookie":
                headers[key] = value
        return StreamingResponse(
            stream(), media_type="text/event-stream", headers=headers
        )

    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/index.html")

    app.state.storage = storage
    app.state.settings = config
    return app
