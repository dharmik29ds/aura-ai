"""
Aura backend (FastAPI) using the Groq API.

Run:  uvicorn main:app --reload
Open: http://localhost:8000

DEV MODE: no login screen yet. The app acts as the single user whose UUID is
in DEV_USER_ID (.env). Before real users, add auth (e.g. Supabase Auth) and
take user_id from the verified token instead.
"""

import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from openai import AsyncOpenAI          # Groq is OpenAI-compatible
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv()  # must run before `import telegram`, which reads env vars at import time

from tools import TOOLS, run_tool, execute_confirmed
import telegram

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
DEV_USER_ID = os.getenv("DEV_USER_ID")
MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
APP_PASSCODE = os.getenv("APP_PASSCODE")   # simple access code; set it before sharing a public link
TELEGRAM_POLL_SECONDS = 30

BASE = Path(__file__).parent
PROMPT_TEMPLATE = (BASE / "system_prompt.md").read_text(encoding="utf-8").split("\n---\n")[1].strip()

client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
pool: asyncpg.Pool | None = None

# tools.py stores tool definitions in Anthropic format; convert to OpenAI/Groq format
GROQ_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    missing = [k for k, v in {"GROQ_API_KEY": GROQ_API_KEY,
                              "DATABASE_URL": DATABASE_URL,
                              "DEV_USER_ID": DEV_USER_ID}.items() if not v]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
    if not APP_PASSCODE:
        print("WARNING: APP_PASSCODE is not set. Do NOT expose this app to the internet without it.")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
    await pool.execute(
        "insert into profiles (id, display_name) values ($1, 'Friend') on conflict (id) do nothing",
        DEV_USER_ID,
    )
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    await pool.close()




async def reminder_loop():
    """Runs forever in the background: every TELEGRAM_POLL_SECONDS, sends
    any reminder whose time has arrived, then marks it sent (or reschedules
    recurring ones would need extra logic -- kept simple here for now)."""
    while True:
        try:
            due = await pool.fetch(
                "select id, user_id, title from reminders "
                "where status = 'pending' and deleted_at is null and remind_at <= now()"
            )
            for r in due:
                prof = await pool.fetchrow(
                    "select telegram_chat_id from profiles where id = $1", r["user_id"])
                sent = False
                if prof and prof["telegram_chat_id"]:
                    sent = await telegram.send_message(
                        prof["telegram_chat_id"], f"\U0001F514 Reminder: {r['title']}")
                await pool.execute(
                    "update reminders set status = 'sent', updated_at = now() where id = $1",
                    r["id"])
                if not sent:
                    print(f"[reminder] {r['id']} due but not sent (no linked Telegram chat)")
        except Exception as e:
            print(f"[reminder_loop error] {e!r}")
        await asyncio.sleep(TELEGRAM_POLL_SECONDS)


app = FastAPI(lifespan=lifespan)


async def build_system_prompt(user_id: str) -> tuple[str, bool]:
    prof = await pool.fetchrow("select * from profiles where id = $1", user_id)
    mems = await pool.fetch(
        "select kind, content from memories where user_id = $1 and deleted_at is null "
        "order by created_at desc limit 10", user_id)
    memories = "\n".join(f"- ({m['kind']}) {m['content']}" for m in mems) or "None yet."
    now_local = datetime.now(ZoneInfo(prof["timezone"])).strftime("%A, %d %B %Y, %I:%M %p")
    prompt = (PROMPT_TEMPLATE
              .replace("{{display_name}}", prof["display_name"] or "the user")
              .replace("{{language_pref}}", prof["language_pref"])
              .replace("{{tone_pref}}", prof["tone_pref"])
              .replace("{{timezone}}", prof["timezone"])
              .replace("{{now_local}}", now_local)
              .replace("{{retrieved_memories}}", memories))
    return prompt, prof["memory_enabled"]



# ---------------------------------------------------------------
# Simple passcode gate (stop-gap until real user login is added)
# ---------------------------------------------------------------
_failed: dict[str, list[float]] = {}


async def require_passcode(request: Request):
    return


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": "text"}]


@app.get("/")
async def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/config")
async def config():
    return {"passcode_required": bool(APP_PASSCODE)}


@app.post("/login", dependencies=[Depends(require_passcode)])
async def login():
    return {"ok": True}


@app.post("/chat", dependencies=[Depends(require_passcode)])
async def chat(body: ChatIn):
    user_id = DEV_USER_ID
    system, memory_enabled = await build_system_prompt(user_id)
    tools = [t for t in GROQ_TOOLS if memory_enabled or t["function"]["name"] != "save_memory"]

    messages = [{"role": "system", "content": system}]
    messages += [m for m in body.history if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": body.message})

    pending = []
    reply_text = ""

    try:
        for _ in range(6):  # tool-use loop, capped
            resp = await client.chat.completions.create(
                model=MODEL, messages=messages, tools=tools, max_tokens=1024)
            msg = resp.choices[0].message

            if not msg.tool_calls:
                reply_text = msg.content or ""
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    out = {"ok": False, "error": "Invalid tool arguments."}
                else:
                    out = await run_tool(pool, user_id, tc.function.name, args)
                if out.get("pending_action_id"):
                    pending.append({"id": out["pending_action_id"], "summary": out["summary"]})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out)})
    except Exception as e:
        print(f"[groq error] {e!r}")
        return {"reply": "Sorry, I couldn't reach the AI service. Check your GROQ_API_KEY and "
                         "the terminal for details, then try again.", "pending_actions": []}

    return {"reply": reply_text or "Done.", "pending_actions": pending}




@app.api_route("/telegram/link", methods=["GET", "POST"], dependencies=[Depends(require_passcode)])
async def telegram_link():
    """Call this once, right after you have messaged your bot on Telegram,
    to save your chat_id against the dev user's profile."""
    chat_id = await telegram.get_latest_chat_id()
    if not chat_id:
        raise HTTPException(400, "No recent message found. Send your bot any message on "
                                 "Telegram first, then try this again.")
    await pool.execute(
        "update profiles set telegram_chat_id = $1, updated_at = now() where id = $2",
        chat_id, DEV_USER_ID)
    return {"ok": True, "chat_id": chat_id}


@app.get("/telegram/status", dependencies=[Depends(require_passcode)])
async def telegram_status():
    prof = await pool.fetchrow("select telegram_chat_id from profiles where id = $1", DEV_USER_ID)
    return {"linked": bool(prof["telegram_chat_id"])}


@app.post("/confirm/{action_id}", dependencies=[Depends(require_passcode)])
async def confirm(action_id: str):
    result = await execute_confirmed(pool, DEV_USER_ID, action_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not confirm."))
    return result


@app.post("/reject/{action_id}", dependencies=[Depends(require_passcode)])
async def reject(action_id: str):
    await pool.execute(
        "update pending_actions set status = 'rejected' "
        "where id = $1 and user_id = $2 and status = 'pending'", action_id, DEV_USER_ID)
    return {"ok": True}
