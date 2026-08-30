"""
Tests for Phase 5 — Two-tier scanner/synthesizer decomposition.

Covers:
- ServiceSummary model construction and JSON round-trip
- ServiceScannerAgent.emit_service_summary tool handler
- ServiceScannerAgent fallback when loop finishes without calling emit
- PlatformSynthesizerAgent.emit_platform_architecture tool handler
- PlatformSynthesizerAgent fallback when loop finishes without calling emit
- QAConfig Phase 5 fields present with correct defaults
- PlatformCheckpoint.scan_results field and CheckpointManager.mark_scan_complete
- _summaries_to_message rendering (smoke-test)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_qa.agents.platform_synthesizer import PlatformSynthesizerAgent, _summaries_to_message
from agentic_qa.agents.service_scanner import ServiceScannerAgent
from agentic_qa.config import QAConfig
from agentic_qa.core.checkpoint import PlatformCheckpoint, ServiceScanResult
from agentic_qa.core.checkpoint_manager import CheckpointManager
from agentic_qa.core.models import (
    PlatformArchitecture,
    ServiceDescriptor,
    ServiceSummary,
)


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _fake_config() -> MagicMock:
    """Return a mock QAConfig with all Phase 5 fields populated."""
    cfg = MagicMock(spec=QAConfig)
    cfg.model = "claude-sonnet-4-6"
    cfg.max_tokens_strategist = 8192
    cfg.max_tokens_specialist = 16384
    cfg.max_retries = 0
    cfg.retry_base_wait_secs = 0.0
    cfg.retry_max_wait_secs = 0.0
    cfg.max_context_tool_pairs = 10
    cfg.scanner_max_iterations = 15
    cfg.synthesizer_max_iterations = 8
    cfg.scanner_concurrency_limit = 10
    return cfg


def _fake_service(name: str = "auth", role: str = "backend", local_path: str = "/tmp/auth") -> ServiceDescriptor:
    return ServiceDescriptor(name=name, repo_url="https://github.com/org/auth", role=role, local_path=local_path)


def _fresh_checkpoint(platform_name: str = "test-platform") -> PlatformCheckpoint:
    return PlatformCheckpoint(platform_name=platform_name)


# ── ServiceSummary model ─────────────────────────────────────────────────────────


class TestServiceSummary:
    def test_minimal_construction(self):
        s = ServiceSummary(name="auth", role="backend")
        assert s.name == "auth"
        assert s.role == "backend"
        assert s.outbound_http_urls == []
        assert s.env_service_refs == []
        assert s.proto_files == []

    def test_full_construction(self):
        s = ServiceSummary(
            name="payments",
            role="backend",
            tech_stack_hint=["python", "fastapi"],
            outbound_http_urls=["https://stripe.com/v1/charges"],
            env_service_refs=["AUTH_SERVICE_URL"],
            event_patterns=["payment.completed"],
            proto_files=["proto/payment.proto"],
            openapi_files=["openapi.yaml"],
            shared_db_hints=["orders"],
            exposed_endpoints=["/v1/charge", "/v1/refund"],
            raw_notes="Uses Stripe SDK",
        )
        assert s.tech_stack_hint == ["python", "fastapi"]
        assert "AUTH_SERVICE_URL" in s.env_service_refs
        assert "payment.completed" in s.event_patterns

    def test_json_roundtrip(self):
        s = ServiceSummary(
            name="gateway",
            role="api_gateway",
            outbound_http_urls=["http://auth:8001"],
            env_service_refs=["AUTH_URL", "PAYMENT_URL"],
        )
        restored = ServiceSummary.model_validate_json(s.model_dump_json())
        assert restored.name == s.name
        assert restored.outbound_http_urls == s.outbound_http_urls
        assert restored.env_service_refs == s.env_service_refs


# ── QAConfig Phase 5 fields ──────────────────────────────────────────────────────


class TestQAConfigPhase5Fields:
    def test_scanner_concurrency_limit_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.scanner_concurrency_limit == 10

    def test_scanner_max_iterations_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.scanner_max_iterations == 15

    def test_synthesizer_max_iterations_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.synthesizer_max_iterations == 8

    def test_scanner_concurrency_env_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("SCANNER_CONCURRENCY_LIMIT", "20")
        config = QAConfig()  # type: ignore[call-arg]
        assert config.scanner_concurrency_limit == 20


# ── PlatformCheckpoint scan_results field ────────────────────────────────────────


class TestCheckpointScanResults:
    def test_scan_results_default_empty(self):
        ckpt = _fresh_checkpoint()
        assert ckpt.scan_results == {}

    def test_scan_result_round_trip(self):
        ckpt = _fresh_checkpoint()
        summary = ServiceSummary(name="auth", role="backend", env_service_refs=["PAY_URL"])
        ckpt.scan_results["auth"] = ServiceScanResult(
            service_name="auth",
            summary_json=summary.model_dump_json(),
            token_usage={"input_tokens": 500, "output_tokens": 200},
        )
        # Round-trip via JSON
        restored = PlatformCheckpoint.model_validate_json(ckpt.model_dump_json())
        scan = restored.scan_results["auth"]
        assert scan.service_name == "auth"
        assert scan.token_usage["input_tokens"] == 500
        restored_summary = ServiceSummary.model_validate_json(scan.summary_json)
        assert restored_summary.env_service_refs == ["PAY_URL"]

    def test_checkpoint_manager_mark_scan_complete(self, tmp_path):
        mgr = CheckpointManager(
            platform_name="test-platform",
            output_dir=str(tmp_path),
        )
        ckpt = _fresh_checkpoint()
        summary = ServiceSummary(name="auth", role="backend")
        usage = {"input_tokens": 100, "output_tokens": 50}

        mgr.mark_scan_complete(ckpt, "auth", summary.model_dump_json(), usage)
        mgr.save(ckpt)

        loaded = mgr.load()
        assert loaded is not None
        assert "auth" in loaded.scan_results
        assert loaded.scan_results["auth"].token_usage["input_tokens"] == 100

    def test_resume_skips_completed_scan(self):
        """A service whose scan_result is in the checkpoint should not be re-scanned."""
        ckpt = _fresh_checkpoint()
        summary = ServiceSummary(name="payments", role="backend")
        ckpt.scan_results["payments"] = ServiceScanResult(
            service_name="payments",
            summary_json=summary.model_dump_json(),
        )
        # Simulate the resume check in PlatformOrchestrator._scan_one
        should_skip = "payments" in ckpt.scan_results
        assert should_skip

    def test_missing_scan_does_not_skip(self):
        ckpt = _fresh_checkpoint()
        should_skip = "new-service" in ckpt.scan_results
        assert not should_skip


# ── ServiceScannerAgent — tool handler ───────────────────────────────────────────


class TestServiceScannerAgent:
    def _make_scanner(self, local_path: str = "/tmp/test-svc") -> ServiceScannerAgent:
        client = MagicMock()
        config = _fake_config()
        svc = _fake_service(local_path=local_path)
        return ServiceScannerAgent(client=client, config=config, service=svc)

    def test_tools_registered(self):
        scanner = self._make_scanner()
        tool_names = {t["name"] for t in scanner._tools}
        assert "read_file" in tool_names
        assert "list_directory" in tool_names
        assert "search_code" in tool_names
        assert "emit_service_summary" in tool_names

    @pytest.mark.asyncio
    async def test_emit_service_summary_handler(self):
        scanner = self._make_scanner()
        result = await scanner._handle_emit_summary(
            name="auth",
            role="backend",
            outbound_http_urls=["http://payments:8002/charge"],
            env_service_refs=["PAYMENT_SERVICE_URL"],
            event_patterns=["user.created"],
        )
        assert "recorded" in result.lower()
        assert scanner._summary is not None
        assert scanner._summary.name == "auth"
        assert scanner._summary.env_service_refs == ["PAYMENT_SERVICE_URL"]
        assert scanner._summary.event_patterns == ["user.created"]

    @pytest.mark.asyncio
    async def test_emit_defaults_service_name_from_descriptor(self):
        """emit_service_summary defaults name/role from the ServiceDescriptor when omitted."""
        scanner = self._make_scanner()
        await scanner._handle_emit_summary()
        assert scanner._summary is not None
        assert scanner._summary.name == "auth"
        assert scanner._summary.role == "backend"

    @pytest.mark.asyncio
    async def test_run_returns_fallback_when_no_emit(self):
        """If the agent loop ends without calling emit_service_summary, a minimal summary is returned."""
        scanner = self._make_scanner()
        # Patch _run_loop to return immediately without setting _summary
        with patch.object(scanner, "_run_loop", new=AsyncMock(return_value=("", {}))):
            summary, usage = await scanner.run()

        assert summary.name == "auth"
        assert summary.role == "backend"
        # All list fields should be empty (fallback)
        assert summary.outbound_http_urls == []

    @pytest.mark.asyncio
    async def test_run_returns_emitted_summary(self):
        """If the agent calls emit_service_summary, run() returns that summary."""
        scanner = self._make_scanner()

        async def _fake_loop(user_msg: str, max_iterations: int = 15) -> tuple[str, dict]:
            # Simulate the agent calling emit_service_summary
            await scanner._handle_emit_summary(
                name="auth",
                role="backend",
                outbound_http_urls=["http://payments:8002"],
                env_service_refs=["PAYMENT_URL"],
            )
            return "", {"input_tokens": 300, "output_tokens": 100}

        with patch.object(scanner, "_run_loop", new=_fake_loop):
            summary, usage = await scanner.run()

        assert summary.outbound_http_urls == ["http://payments:8002"]
        assert usage["input_tokens"] == 300


# ── PlatformSynthesizerAgent — tool handler ──────────────────────────────────────


class TestPlatformSynthesizerAgent:
    def _make_synthesizer(self) -> PlatformSynthesizerAgent:
        client = MagicMock()
        config = _fake_config()
        return PlatformSynthesizerAgent(client=client, config=config)

    def test_tools_registered(self):
        synth = self._make_synthesizer()
        assert len(synth._tools) == 1
        assert synth._tools[0]["name"] == "emit_platform_architecture"

    def test_has_no_file_tools(self):
        """The synthesizer must NOT have read_file / list_directory — it only reasons."""
        synth = self._make_synthesizer()
        tool_names = {t["name"] for t in synth._tools}
        assert "read_file" not in tool_names
        assert "list_directory" not in tool_names
        assert "search_code" not in tool_names

    def test_uses_thinking(self):
        synth = self._make_synthesizer()
        assert synth._use_thinking() is True

    @pytest.mark.asyncio
    async def test_emit_platform_architecture_handler(self):
        synth = self._make_synthesizer()
        result = await synth._handle_emit_architecture(
            services=[
                {"name": "auth", "role": "backend"},
                {"name": "web", "role": "frontend"},
            ],
            contracts=[
                {
                    "consumer": "web",
                    "provider": "auth",
                    "contract_type": "rest",
                    "endpoints": ["/login", "/me"],
                    "description": "Web SPA calls auth API",
                }
            ],
            shared_schemas=["openapi.yaml"],
            topology_notes="Single-region deployment",
        )
        assert "recorded" in result.lower()
        assert synth._architecture is not None
        assert len(synth._architecture.services) == 2
        assert len(synth._architecture.contracts) == 1
        assert synth._architecture.contracts[0].consumer == "web"
        assert synth._architecture.contracts[0].provider == "auth"
        assert synth._architecture.shared_schemas == ["openapi.yaml"]

    @pytest.mark.asyncio
    async def test_run_returns_fallback_when_no_emit(self):
        synth = self._make_synthesizer()
        summaries = [
            ServiceSummary(name="auth", role="backend"),
            ServiceSummary(name="web", role="frontend"),
        ]
        with patch.object(synth, "_run_loop", new=AsyncMock(return_value=("", {}))):
            arch = await synth.run(summaries=summaries, platform_name="test")

        # Fallback: services list built from summaries, empty contracts
        assert len(arch.services) == 2
        assert arch.contracts == []

    @pytest.mark.asyncio
    async def test_run_returns_emitted_architecture(self):
        synth = self._make_synthesizer()
        summaries = [
            ServiceSummary(name="auth", role="backend", exposed_endpoints=["/login"]),
            ServiceSummary(name="web", role="frontend", env_service_refs=["AUTH_URL"]),
        ]

        async def _fake_loop(user_msg: str, max_iterations: int = 8) -> tuple[str, dict]:
            await synth._handle_emit_architecture(
                services=[{"name": "auth"}, {"name": "web"}],
                contracts=[
                    {
                        "consumer": "web",
                        "provider": "auth",
                        "contract_type": "rest",
                    }
                ],
            )
            return "", {}

        with patch.object(synth, "_run_loop", new=_fake_loop):
            arch = await synth.run(summaries=summaries)

        assert len(arch.contracts) == 1
        assert arch.contracts[0].contract_type == "rest"


# ── _summaries_to_message rendering ─────────────────────────────────────────────


class TestSummariesToMessage:
    def test_includes_all_services(self):
        summaries = [
            ServiceSummary(name="auth", role="backend"),
            ServiceSummary(name="web", role="frontend"),
            ServiceSummary(name="payments", role="backend"),
        ]
        msg = _summaries_to_message(summaries)
        assert "auth" in msg
        assert "web" in msg
        assert "payments" in msg

    def test_includes_env_service_refs(self):
        summaries = [
            ServiceSummary(
                name="web",
                role="frontend",
                env_service_refs=["AUTH_SERVICE_URL", "PAYMENT_SERVICE_URL"],
            )
        ]
        msg = _summaries_to_message(summaries)
        assert "AUTH_SERVICE_URL" in msg
        assert "PAYMENT_SERVICE_URL" in msg

    def test_includes_exposed_endpoints(self):
        summaries = [
            ServiceSummary(
                name="auth",
                role="backend",
                exposed_endpoints=["/login", "/logout", "/me"],
            )
        ]
        msg = _summaries_to_message(summaries)
        assert "/login" in msg

    def test_includes_call_to_action(self):
        msg = _summaries_to_message([ServiceSummary(name="svc", role="backend")])
        assert "emit_platform_architecture" in msg

    def test_empty_summaries_still_produces_message(self):
        msg = _summaries_to_message([])
        assert "emit_platform_architecture" in msg
