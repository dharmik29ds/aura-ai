import os
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from groq import AsyncGroq
from tools import TOOLS, run_tool

BASE = Path(__file__).resolve().parent

APP_PASSCODE = os.getenv("APP_PASSCODE", "")
DEV_USER_ID = "dev_user"
MODEL = "llama-3.3-70b-versatile"

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

async def require_passcode(request: Request):
    if APP_PASSCODE:
        passcode = request.headers.get("X-Passcode") or request.query_params.get("passcode")
        if passcode != APP_PASSCODE:
            raise HTTPException(401, "Wrong passcode.")

class ChatIn(BaseModel):
    message: str
    history: list[dict] = []

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
    user_text = body.message.strip()

    # ૧. ઈમેજ જનરેશન કીવર્ડ ચેક
    image_keywords = ["photo", "image", "draw", "picture", "create photo", "generate image", "make photo"]
    if any(keyword in user_text.lower() for keyword in image_keywords):
        encoded_prompt = urllib.parse.quote(user_text)
        img_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"
        return {
            "response": f"Here is your generated image:\n\n![Generated Image]({img_url})",
            "reply": f"Here is your generated image:\n\n![Generated Image]({img_url})",
            "images": [img_url],
            "pending": []
        }

    # ૨. સામાન્ય ચેટ (LLM response)
    user_id = DEV_USER_ID

    messages = [{"role": "system", "content": "You are Aura, a helpful AI assistant."}]
    messages += [m for m in body.history if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": body.message})

    reply_text = ""
    try:
        for _ in range(6):
            resp = await client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, max_tokens=1024
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                reply_text = msg.content or ""
                break

            messages.append(msg)
            for tool_call in msg.tool_calls:
                tool_result = await run_tool(tool_call, user_id)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(tool_result)
                })

        return {
            "response": reply_text,
            "reply": reply_text,
            "images": [],
            "pending": []
        }
    except Exception as e:
        return {
            "response": f"Error: {str(e)}",
            "reply": f"Error: {str(e)}",
            "images": [],
            "pending": []
        }
