"""
doc_resolver.py — Centralized pre-fetch + cache for platform-level documentation.

Fetches every unique doc URL referenced anywhere in a platform run (per-service
doc_links, top-level system docs, subsystem docs) exactly once, then shapes the
fetched text for two different consumers:
  - ServiceScannerAgent: only the doc text for that ONE service's own doc_links
  - PlatformSynthesizerAgent: system docs + subsystem docs (tagged by member services)

Per-service doc_links are never included in the synthesizer text, and system /
subsystem docs are never included in a service scanner's text — this preserves
the routing boundary between the two agents.
"""
from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from ..tools.web_tools import async_fetch_url
from .models import DocSubsystem, ServiceDescriptor

logger = logging.getLogger(__name__)


class ResolvedDocs(BaseModel):
    per_service_text: dict[str, str] = {}   # service_name -> rendered doc text (own doc_links only)
    synthesizer_text: str = ""              # combined system + subsystem block
    cache: dict[str, str] = {}              # url -> fetched text (for checkpoint persistence)


async def resolve_platform_docs(
    services: list[ServiceDescriptor],
    system_docs: list[str],
    subsystems: list[DocSubsystem],
    existing_cache: dict[str, str] | None = None,
    max_chars_per_doc: int = 8000,
    fetch_concurrency: int = 10,
) -> ResolvedDocs:
    """Dedup, fetch (once per unique URL), and render doc content for the two consumers."""
    cache: dict[str, str] = dict(existing_cache or {})

    all_urls: set[str] = set(system_docs)
    for svc in services:
        all_urls.update(svc.doc_links)
    for sub in subsystems:
        all_urls.update(sub.docs)

    to_fetch = [u for u in all_urls if u not in cache]
    sem = asyncio.Semaphore(fetch_concurrency)

    async def _fetch(url: str) -> None:
        async with sem:
            cache[url] = await async_fetch_url(url, max_chars=max_chars_per_doc)

    if to_fetch:
        logger.info(
            "doc_resolver: fetching %d new doc URL(s) (%d already cached)",
            len(to_fetch), len(all_urls) - len(to_fetch),
        )
        await asyncio.gather(*[_fetch(u) for u in to_fetch])

    def _render(urls: list[str]) -> str:
        if not urls:
            return ""
        blocks = [f"### {u}\n{cache.get(u, '[error] not fetched')}" for u in urls]
        return "\n\n".join(blocks)

    per_service_text = {svc.name: _render(svc.doc_links) for svc in services}

    synth_parts: list[str] = ["## System-wide documentation\n" + (_render(system_docs) or "None provided.")]
    for sub in subsystems:
        synth_parts.append(
            f"## Subsystem: {sub.name} (covers services: {', '.join(sub.services)})\n"
            + (_render(sub.docs) or "No docs provided for this subsystem.")
        )
    synthesizer_text = "\n\n".join(synth_parts)

    return ResolvedDocs(per_service_text=per_service_text, synthesizer_text=synthesizer_text, cache=cache)
