"""
Real web + image search via Serper.dev (a thin wrapper around Google Search).
Free tier: 2,500 queries, no credit card needed. Get a key at serper.dev.
"""
import os
import httpx

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
BASE = "https://google.serper.dev"


async def web_search(query: str, num: int = 5) -> dict:
    """Text search: returns titles, links, and snippets."""
    if not SERPER_API_KEY:
        return {"ok": False, "error": "Search is not configured (missing SERPER_API_KEY)."}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{BASE}/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": num},
            )
        data = r.json()
        results = [
            {"title": item.get("title"), "link": item.get("link"), "snippet": item.get("snippet")}
            for item in data.get("organic", [])[:num]
        ]
        return {"ok": True, "results": results}
    except Exception as e:
        print(f"[search] web_search error: {e!r}")
        return {"ok": False, "error": "Search failed. Try again in a moment."}


async def image_search(query: str, num: int = 4) -> dict:
    """Image search: returns real image URLs (not AI-generated) for a query,
    e.g. a product name. The backend attaches these to the chat response so
    the frontend can render them -- the model never has to paste raw URLs."""
    if not SERPER_API_KEY:
        return {"ok": False, "error": "Image search is not configured (missing SERPER_API_KEY)."}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{BASE}/images",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": num},
            )
        data = r.json()
        images = [
            {"title": item.get("title"), "image_url": item.get("imageUrl"), "source": item.get("source")}
            for item in data.get("images", [])[:num]
            if item.get("imageUrl")
        ]
        if not images:
            return {"ok": False, "error": "No images found for that query."}
        return {"ok": True, "images": images}
    except Exception as e:
        print(f"[search] image_search error: {e!r}")
        return {"ok": False, "error": "Image search failed. Try again in a moment."}
