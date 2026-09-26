"""
Aura backend (FastAPI) using the Groq API, with real multi-user login via
Supabase Auth (email + password).

Run:  uvicorn main:app --reload
Open: http://localhost:8000
"""

import json
import os
from openai import AsyncOpenAI
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncio
import asyncpg
import httpx
from dotenv import load_dotenv
load_dotenv()  # must run before `import telegram`, which reads env vars at import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from openai import AsyncOpenAI          # Groq is OpenAI-compatible
from pydantic import BaseModel

from tools import TOOLS, run_tool, execute_confirmed
import telegram

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = os.getenv("MODEL", "openai/gpt-oss-120b")
MODEL_VISION = os.getenv("MODEL_VISION", "meta-llama/llama-4-scout-17b-16e-instruct")
SUPABASE_URL = os.getenv("SUPABASE_URL")           # e.g. https://xxxx.supabase.co
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
TELEGRAM_POLL_SECONDS = 30
image_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

BASE = Path(__file__).parent
PROMPT_TEMPLATE = (BASE / "system_prompt.md").read_text(encoding="utf-8").split("\n---\n")[1].strip()

client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
pool: asyncpg.Pool | None = None

GROQ_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOLS
]


async def reminder_loop():
    """Runs forever in the background: every TELEGRAM_POLL_SECONDS, sends any
    reminder whose time has arrived to that user's linked Telegram chat."""
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
    import traceback
    print("[groq error]")
    traceback.print_exc()

    return {
        "reply": "AI service error. Please check Render logs.",
        "pending_actions": [],
        "images": []
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    missing = [k for k, v in {"GROQ_API_KEY": GROQ_API_KEY, "DATABASE_URL": DATABASE_URL,
                              "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY
                              }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------
# Auth: verify the Supabase access token sent by the frontend, upsert a
# profile row for that user, and return their user_id. This is what makes
# every user's notes, reminders, and name their own instead of one shared
# "DEV_USER_ID".
# ---------------------------------------------------------------
async def require_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not logged in.")
    token = authorization.removeprefix("Bearer ").strip()
    anon_key = (SUPABASE_ANON_KEY or "").strip()
    try:
        async with httpx.AsyncClient(timeout=10) as http_client:
            r = await http_client.get(
                f"{SUPABASE_URL.strip()}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": anon_key},
            )
    except Exception as e:
        print(f"[auth] Supabase reach error: {e!r}")
        raise HTTPException(503, "Could not verify login right now. Try again.")
    if r.status_code != 200:
        raise HTTPException(401, "Your session expired. Please log in again.")
    user = r.json()
    user_id = user["id"]
    display_name = (
        (user.get("user_metadata") or {}).get("full_name")
        or (user.get("email") or "").split("@")[0]
        or "Friend"
    )
    await pool.execute(
        "insert into profiles (id, display_name) values ($1, $2) "
        "on conflict (id) do update set display_name = coalesce(profiles.display_name, excluded.display_name)",
        user_id, display_name,
    )
    return user_id


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
async def do_edit_photo(
    instruction: str,
    image_base64: str,
    image_mime: str = "image/jpeg"
) -> dict:
    """Edit an uploaded image using OpenAI."""

    if not image_base64:
        return {
            "ok": False,
            "error": "No photo was uploaded."
        }

    if not OPENAI_API_KEY:
        return {
            "ok": False,
            "error": "OPENAI_API_KEY is not configured."
        }

    try:
        import base64
        from io import BytesIO

        image_bytes = base64.b64decode(image_base64)
        image_file = BytesIO(image_bytes)

        if "png" in image_mime:
            image_file.name = "upload.png"
        elif "webp" in image_mime:
            image_file.name = "upload.webp"
        else:
            image_file.name = "upload.jpg"

        result = await image_client.images.edit(
            model="gpt-image-1",
            image=image_file,
            prompt=instruction
        )

        if not result.data or not result.data[0].b64_json:
            return {
                "ok": False,
                "error": "No edited image was returned."
            }

        edited_base64 = result.data[0].b64_json

        return {
            "ok": True,
            "images": [
                {
                    "title": instruction,
                    "image_url": f"data:image/png;base64,{edited_base64}",
                    "source": "OpenAI"
                }
            ]
        }

    except Exception as e:
        print(f"[edit_photo error] {e!r}")
        return {
            "ok": False,
            "error": f"Image editing failed: {str(e)}"
        }

class ChatIn(BaseModel):
    message: str
    history: list[dict] = []
    image_base64: str | None = None   # data-URL-ready base64 (no prefix), for an uploaded photo
    image_mime: str | None = None     # e.g. "image/jpeg"


@app.get("/")
async def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/config")
async def config():
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY}


@app.get("/me", dependencies=[])
async def me(user_id: str = Depends(require_user)):
    prof = await pool.fetchrow("select display_name, telegram_chat_id from profiles where id = $1", user_id)
  
    return {"display_name": prof["display_name"], "telegram_linked": bool(prof["telegram_chat_id"])}


@app.post("/chat")
async def chat(body: ChatIn, user_id: str = Depends(require_user)):
    system, memory_enabled = await build_system_prompt(user_id)
    tools = [
    t for t in (GROQ_TOOLS or [])
    if memory_enabled or t["function"]["name"] != "save_memory"
]

    messages = [{"role": "system", "content": system}]
    messages += [m for m in body.history if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": body.message})

    pending = []
    images = []
    reply_text = ""

    has_image = bool(body.image_base64)
    active_model = MODEL_VISION if has_image else MODEL
    if has_image:
        # Replace the plain-text last user message with a multimodal one
        # carrying the photo, so the model can see it in the same
        # tool-calling loop (it may just describe it, or call edit_photo).
        messages[-1] = {"role": "user", "content": [
            {"type": "text", "text": body.message or "What's in this photo?"},
            {"type": "image_url", "image_url": {
                "url": f"data:{body.image_mime or 'image/jpeg'};base64,{body.image_base64}"}},
        ]}

    try:
        for i in range(6):
            try:
                resp = await client.chat.completions.create(
                    model=active_model, messages=messages, tools=tools, max_tokens=1024)
            except Exception as e:
                if "tool call validation failed" in str(e).lower() and tools:
                    print(f"[groq tool-name error, retrying without tools] {e!r}")
                    resp = await client.chat.completions.create(
                        model=active_model, messages=messages, tools=None, max_tokens=1024)
                else:
                    raise
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
                if tc.function.name == "edit_photo":
                    out = await do_edit_photo(
                        args.get("instruction", ""),
                        body.image_base64,
                        body.image_mime or "image/jpeg"
                    )
                else:
                    out = await run_tool(
                        pool,
                        user_id,
                        tc.function.name,
                        args
                    )

            if out.get("pending_action_id"):
                pending.append({
                    "id": out["pending_action_id"],
                    "summary": out["summary"]
                })

            if tc.function.name in ("image_search", "generate_image", "edit_photo") and out.get("ok"):
                images.extend(out.get("images", []))

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(out)
            })
    except Exception as e:
        print(f"[groq error] {e!r}")
        return {"reply": "Sorry, I couldn't reach the AI service. Check your GROQ_API_KEY and "
                         "the terminal for details, then try again.", "pending_actions": [], "images": []}

    return {"reply": reply_text or "Done.", "pending_actions": pending, "images": images}


@app.api_route("/telegram/link", methods=["GET", "POST"])
async def telegram_link(user_id: str = Depends(require_user)):
    chat_id = await telegram.get_latest_chat_id()
    if not chat_id:
        raise HTTPException(400, "No recent message found. Send your bot any message on "
                                 "Telegram first, then try this again.")
    await pool.execute(
        "update profiles set telegram_chat_id = $1, updated_at = now() where id = $2",
        chat_id, user_id)
    return {"ok": True, "chat_id": chat_id}


@app.get("/telegram/status")
async def telegram_status(user_id: str = Depends(require_user)):
    prof = await pool.fetchrow("select telegram_chat_id from profiles where id = $1", user_id)
    return {"linked": bool(prof["telegram_chat_id"])}


@app.post("/confirm/{action_id}")
async def confirm(action_id: str, user_id: str = Depends(require_user)):
    result = await execute_confirmed(pool, user_id, action_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not confirm."))
    return result


@app.post("/reject/{action_id}")
async def reject(action_id: str, user_id: str = Depends(require_user)):
    await pool.execute(
        "update pending_actions set status = 'rejected' "
        "where id = $1 and user_id = $2 and status = 'pending'", action_id, user_id)
    return {"ok": True}
