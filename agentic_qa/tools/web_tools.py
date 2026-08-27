from __future__ import annotations

import httpx


async def async_fetch_url(url: str, max_chars: int = 8000) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers={"User-Agent": "agentic-qa/0.1"})
            resp.raise_for_status()
            text = resp.text
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars] ..."
            return text
    except Exception as exc:
        return f"[error] Failed to fetch {url}: {exc}"
