# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""Rafeeq as an HTTP service with a human approval queue (based on servers/rafeeq_api.py, Lab 4.4).

    RAFEEQ_API_KEYS=dev-key-1 uv run my_capstone/app.py --port 8793      # then open http://127.0.0.1:8793/

    curl -s localhost:8793/v1/chat -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" \
         -d '{"session_id": "s1", "message": "Book inspection ... I confirm. ID ends 6654, mobile 0532236654"}'
    curl -s localhost:8793/v1/pending -H "X-API-Key: dev-key-1"
    curl -s localhost:8793/v1/pending/1/decide -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" \
         -d '{"decision": "approve"}'

What it adds around rafeeq.py's agent (same spec, tools and guard):
- an API key per client (X-API-Key); approvals can be limited to officer keys (RAFEEQ_OFFICER_KEYS);
- sessions in SQLite, a token budget per session (HTTP 429), the step and cost budgets of rafeeq.py;
- least privilege: read tools run at once; write tools are NEVER run inside a chat turn. They are queued in the table
  `pending` and the model is told "waiting for approval". A human decides through POST /v1/pending/{id}/decide:
  approve runs the stored tool call, reject does not. Both are logged (table + audit.jsonl);
- the egress guard of rafeeq.py (send_email only to @madar.gov) is checked when the call is made AND again when it is
  approved, so a stale or tampered queue row cannot send mail either;
- one shared database: the MCP server and the write tools use the same file, so a booking you approve shows up in the
  next get_permit;
- an audit log without personal data (mobile numbers are masked) and one trace line per chat turn.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
LABS = HERE.parent
sys.path.insert(0, str(LABS))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from mcp import StdioServerParameters  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agentkit as K  # noqa: E402
import madar  # noqa: E402
import rafeeq  # noqa: E402

API_KEYS = {k.strip() for k in os.getenv("RAFEEQ_API_KEYS", "dev-key-1").split(",") if k.strip()}
OFFICER_KEYS = {k.strip() for k in os.getenv("RAFEEQ_OFFICER_KEYS", "").split(",") if k.strip()} or API_KEYS
SESSION_TOKENS = int(os.getenv("RAFEEQ_SESSION_TOKENS", "40000"))
CONFIG = os.getenv("RAFEEQ_CONFIG", "B")
SECOND_MODEL = os.getenv("RAFEEQ_SECOND_MODEL", K.FALLBACKS[0] if K.FALLBACKS else K.MODEL)


# ---------------------------------------------------------------- small helpers
def who(key: str) -> str:
    """A label for logs: never the key itself."""
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:8]


def masked(args: dict) -> dict:
    """Data minimisation: show a mobile number only by its last 4 digits (the stored call keeps the full value)."""
    return {k: ("******" + re.sub(r"\D", "", str(v))[-4:] if k == "mobile" else v) for k, v in args.items()}


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


# ---------------------------------------------------------------- API models
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class PendingOut(BaseModel):
    id: int
    session_id: str
    tool: str
    args: dict
    status: str  # pending | approved | rejected | failed | blocked | expired
    created: str | None
    decided_at: str | None = None
    decided_by: str | None = None
    reason: str | None = None
    result: dict | None = None


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tools: list[str]
    pending_actions: list[PendingOut]
    blocked: list[dict]
    tokens: dict
    model_steps: int


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=500)


# ---------------------------------------------------------------- the service
def create_app(workdir: Path | None = None, use_mcp: bool = True, shape="chat") -> FastAPI:
    """`use_mcp=False` and a custom `shape` exist for tests without a model or a subprocess."""
    workdir = workdir or K.lab_dir("rafeeq_app")
    m = madar.Madar(workdir, actor="rafeeq-api")  # a fresh copy of the database at every start
    db_path = workdir / "sessions.sqlite"
    audit_path, trace_path = workdir / "audit.jsonl", workdir / "traces.jsonl"
    config = rafeeq.CONFIGS[CONFIG]
    lock = threading.Lock()  # one agent run or tool execution at a time: K.LEDGER is process-wide (fine for a lab)
    state: dict = {"bridge": None}

    @contextmanager
    def conn():
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def log(path: Path, event: str, **data) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data},
                               ensure_ascii=False, default=str) + "\n")

    with conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, history TEXT, tokens INTEGER, updated REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, owner TEXT, "
                    "tool TEXT, args TEXT, status TEXT, created REAL, decided_at REAL, decided_by TEXT, reason TEXT, result TEXT)")
        # the database was just reset, so requests queued before this start no longer match it
        stale = con.execute("UPDATE pending SET status = 'expired' WHERE status IN ('pending', 'deciding')").rowcount
    if stale:
        log(audit_path, "pending_expired_on_start", count=stale)

    def view(row: sqlite3.Row) -> PendingOut:
        result = json.loads(row["result"]) if row["result"] else None
        return PendingOut(id=row["id"], session_id=row["session_id"], tool=row["tool"], args=masked(json.loads(row["args"])),
                          status=row["status"], created=iso(row["created"]), decided_at=iso(row["decided_at"]),
                          decided_by=row["decided_by"], reason=row["reason"] or None, result=result)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not use_mcp:
            yield
            return
        # the MCP server gets the same workdir, so reads (get_permit) and approved writes see one database
        server = StdioServerParameters(command=sys.executable, args=[str(LABS / "servers" / "madar_mcp.py"), "--workdir", str(workdir)])
        with K.MCPBridge(server) as bridge:
            state["bridge"] = bridge
            yield

    app = FastAPI(title="Rafeeq API", version="1.0.0", lifespan=lifespan)

    def api_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in API_KEYS | OFFICER_KEYS:
            raise HTTPException(401, "Missing or invalid X-API-Key")
        return x_api_key

    def officer_key(key: str = Depends(api_key)) -> str:
        if key not in OFFICER_KEYS:
            raise HTTPException(403, "This key may chat but not review pending actions")
        return key

    def read_tools() -> list:
        return state["bridge"].agent_tools() if state["bridge"] else m.tools(m.READ_TOOLS)

    # -------------------------------------------------------------- routes
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "model": K.MODEL, "config": CONFIG, "mcp": bool(state["bridge"])}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        page = HERE / "index.html"
        return page.read_text(encoding="utf-8") if page.exists() else "<p>index.html not found next to app.py</p>"

    @app.post("/v1/chat", response_model=ChatOut)
    def chat(req: ChatIn, key: str = Depends(api_key)) -> ChatOut:
        sid = f"{key}:{req.session_id}"
        with conn() as con:
            row = con.execute("SELECT history, tokens FROM sessions WHERE id = ?", (sid,)).fetchone()
        history, spent = (json.loads(row["history"]), row["tokens"]) if row else ([], 0)
        if spent >= SESSION_TOKENS:
            raise HTTPException(429, f"Session budget of {SESSION_TOKENS} tokens used up. Start a new session.")
        queued: list[int] = []
        blocked: list[dict] = []

        def permission(name: str, args: dict) -> str:
            decision = rafeeq.permission(name, args)  # the egress guard lives there: "deny" for a forbidden recipient
            if decision == "deny":
                blocked.append({"tool": name, "reason": rafeeq.egress_blocked(name, args)})
            # rafeeq.py asks a human for writes; here the queue below is that human step, so the loop must not prompt
            return "allow" if decision == "ask" else decision

        def queue_write(name: str, args: dict) -> str | None:
            """before_tool: a write tool does not run in the chat turn; it becomes a pending row."""
            if name not in rafeeq.WRITE_TOOLS:
                return None
            try:  # a malformed call would only fail after a human approved it, so refuse it now
                inspect.signature(getattr(m, name)).bind(**args)
            except TypeError as e:
                return json.dumps({"error": f"Bad arguments for {name}: {e}"})
            with conn() as con:
                pid = con.execute("INSERT INTO pending (session_id, owner, tool, args, status, created) VALUES (?,?,?,?,?,?)",
                                  (req.session_id, who(key), name, json.dumps(args, ensure_ascii=False), "pending", time.time())).lastrowid
            queued.append(pid)
            log(audit_path, "pending_created", id=pid, session=req.session_id, tool=name, args=masked(args))
            return json.dumps({"status": "pending_approval", "pending_id": pid,
                               "note": "NOT done yet. It is queued for a human approval. Tell the person it is waiting for approval."})

        started = time.perf_counter()
        with lock:
            before = (K.LEDGER.input_tokens, K.LEDGER.output_tokens)
            kw = dict(system=config["system"], shape=shape, history=rafeeq.compact(history), max_steps=rafeeq.MAX_STEPS,
                      max_cost=rafeeq.MAX_COST, permission=permission, before_tool=queue_write, after_tool=rafeeq.cap_result,
                      verbose=False)
            tools = rafeeq.local_tools(m) + read_tools()
            try:
                run = K.run_agent(req.message, tools, model=config["model"], **kw)
            except Exception as e:  # noqa: BLE001 - every model in the routing list failed: try a second model once
                log(audit_path, "model_error", session=req.session_id, error=type(e).__name__)
                try:
                    run = K.run_agent(req.message, tools, model=SECOND_MODEL, **kw)
                except Exception as e2:  # noqa: BLE001
                    raise HTTPException(503, f"The model is unavailable ({type(e2).__name__}). Try again shortly.") from e2
            used = {"input": K.LEDGER.input_tokens - before[0], "output": K.LEDGER.output_tokens - before[1]}
        reply = run.final or "I couldn't finish this request. An officer will follow up."
        with conn() as con:
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (sid, json.dumps(run.state[1:], ensure_ascii=False, default=str), spent + used["input"] + used["output"], time.time()))
            rows = [con.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone() for pid in queued]
        names = [n for n, _ in run.tool_calls]
        log(trace_path, "chat_turn", session=req.session_id, tools=names, steps=run.steps, stopped=run.stopped, tokens=used,
            queued=queued, blocked=blocked, seconds=round(time.perf_counter() - started, 1), by=who(key))
        return ChatOut(session_id=req.session_id, reply=reply, tools=names, pending_actions=[view(r) for r in rows],
                       blocked=blocked, tokens=used, model_steps=run.steps)

    @app.get("/v1/pending", response_model=list[PendingOut])
    def list_pending(status: str = "pending", session_id: str | None = None, key: str = Depends(officer_key)) -> list[PendingOut]:
        sql, params = "SELECT * FROM pending WHERE status = ?", [status]
        if status == "all":
            sql, params = "SELECT * FROM pending WHERE 1 = 1", []
        if session_id:
            sql, params = sql + " AND session_id = ?", params + [session_id]
        with conn() as con:
            return [view(r) for r in con.execute(sql + " ORDER BY id", params)]

    @app.get("/v1/pending/{pid}", response_model=PendingOut)
    def get_pending(pid: int, key: str = Depends(officer_key)) -> PendingOut:
        with conn() as con:
            row = con.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, f"No pending action {pid}")
        return view(row)

    @app.post("/v1/pending/{pid}/decide", response_model=PendingOut)
    def decide(pid: int, body: DecisionIn, key: str = Depends(officer_key)) -> PendingOut:
        by = who(key)
        with conn() as con:
            # claim the row first: two clicks (or two officers) cannot both run the same action
            claimed = con.execute("UPDATE pending SET status = 'deciding' WHERE id = ? AND status = 'pending'", (pid,)).rowcount
            row = con.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, f"No pending action {pid}")
        if not claimed:
            raise HTTPException(409, f"Action {pid} is already {row['status']}")
        name, args = row["tool"], json.loads(row["args"])
        result: dict | None = None
        if body.decision == "reject":
            status = "rejected"
        elif reason := rafeeq.egress_blocked(name, args):  # checked again: the guard does not trust the queue
            status, result = "blocked", {"error": reason}
        else:
            with lock:
                m.actor = f"rafeeq-api, approved by {by}"  # recorded on the write itself (refunds.created_by, audit_log)
                try:
                    out = K.call_tool({t.__name__: t for t in rafeeq.local_tools(m)}, name, args)
                finally:
                    m.actor = "rafeeq-api"
            result = json.loads(out)
            status = "failed" if isinstance(result, dict) and "error" in result else "approved"
        with conn() as con:
            con.execute("UPDATE pending SET status = ?, decided_at = ?, decided_by = ?, reason = ?, result = ? WHERE id = ?",
                        (status, time.time(), by, body.reason, json.dumps(result, ensure_ascii=False) if result else None, pid))
            row = con.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone()
        log(audit_path, "decision", id=pid, decision=body.decision, status=status, tool=name, args=masked(args), by=by,
            reason=body.reason, result=result)
        return view(row)

    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8793)
    args = ap.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
