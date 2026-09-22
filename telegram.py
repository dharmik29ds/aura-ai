"""
Minimal Telegram Bot API client (no extra dependencies beyond httpx,
which is already installed as a dependency of openai/fastapi).
"""
import os
import httpx

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None


async def send_message(chat_id: str, text: str) -> bool:
    if not API:
        print("[telegram] TELEGRAM_BOT_TOKEN not set, skipping send")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})
        if r.status_code != 200:
            print(f"[telegram] send failed: {r.status_code} {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[telegram] send error: {e!r}")
        return False


async def get_latest_chat_id() -> str | None:
    """Used once during /telegram/link: finds the most recent chat_id that
    messaged the bot, so we can save it against the dev user's profile.
    Uses offset=0 (the default) so it never marks updates as read/consumed --
    a negative offset does that, which caused this to return empty on a
    second call."""
    if not API:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{API}/getUpdates", params={"limit": 20})
        data = r.json()
        results = [u for u in data.get("result", []) if "message" in u]
        if not results:
            return None
        return str(results[-1]["message"]["chat"]["id"])
    except Exception as e:
        print(f"[telegram] getUpdates error: {e!r}")
        return None
