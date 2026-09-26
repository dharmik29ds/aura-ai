"""Search helpers for Aura AI."""
import html
import re
import urllib.parse
from typing import Any
import httpx

async def web_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []

    max_results = max(1, min(int(max_results or 5), 10))
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AuraAI/1.0)"}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
        )
        response.raise_for_status()

    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )

    results = []
    for href, title_html in pattern.findall(response.text):
        title = html.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        href = html.unescape(href)

        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("uddg"):
            href = params["uddg"][0]

        if title and href:
            results.append({"title": title, "url": href, "snippet": ""})

        if len(results) >= max_results:
            break

    return results

async def search(query: str, max_results: int = 5):
    return await web_search(query, max_results)

async def search_web(query: str, max_results: int = 5):
    return await web_search(query, max_results)

async def run(query: str, max_results: int = 5):
    return await web_search(query, max_results)
