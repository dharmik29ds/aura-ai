"""
Aura tool layer: Claude tool definitions + a dispatcher.

Design rules:
1. user_id is NEVER a tool parameter. The backend injects it from the
   verified auth token, so the model cannot act on another user's data.
2. Sensitive tools (delete) do not execute directly. They create a row in
   pending_actions. Only the backend, on a real user confirmation
   (button tap or explicit "yes"), executes it. The model has no
   "confirm" tool, so it cannot approve its own action.
3. Every call is written to action_logs, and every result carries "ok" so
   the model can only claim success when the tool actually succeeded.

NOTE: Handlers use an asyncpg-style `db` object (fetch, fetchrow, execute)
and an `embed()` function you supply. Not yet run against a live database.
"""

import json
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------
# Tool definitions (Anthropic Messages API format)
# ---------------------------------------------------------------
TOOLS = [
    {
        "name": "save_memory",
        "description": (
            "Store a durable fact, preference, goal, or standing instruction "
            "about the user (e.g. 'prefers Hinglish', 'wants to gym 4x/week'). "
            "Do not store passwords, card numbers, or other secrets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["preference", "fact", "goal", "instruction"]},
                "content": {"type": "string", "description": "The memory, as a short standalone sentence."},
            },
            "required": ["kind", "content"],
        },
    },
    {
        "name": "search_memory",
        "description": "Semantic search over the user's saved memories. Use before answering personal questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_note",
        "description": "Save a note for the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["body"],
        },
    },
    {
        "name": "list_notes",
        "description": "List recent notes, optionally filtered by tag or keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Create a reminder. remind_at must be ISO 8601 with the user's timezone offset, "
            "resolved from the user's local time. Ask if the time is ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "remind_at": {"type": "string", "description": "ISO 8601, e.g. 2026-09-22T08:00:00+05:30"},
                "recurrence": {"type": "string", "description": "Optional iCal RRULE, e.g. FREQ=DAILY"},
            },
            "required": ["title", "remind_at"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the user's upcoming pending reminders.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        },
    },
    {
        "name": "update_reminder",
        "description": "Change a reminder's title or time, or mark it done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string"},
                "title": {"type": "string"},
                "remind_at": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "done", "cancelled"]},
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "delete_record",
        "description": (
            "Request deletion of a note, reminder, or memory. This does NOT delete "
            "immediately: it asks the user to confirm. Tell the user you are waiting "
            "for their confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "enum": ["notes", "reminders", "memories"]},
                "record_id": {"type": "string"},
                "reason_summary": {"type": "string", "description": "Plain-language description of what will be deleted."},
            },
            "required": ["table", "record_id", "reason_summary"],
        },
    },
]

REQUIRES_CONFIRMATION = {"delete_record"}


# ---------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------
async def save_memory(db, user_id, kind, content, **_):
    emb = await embed(content)
    row = await db.fetchrow(
        "insert into memories (user_id, kind, content, embedding) "
        "values ($1,$2,$3,$4::vector) returning id",
        user_id, kind, content, emb,
    )
    return {"ok": True, "id": str(row["id"])}


async def search_memory(db, user_id, query, limit=5, **_):
    emb = await embed(query)
    if emb is None:
        # No embedding model configured: fall back to keyword search
        rows = await db.fetch(
            "select id, kind, content from memories "
            "where user_id = $1 and deleted_at is null and content ilike '%' || $2 || '%' "
            "order by created_at desc limit $3",
            user_id, query, limit,
        )
        return {"ok": True, "results": [
            {"id": str(r["id"]), "kind": r["kind"], "content": r["content"]} for r in rows]}
    rows = await db.fetch(
        "select id, kind, content, 1 - (embedding <=> $2::vector) as similarity "
        "from memories where user_id = $1 and deleted_at is null "
        "order by embedding <=> $2::vector limit $3",
        user_id, emb, limit,
    )
    return {"ok": True, "results": [
        {"id": str(r["id"]), "kind": r["kind"], "content": r["content"],
         "similarity": round(r["similarity"], 3)} for r in rows]}


async def create_note(db, user_id, body, title=None, tags=None, **_):
    row = await db.fetchrow(
        "insert into notes (user_id, title, body, tags) values ($1,$2,$3,$4) returning id",
        user_id, title, body, tags or [],
    )
    return {"ok": True, "id": str(row["id"])}


async def list_notes(db, user_id, tag=None, keyword=None, limit=10, **_):
    rows = await db.fetch(
        "select id, title, body, tags, created_at from notes "
        "where user_id = $1 and deleted_at is null "
        "and ($2::text is null or $2 = any(tags)) "
        "and ($3::text is null or body ilike '%' || $3 || '%' or title ilike '%' || $3 || '%') "
        "order by created_at desc limit $4",
        user_id, tag, keyword, limit,
    )
    return {"ok": True, "notes": [
        {**dict(r), "id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]}


async def create_reminder(db, user_id, title, remind_at, recurrence=None, **_):
    row = await db.fetchrow(
        "insert into reminders (user_id, title, remind_at, recurrence) "
        "values ($1,$2,$3,$4) returning id, remind_at",
        user_id, title, datetime.fromisoformat(remind_at), recurrence,
    )
    return {"ok": True, "id": str(row["id"]), "remind_at": row["remind_at"].isoformat()}


async def list_reminders(db, user_id, limit=10, **_):
    rows = await db.fetch(
        "select id, title, remind_at, recurrence from reminders "
        "where user_id = $1 and status = 'pending' and deleted_at is null "
        "order by remind_at limit $2",
        user_id, limit,
    )
    return {"ok": True, "reminders": [
        {"id": str(r["id"]), "title": r["title"],
         "remind_at": r["remind_at"].isoformat(), "recurrence": r["recurrence"]} for r in rows]}


async def update_reminder(db, user_id, reminder_id, title=None, remind_at=None, status=None, **_):
    row = await db.fetchrow(
        "update reminders set title = coalesce($3, title), "
        "remind_at = coalesce($4, remind_at), "
        "status = coalesce($5, status), updated_at = now() "
        "where id = $2 and user_id = $1 and deleted_at is null returning id",
        user_id, reminder_id, title,
        datetime.fromisoformat(remind_at) if remind_at else None, status,
    )
    if not row:
        return {"ok": False, "error": "Reminder not found."}
    return {"ok": True, "id": str(row["id"])}


async def delete_record(db, user_id, table, record_id, reason_summary, **_):
    """Creates a pending action only. Execution happens in execute_confirmed()."""
    row = await db.fetchrow(
        "insert into pending_actions (user_id, tool_name, tool_input, summary) "
        "values ($1,'delete_record',$2,$3) returning id",
        user_id, json.dumps({"table": table, "record_id": record_id}), reason_summary,
    )
    return {"ok": True, "status": "awaiting_user_confirmation",
            "pending_action_id": str(row["id"]), "summary": reason_summary}


HANDLERS = {
    "save_memory": save_memory, "search_memory": search_memory,
    "create_note": create_note, "list_notes": list_notes,
    "create_reminder": create_reminder, "list_reminders": list_reminders,
    "update_reminder": update_reminder, "delete_record": delete_record,
}


# ---------------------------------------------------------------
# Dispatcher: call this for each tool_use block Claude returns
# ---------------------------------------------------------------
async def run_tool(db, user_id: str, name: str, tool_input: dict[str, Any]) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        result = await handler(db, user_id, **tool_input)
    except Exception as e:  # never leak internals to the model or user
        result = {"ok": False, "error": "Something went wrong running that action."}
        print(f"[tool error] {name}: {e!r}")
    await db.execute(
        "insert into action_logs (user_id, tool_name, tool_input, result, ok) "
        "values ($1,$2,$3,$4,$5)",
        user_id, name, json.dumps(tool_input), json.dumps(result), bool(result.get("ok")),
    )
    return result


# ---------------------------------------------------------------
# Called by your API endpoint when the USER taps Confirm (never by the model)
# ---------------------------------------------------------------
ALLOWED_DELETE_TABLES = {"notes", "reminders", "memories"}

async def execute_confirmed(db, user_id: str, pending_action_id: str) -> dict:
    pa = await db.fetchrow(
        "update pending_actions set status = 'confirmed' "
        "where id = $1 and user_id = $2 and status = 'pending' and expires_at > now() "
        "returning tool_name, tool_input",
        pending_action_id, user_id,
    )
    if not pa:
        return {"ok": False, "error": "This request expired or was already handled."}
    args = json.loads(pa["tool_input"])
    if pa["tool_name"] == "delete_record" and args["table"] in ALLOWED_DELETE_TABLES:
        # table name is validated against an allowlist above, so the f-string is safe
        await db.execute(
            f"update {args['table']} set deleted_at = now() where id = $1 and user_id = $2",
            args["record_id"], user_id,
        )
        return {"ok": True, "deleted": args["record_id"]}
    return {"ok": False, "error": "Unsupported action."}


async def embed(text: str):
    """Return None until you plug in an embedding model. Memory then uses keyword search.
    To enable semantic search, return a string like '[0.1,0.2,...]' whose length
    matches vector(1536) in schema.sql."""
    return None
