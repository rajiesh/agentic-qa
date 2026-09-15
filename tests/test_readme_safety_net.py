"""
Tests for the dynamic, self-authored README safety net.

README content is authored by the model itself (framework-agnostic — no static template),
based on whatever it actually wrote. BaseAgent._ensure_readme is only a guarantee that the
model gets one more nudge if it forgot; these tests verify that guarantee without asserting
anything about README *content*.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agentic_qa.agents.base_agent import BaseAgent
from agentic_qa.agents.specialists.contract import ContractTestAgent
from agentic_qa.agents.specialists.functional import FunctionalTestAgent
from agentic_qa.core.models import (
    ContractTestEntry,
    GeneratedTestFile,
    ServiceContract,
    TechStack,
    TestPlanEntry,
    TestScope,
)
from agentic_qa.core.output_manager import OutputManager

_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _fake_config() -> MagicMock:
    cfg = MagicMock()
    cfg.model = "claude-sonnet-4-6"
    cfg.max_tokens_specialist = 16384
    cfg.max_retries = 0
    cfg.retry_base_wait_secs = 0.0
    cfg.retry_max_wait_secs = 0.0
    cfg.max_context_tool_pairs = 10
    return cfg


def _plan_entry() -> TestPlanEntry:
    return TestPlanEntry(
        test_type="functional",
        priority="high",
        scope=TestScope(description="cover the API"),
        suggested_framework="pytest",
        rationale="needs coverage",
    )


def _gtf(
    filename: str, framework: str = "pytest", test_type: str = "functional"
) -> GeneratedTestFile:
    return GeneratedTestFile(
        filename=filename, content="x", test_type=test_type, framework=framework,
        description="x",
    )


class TestReadmePresent:
    def test_empty_list(self):
        assert BaseAgent._readme_present([]) is False

    def test_present_case_insensitive(self):
        assert BaseAgent._readme_present([_gtf("Readme.MD", "markdown")]) is True

    def test_absent_among_other_files(self):
        assert BaseAgent._readme_present([_gtf("test_x.py")]) is False

    def test_subdir_scoping(self):
        files = [_gtf("contracts/a-b/consumer/README.md", "markdown", "contract")]
        assert BaseAgent._readme_present(files, subdir="contracts/a-b/consumer") is True
        assert BaseAgent._readme_present(files, subdir="contracts/a-b/provider") is False


@pytest.mark.asyncio
class TestEnsureReadmeSafetyNet:
    async def test_nudges_when_readme_missing(self, tmp_path):
        om = OutputManager(base_dir=str(tmp_path), repo_name="repo", run_id="run")
        agent = FunctionalTestAgent(
            client=MagicMock(), config=_fake_config(), agent_id="functional-test"
        )

        calls = {"n": 0}

        async def run_loop_side_effect(user_message, max_iterations=20, extra_messages=None):
            calls["n"] += 1
            if calls["n"] == 1:
                agent._generated_files.append(_gtf("test_x.py"))
            else:
                agent._generated_files.append(_gtf("README.md", "markdown"))
            return "", dict(_USAGE)

        with patch.object(agent, "_run_loop", side_effect=run_loop_side_effect):
            result = await agent.run(
                plan_entry=_plan_entry(),
                tech_stack=TechStack(),
                repo_local_path="/tmp/repo",
                output_manager=om,
            )

        assert calls["n"] == 2
        assert any(f.filename.lower() == "readme.md" for f in result.generated_files)

    async def test_no_nudge_when_readme_already_written(self, tmp_path):
        om = OutputManager(base_dir=str(tmp_path), repo_name="repo", run_id="run")
        agent = FunctionalTestAgent(
            client=MagicMock(), config=_fake_config(), agent_id="functional-test"
        )

        calls = {"n": 0}

        async def run_loop_side_effect(user_message, max_iterations=20, extra_messages=None):
            calls["n"] += 1
            agent._generated_files.append(_gtf("test_x.py"))
            agent._generated_files.append(_gtf("README.md", "markdown"))
            return "", dict(_USAGE)

        with patch.object(agent, "_run_loop", side_effect=run_loop_side_effect):
            await agent.run(
                plan_entry=_plan_entry(),
                tech_stack=TechStack(),
                repo_local_path="/tmp/repo",
                output_manager=om,
            )

        assert calls["n"] == 1

    async def test_no_nudge_when_no_files_generated(self, tmp_path):
        om = OutputManager(base_dir=str(tmp_path), repo_name="repo", run_id="run")
        agent = FunctionalTestAgent(
            client=MagicMock(), config=_fake_config(), agent_id="functional-test"
        )

        calls = {"n": 0}

        async def run_loop_side_effect(user_message, max_iterations=20, extra_messages=None):
            calls["n"] += 1
            return "", dict(_USAGE)

        with patch.object(agent, "_run_loop", side_effect=run_loop_side_effect):
            await agent.run(
                plan_entry=_plan_entry(),
                tech_stack=TechStack(),
                repo_local_path="/tmp/repo",
                output_manager=om,
            )

        assert calls["n"] == 1


@pytest.mark.asyncio
class TestContractPerSubdirSafetyNet:
    def _entry(self) -> ContractTestEntry:
        return ContractTestEntry(
            contract=ServiceContract(
                consumer="web", provider="api", contract_type="rest", endpoints=["/todos"]
            ),
            consumer_framework="pact-python",
            provider_framework="@pact-foundation/pact",
        )

    async def test_nudges_only_for_missing_subdir(self, tmp_path):
        om = OutputManager(base_dir=str(tmp_path), repo_name="contracts", run_id="run")
        agent = ContractTestAgent(
            client=MagicMock(), config=_fake_config(), agent_id="contract-test"
        )

        calls = {"n": 0}
        nudged_subdirs: list[str] = []
        consumer_dir = "contracts/web-api/consumer"
        provider_dir = "contracts/web-api/provider"

        async def run_loop_side_effect(user_message, max_iterations=20, extra_messages=None):
            calls["n"] += 1
            if calls["n"] == 1:
                agent._generated_files.append(
                    _gtf(f"{consumer_dir}/test_api_contract.py", "pact-python", "contract")
                )
                agent._generated_files.append(
                    _gtf(f"{consumer_dir}/README.md", "markdown", "contract")
                )
                agent._generated_files.append(
                    _gtf(
                        f"{provider_dir}/api.provider.spec.ts",
                        "@pact-foundation/pact", "contract",
                    )
                )
                # provider README intentionally omitted
            else:
                nudged_subdirs.append(user_message)
                agent._generated_files.append(
                    _gtf(f"{provider_dir}/README.md", "markdown", "contract")
                )
            return "", dict(_USAGE)

        with patch.object(agent, "_run_loop", side_effect=run_loop_side_effect):
            result = await agent.run(
                entry=self._entry(),
                service_paths={"consumer": "/tmp/web", "provider": "/tmp/api"},
                output_manager=om,
            )

        assert calls["n"] == 2  # one main call + exactly one nudge (provider only)
        assert "provider" in nudged_subdirs[0]
        assert BaseAgent._readme_present(result.generated_files, subdir=consumer_dir)
        assert BaseAgent._readme_present(result.generated_files, subdir=provider_dir)
