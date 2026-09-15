"""Tests for doc_resolver.py — centralized doc fetch/dedup/cache/render."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agentic_qa.core.doc_resolver import resolve_platform_docs
from agentic_qa.core.models import DocSubsystem, ServiceDescriptor


def _svc(name: str, doc_links: list[str] | None = None) -> ServiceDescriptor:
    return ServiceDescriptor(name=name, repo_url=f"https://github.com/org/{name}", doc_links=doc_links or [])


@pytest.mark.asyncio
async def test_resolve_platform_docs_dedups_urls_across_scopes():
    shared_url = "https://wiki.internal/shared"
    services = [_svc("auth", [shared_url])]
    subsystems = [DocSubsystem(name="sub", services=["auth"], docs=[shared_url])]

    with patch(
        "agentic_qa.core.doc_resolver.async_fetch_url", new=AsyncMock(return_value="content")
    ) as mock_fetch:
        await resolve_platform_docs(services=services, system_docs=[shared_url], subsystems=subsystems)

    mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_platform_docs_per_service_text_only_contains_own_links():
    services = [_svc("auth", ["https://wiki/auth"]), _svc("web", ["https://wiki/web"])]

    with patch("agentic_qa.core.doc_resolver.async_fetch_url", new=AsyncMock(side_effect=lambda url, max_chars: f"content-of-{url}")):
        resolved = await resolve_platform_docs(services=services, system_docs=[], subsystems=[])

    assert "content-of-https://wiki/auth" in resolved.per_service_text["auth"]
    assert "content-of-https://wiki/web" not in resolved.per_service_text["auth"]
    assert "content-of-https://wiki/web" in resolved.per_service_text["web"]


@pytest.mark.asyncio
async def test_resolve_platform_docs_synthesizer_text_excludes_service_only_docs():
    services = [_svc("auth", ["https://wiki/auth-only"])]

    with patch("agentic_qa.core.doc_resolver.async_fetch_url", new=AsyncMock(return_value="secret-service-content")):
        resolved = await resolve_platform_docs(services=services, system_docs=[], subsystems=[])

    assert "secret-service-content" not in resolved.synthesizer_text


@pytest.mark.asyncio
async def test_resolve_platform_docs_synthesizer_text_tags_subsystem_services():
    services = [_svc("auth"), _svc("billing")]
    subsystems = [DocSubsystem(name="payments-subsystem", services=["auth", "billing"], docs=["https://wiki/payments"])]

    with patch("agentic_qa.core.doc_resolver.async_fetch_url", new=AsyncMock(return_value="payments doc content")):
        resolved = await resolve_platform_docs(services=services, system_docs=[], subsystems=subsystems)

    assert "payments-subsystem" in resolved.synthesizer_text
    assert "auth" in resolved.synthesizer_text and "billing" in resolved.synthesizer_text
    assert "payments doc content" in resolved.synthesizer_text


@pytest.mark.asyncio
async def test_resolve_platform_docs_reuses_existing_cache():
    services = [_svc("auth", ["https://wiki/auth"])]

    with patch(
        "agentic_qa.core.doc_resolver.async_fetch_url", new=AsyncMock(return_value="should-not-be-called")
    ) as mock_fetch:
        resolved = await resolve_platform_docs(
            services=services,
            system_docs=[],
            subsystems=[],
            existing_cache={"https://wiki/auth": "cached content"},
        )

    mock_fetch.assert_not_called()
    assert "cached content" in resolved.per_service_text["auth"]


@pytest.mark.asyncio
async def test_resolve_platform_docs_error_string_passthrough():
    services = [_svc("auth", ["https://wiki/broken"])]

    with patch(
        "agentic_qa.core.doc_resolver.async_fetch_url",
        new=AsyncMock(return_value="[error] Failed to fetch https://wiki/broken: timeout"),
    ):
        resolved = await resolve_platform_docs(services=services, system_docs=[], subsystems=[])

    assert "[error]" in resolved.per_service_text["auth"]
    assert resolved.cache["https://wiki/broken"].startswith("[error]")
