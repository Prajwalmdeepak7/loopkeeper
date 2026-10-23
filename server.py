"""
Loopkeeper MCP Server (mcp 2.x)
Exposes MCP tools over Streamable HTTP at /mcp
Protocol version: 2025-11-25
"""
import sqlite3
import json
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# mcp 2.x: FastMCP was renamed to MCPServer
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("loopkeeper")

# ─── Database setup ──────────────────────────────────────────────────────────

# Vercel serverless supports writable filesystem only in /tmp
DEFAULT_DB = "/tmp/loopkeeper.db" if os.environ.get("VERCEL") else "loopkeeper.db"
DB_PATH = os.environ.get("DATABASE_URL", DEFAULT_DB)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS errands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            counterparty TEXT NOT NULL,
            details TEXT DEFAULT '',
            status TEXT DEFAULT 'captured',
            created_at TEXT DEFAULT (date('now')),
            updated_at TEXT DEFAULT (date('now'))
        );

        CREATE TABLE IF NOT EXISTS outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            errand_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            channel TEXT DEFAULT 'email',
            status TEXT DEFAULT 'drafted',
            created_at TEXT DEFAULT (date('now')),
            FOREIGN KEY(errand_id) REFERENCES errands(id)
        );

        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            errand_id INTEGER NOT NULL,
            reply_text TEXT NOT NULL,
            simulated_day INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (date('now')),
            FOREIGN KEY(errand_id) REFERENCES errands(id)
        );

        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (date('now'))
        );

        CREATE TABLE IF NOT EXISTS clock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sim_day INTEGER NOT NULL DEFAULT 0,
            last_session_day INTEGER NOT NULL DEFAULT 0
        );
    """)
    c.execute("INSERT OR IGNORE INTO clock (id, sim_day, last_session_day) VALUES (1, 0, 0)")
    conn.commit()

    existing = c.execute("SELECT COUNT(*) FROM errands").fetchone()[0]
    if existing == 0:
        c.execute("""
            INSERT INTO errands (title, counterparty, details, status, created_at, updated_at)
            VALUES (
                'Return the blender',
                'RetailStore',
                'Return window closes in 3 days. Receipt #3910 in kitchen drawer.',
                'captured',
                date('now', '-2 days'),
                date('now', '-2 days')
            )
        """)
        c.execute("""
            INSERT INTO replies (errand_id, reply_text, simulated_day)
            SELECT id, 'Hi, yes we accept returns within the 30-day window. Please bring the receipt and original box.', 1
            FROM errands WHERE title = 'Return the blender'
        """)
        conn.commit()
    conn.close()


def reset_database():
    """Clear and re-initialize DB for a fresh rehearsal."""
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        DROP TABLE IF EXISTS replies;
        DROP TABLE IF EXISTS outreach;
        DROP TABLE IF EXISTS errands;
        DROP TABLE IF EXISTS preferences;
        DROP TABLE IF EXISTS clock;
    """)
    conn.commit()
    conn.close()
    init_db()


def get_sim_day() -> int:
    conn = get_db()
    row = conn.execute("SELECT sim_day FROM clock WHERE id=1").fetchone()
    conn.close()
    return row["sim_day"] if row else 0


def set_sim_day(day: int):
    conn = get_db()
    conn.execute("UPDATE clock SET sim_day=? WHERE id=1", (day,))
    conn.commit()
    conn.close()


# ─── MCPServer (mcp 2.x) ─────────────────────────────────────────────────────

mcp = MCPServer("Loopkeeper")

# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool(
    description="Capture a new errand / open loop to track.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def capture_errand(title: str, counterparty: str, details: str = "") -> dict:
    """
    Capture a new errand.

    Args:
        title: Short title for the errand.
        counterparty: Person/service/company to contact.
        details: Any additional details or notes.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO errands (title, counterparty, details) VALUES (?,?,?)",
        (title, counterparty, details),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"errand_id": eid, "status": "captured", "title": title, "counterparty": counterparty}


@mcp.tool(
    description="Draft an outreach message for an errand (not yet sent).",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def draft_outreach(errand_id: int, message: str) -> dict:
    """
    Draft a message for an errand.

    Args:
        errand_id: ID of the errand.
        message: The message text to draft.
    """
    conn = get_db()
    errand = conn.execute("SELECT * FROM errands WHERE id=?", (errand_id,)).fetchone()
    if not errand:
        conn.close()
        return {"error": f"Errand {errand_id} not found"}
    cur = conn.execute(
        "INSERT INTO outreach (errand_id, message, status) VALUES (?,?,'drafted')",
        (errand_id, message),
    )
    oid = cur.lastrowid
    conn.execute(
        "UPDATE errands SET status='drafted', updated_at=date('now') WHERE id=?",
        (errand_id,),
    )
    conn.commit()
    conn.close()
    return {
        "outreach_id": oid,
        "errand_id": errand_id,
        "status": "drafted",
        "message": message,
        "requires_approval": True,
    }


@mcp.tool(
    description=(
        "Send the most recent drafted outreach for an errand. "
        "SIDE-EFFECTING — the UI must pause and show an Approve/Edit card before executing. "
        "⚠️ SIMULATED: Uses a simulated provider adapter; no real message is sent."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def send_outreach(errand_id: int, channel: str = "email") -> dict:
    """
    Send drafted outreach for an errand.

    Args:
        errand_id: ID of the errand.
        channel: Delivery channel — email, sms, or portal. Default: email.
    """
    conn = get_db()
    errand = conn.execute("SELECT * FROM errands WHERE id=?", (errand_id,)).fetchone()
    if not errand:
        conn.close()
        return {"error": f"Errand {errand_id} not found"}
    outreach = conn.execute(
        "SELECT * FROM outreach WHERE errand_id=? AND status='drafted' ORDER BY id DESC LIMIT 1",
        (errand_id,),
    ).fetchone()
    if not outreach:
        conn.close()
        return {"error": "No drafted outreach found — call draft_outreach first."}
    conn.execute(
        "UPDATE outreach SET status='sent', channel=? WHERE id=?",
        (channel, outreach["id"]),
    )
    conn.execute(
        "UPDATE errands SET status='waiting', updated_at=date('now') WHERE id=?",
        (errand_id,),
    )
    sim_day = get_sim_day()

    # Contextual simulated reply
    title_lower = (errand["title"] or "").lower()
    if "gym" in title_lower or "charge" in title_lower:
        reply_msg = (
            "FitLife Gym Billing: Hi Alex, we investigated your inquiry regarding the duplicate $49 charge on October 1st. "
            "We verified the billing error and have processed a full $49 refund back to your payment card. Reference #TX-9402."
        )
    else:
        reply_msg = f"Thanks for contacting {errand['counterparty']}. We have received your request and processed the update."

    conn.execute(
        "INSERT INTO replies (errand_id, reply_text, simulated_day) VALUES (?,?,?)",
        (errand_id, reply_msg, sim_day + 1),
    )
    conn.commit()
    conn.close()
    return {
        "outreach_id": outreach["id"],
        "errand_id": errand_id,
        "status": "sent",
        "channel": channel,
        "message": outreach["message"],
        "note": "⚠️ SIMULATED: No real message was sent. Simulated service adapter only.",
    }


@mcp.tool(
    description="List all open (unresolved) errands.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def list_open_loops() -> dict:
    """Return all errands that are not yet resolved."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, counterparty, status, created_at, updated_at FROM errands WHERE status != 'resolved' ORDER BY id"
    ).fetchall()
    conn.close()
    return {"errands": [dict(r) for r in rows], "count": len(rows)}


@mcp.tool(
    description=(
        "Check for replies to an errand from the SIMULATED provider adapter. "
        "Replies only appear after the simulated clock advances >= 1 day from when outreach was sent. "
        "⚠️ SIMULATED: replies are pre-scripted and deterministic — no real provider is contacted."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def check_replies(errand_id: Optional[int] = None) -> dict:
    """
    Check simulated replies for one or all errands.

    Args:
        errand_id: Optional errand ID. Omit to check all errands.
    """
    sim_day = get_sim_day()
    conn = get_db()
    if errand_id is not None:
        rows = conn.execute(
            """SELECT r.id, r.errand_id, r.reply_text, r.simulated_day, e.title
               FROM replies r JOIN errands e ON r.errand_id=e.id
               WHERE r.errand_id=? AND r.simulated_day <= ?
               ORDER BY r.simulated_day""",
            (errand_id, sim_day),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT r.id, r.errand_id, r.reply_text, r.simulated_day, e.title
               FROM replies r JOIN errands e ON r.errand_id=e.id
               WHERE r.simulated_day <= ?
               ORDER BY r.simulated_day""",
            (sim_day,),
        ).fetchall()
    conn.close()
    return {
        "replies": [dict(r) for r in rows],
        "count": len(rows),
        "sim_day": sim_day,
        "provider_note": "⚠️ SIMULATED: All replies are pre-scripted. No real communication occurs.",
    }


@mcp.tool(
    description="Resolve an errand with a given outcome.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def resolve_errand(errand_id: int, outcome: str) -> dict:
    """
    Mark an errand as resolved.

    Args:
        errand_id: ID of the errand.
        outcome: Description of how it was resolved.
    """
    conn = get_db()
    errand = conn.execute("SELECT * FROM errands WHERE id=?", (errand_id,)).fetchone()
    if not errand:
        conn.close()
        return {"error": f"Errand {errand_id} not found"}
    conn.execute(
        "UPDATE errands SET status='resolved', details=details||' | Outcome: '||?, updated_at=date('now') WHERE id=?",
        (outcome, errand_id),
    )
    conn.commit()
    conn.close()
    return {"errand_id": errand_id, "status": "resolved", "outcome": outcome}


@mcp.tool(
    description="Advance the simulated clock by N days (demo control).",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def advance_clock(days: int = 1) -> dict:
    """
    Advance the simulated clock.

    Args:
        days: Number of days to advance. Default: 1. Use 0 to read current day without advancing.
    """
    current = get_sim_day()
    new_day = current + days
    if days > 0:
        set_sim_day(new_day)
    return {"previous_day": current, "current_day": new_day, "advanced_by": days}


@mcp.tool(
    description="Generate a morning brief: what changed since last session, what is waiting, what needs user action.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def morning_brief() -> dict:
    """Return a comprehensive morning briefing about all open loops."""
    sim_day = get_sim_day()
    conn = get_db()

    open_errands = conn.execute(
        "SELECT id, title, counterparty, status, updated_at FROM errands WHERE status != 'resolved' ORDER BY id"
    ).fetchall()

    pending_replies = conn.execute(
        """SELECT r.errand_id, r.reply_text, r.simulated_day, e.title
           FROM replies r JOIN errands e ON r.errand_id=e.id
           WHERE r.simulated_day <= ? AND e.status != 'resolved'
           ORDER BY r.simulated_day""",
        (sim_day,),
    ).fetchall()

    needs_action = conn.execute(
        "SELECT id, title, counterparty, status FROM errands WHERE status IN ('captured','drafted')"
    ).fetchall()

    conn.close()

    summary_parts = []
    if open_errands:
        summary_parts.append(f"You have {len(open_errands)} open loop(s).")
    if pending_replies:
        summary_parts.append(f"{len(pending_replies)} reply(ies) waiting for your attention.")
    if needs_action:
        titles = [r["title"] for r in needs_action]
        summary_parts.append(f"Needs your action: {', '.join(titles)}.")

    return {
        "sim_day": sim_day,
        "open_errands": [dict(r) for r in open_errands],
        "pending_replies": [dict(r) for r in pending_replies],
        "needs_action": [dict(r) for r in needs_action],
        "summary": " ".join(summary_parts) if summary_parts else "All loops resolved! Great job.",
    }


@mcp.tool(
    description="Remember a user preference by key.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def remember_preference(key: str, value: str) -> dict:
    """
    Store a user preference.

    Args:
        key: Preference key (e.g. 'preferred_channel').
        value: Preference value (e.g. 'email').
    """
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, date('now'))",
        (key, value),
    )
    conn.commit()
    conn.close()
    return {"key": key, "value": value, "stored": True}


@mcp.tool(
    description="Recall all stored user preferences.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
def recall_preferences() -> dict:
    """Return all stored user preferences."""
    conn = get_db()
    rows = conn.execute("SELECT key, value, updated_at FROM preferences ORDER BY key").fetchall()
    conn.close()
    return {"preferences": {r["key"]: r["value"] for r in rows}}


@mcp.tool(
    description="Reset demo database state back to Day 0 with only pre-seeded blender errand.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def reset_demo() -> dict:
    """Reset demo state back to pristine starting point."""
    reset_database()
    return {"status": "ok", "message": "Demo reset to Day 0", "sim_day": 0}


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("loops://open")
def open_loops_resource() -> str:
    """Live list of open errands as JSON text."""
    return json.dumps(list_open_loops(), indent=2)


# ── Prompts ──────────────────────────────────────────────────────────────────


@mcp.prompt("morning_brief")
def morning_brief_prompt() -> str:
    """A prompt template for the morning brief."""
    result = morning_brief()
    return (
        "You are Loopkeeper, a voice assistant that helps people finish errands they keep avoiding.\n\n"
        f"Current simulated day: {result['sim_day']}\n\n"
        f"Open loops:\n{json.dumps(result['open_errands'], indent=2)}\n\n"
        f"Pending replies:\n{json.dumps(result['pending_replies'], indent=2)}\n\n"
        f"Needs action:\n{json.dumps(result['needs_action'], indent=2)}\n\n"
        f"Summary: {result['summary']}\n\n"
        "Please give a warm, concise morning brief. Prioritize urgency. Speak naturally."
    )


# ─── FastAPI app ──────────────────────────────────────────────────────────────

# Build the MCP streamable HTTP app first (this initializes the session_manager)
mcp_starlette_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Loopkeeper", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)


@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "Loopkeeper",
        "mcp_endpoint": "/mcp/",
        "protocol_version": "2025-11-25",
        "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
    }


@app.post("/api/reset")
async def api_reset():
    reset_database()
    return {"status": "ok", "message": "Demo state successfully reset to Day 0"}


# ── Gemini proxy (keeps API key server-side) ──────────────────────────────────

@app.post("/api/gemini")
async def gemini_proxy(request: Request):
    try:
        import google.generativeai as genai
        body = await request.json()
        messages = body.get("messages", [])
        tools_defs = body.get("tools", [])

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return JSONResponse({
                "text": "GEMINI_API_KEY is not set. You can toggle Demo Mode at the top right to run the scripted voice experience, or provide GEMINI_API_KEY in .env.",
                "tool_calls": [],
                "note": "no_api_key",
            })

        genai.configure(api_key=api_key)

        for model_name in ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash-latest"]:
            try:
                model = genai.GenerativeModel(model_name=model_name)
                contents = []
                for msg in messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role == "user":
                        contents.append({"role": "user", "parts": [{"text": content}]})
                    elif role == "assistant":
                        contents.append({"role": "model", "parts": [{"text": content}]})
                    elif role == "tool":
                        contents.append({"role": "user", "parts": [{"text": f"Tool result: {content}"}]})

                if not contents:
                    contents.append({"role": "user", "parts": [{"text": "Hello"}]})

                func_decls = [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    }
                    for t in tools_defs
                ]
                gemini_tools = [{"function_declarations": func_decls}] if func_decls else []

                history = contents[:-1] if len(contents) > 1 else []
                last_msg = contents[-1]["parts"][0]["text"]

                chat = model.start_chat(history=history)
                resp = chat.send_message(
                    last_msg,
                    generation_config=genai.GenerationConfig(temperature=0),
                    **({"tools": gemini_tools} if gemini_tools else {}),
                )

                tool_calls, text_parts = [], []
                for cand in resp.candidates:
                    for part in cand.content.parts:
                        if hasattr(part, "function_call") and part.function_call:
                            fc = part.function_call
                            tool_calls.append({"name": fc.name, "args": dict(fc.args) if fc.args else {}})
                        elif hasattr(part, "text") and part.text:
                            text_parts.append(part.text)

                return JSONResponse({"text": " ".join(text_parts), "tool_calls": tool_calls, "model": model_name})

            except Exception as e:
                logger.warning(f"Gemini model {model_name} failed: {e}")
                continue

        return JSONResponse({"text": "All Gemini models unavailable. Please check your API key.", "tool_calls": []})

    except Exception as e:
        logger.error(f"Gemini proxy error: {e}")
        return JSONResponse({"text": f"Error: {e}", "tool_calls": []}, status_code=500)


# Mount MCP at /mcp
app.mount("/mcp", mcp_starlette_app)

# Serve static files
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
