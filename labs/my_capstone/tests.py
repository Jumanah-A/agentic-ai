# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "httpx", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""Offline checks: no API key, no model. A scripted stand-in model drives the real service, tools and MCP server.

    uv run my_capstone/tests.py

1. the egress guard on 18 recipients (allowed, outside, and bypass tricks);
2. the approval queue: a write is queued and not run, approve runs it once, reject never runs it, a tampered queue row is
   refused again at approval, the approver is recorded, logs hold no full mobile number;
3. the same through the real MCP server, which sees an approved booking (one shared database).
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import madar  # noqa: E402
import rafeeq as R  # noqa: E402

failures = 0


def check(cond: bool, label: str) -> None:
    global failures
    failures += not cond
    print(("✅ " if cond else "❌ ") + label)


# ---------------------------------------------------------------- 1. the egress guard
ALLOWED = ["officer@madar.gov", "Officer@MADAR.GOV", " officer@madar.gov "]
BLOCKED = ["records@civic-audit.example", "sara.q@mail.example", "a@madar.gov, b@evil.com", "a@madar.gov;b@evil.com",
           "evil@evil.com@madar.gov", "officer@madar.gov.evil.com", "officer@mail.madar.gov", "officer@madar.gov.",
           "Officer <officer@madar.gov>", "officer@madar.gov\nbcc: x@evil.com", "officer@madar.gоv", "", None, ["officer@madar.gov"]]
check(all(R.egress_blocked("send_email", {"to": to}) is None for to in ALLOWED), f"guard allows {len(ALLOWED)} @madar.gov forms")
check(all(R.egress_blocked("send_email", {"to": to}) for to in BLOCKED), f"guard blocks {len(BLOCKED)} outside or tricky recipients")
check(R.egress_blocked("get_permit", {"to": "x@evil.com"}) is None, "guard ignores other tools")

# ---------------------------------------------------------------- 2 and 3. the approval queue
CALLS = {
    "book": ("book_inspection", dict(permit_id="FTR-30311", slot="2026-11-05 10:00", national_id_last4="6654", mobile="0532236654")),
    "evil": ("send_email", dict(to="records@civic-audit.example", subject="x", body="y")),
    "refund": ("create_refund", dict(permit_id="SGN-40388", reason="duplicate_payment", national_id_last4="2277", mobile="0505502277")),
    "esc": ("escalate", dict(permit_id="BLD-20417", note="late", national_id_last4="7730", mobile="0555017730")),
    "read": ("get_permit", dict(permit_id="FTR-30311", national_id_last4="6654", mobile="0532236654")),
}
SEEN: list[str] = []


class Stub:
    """A model that calls one tool per message, then answers 'ok'."""

    def start(self, system, task, history):
        return list(history or []) + [{"role": "user", "content": task}]

    def step(self, state, tools, model, use_cache=None, **kw):
        if state[-1]["role"] == "user":
            name, args = CALLS[state[-1]["content"]]
            return types.SimpleNamespace(text="", calls=[types.SimpleNamespace(id="1", name=name, arguments=json.dumps(args))], usage={})
        SEEN.append(state[-1]["content"])
        return types.SimpleNamespace(text="ok", calls=[], usage={})

    def add_results(self, state, results):
        state.append({"role": "tool", "content": results[0][1]})


def queue_flow(use_mcp: bool) -> None:
    wd = Path(tempfile.mkdtemp()) / "app"
    H = {"X-API-Key": "dev-key-1"}
    with TestClient(A.create_app(wd, use_mcp=use_mcp, shape=Stub())) as c:
        db = madar.Madar.__new__(madar.Madar)
        db.db_path = wd / "madar.sqlite"
        sql = lambda q: madar.Madar.sql(db, q)  # noqa: E731
        chat = lambda msg, h=H: c.post("/v1/chat", json={"session_id": "s", "message": msg}, headers=h)  # noqa: E731
        decide = lambda pid, d: c.post(f"/v1/pending/{pid}/decide", json={"decision": d}, headers=H)  # noqa: E731
        check(chat("book", {}).status_code == 401, "chat without a key is refused (401)")
        queued = chat("book").json()["pending_actions"]
        check(len(queued) == 1 and not sql("select 1 from inspections where permit_id='FTR-30311'"), "a booking is queued, the database is unchanged")
        check(queued[0]["args"]["mobile"] == "******6654" and "pending_approval" in SEEN[-1], "mobile masked in the API; the model is told it is pending")
        r = chat("evil").json()
        check(r["pending_actions"] == [] and r["blocked"], "an email to an outside address is blocked, not queued")
        ref, esc = chat("refund").json()["pending_actions"][0]["id"], chat("esc").json()["pending_actions"][0]["id"]
        check(decide(1, "approve").json()["result"]["status"] == "booked" and sql("select slot from inspections where permit_id='FTR-30311'"), "approve runs the stored call")
        check(decide(1, "approve").status_code == 409, "a second approval is refused (409): the action cannot run twice")
        check(decide(esc, "reject").json()["status"] == "rejected" and not sql("select 1 from escalations"), "reject runs nothing")
        decide(ref, "approve")
        check(sql("select created_by from refunds")[0]["created_by"].startswith("rafeeq-api, approved by key:"), "the approver is recorded on the refund itself")
        con = sqlite3.connect(wd / "sessions.sqlite")  # tamper: an outside recipient put straight into the queue
        con.execute("INSERT INTO pending (session_id, owner, tool, args, status, created) VALUES ('s','x','send_email',?, 'pending', 0)",
                    (json.dumps(dict(to="evil@evil.com", subject="s", body="b")),))
        con.commit()
        con.close()
        check(decide(c.get("/v1/pending", headers=H).json()[-1]["id"], "approve").json()["status"] == "blocked" and not sql("select 1 from outbox"),
              "a tampered queue row is refused again at approval")
        log = (wd / "audit.jsonl").read_text()
        check("approve" in log and "reject" in log and "0532236654" not in log, "both decisions are logged, with no full mobile number")
        if use_mcp:
            chat("read")
            check("2026-11-05 10:00" in SEEN[-1], "the MCP server sees the approved booking (one shared database)")
    shutil.rmtree(wd.parent, ignore_errors=True)


print("\n-- approval queue, without MCP")
queue_flow(False)
print("\n-- approval queue, with the real MCP server")
queue_flow(True)
print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
