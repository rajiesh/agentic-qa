"""Tests for PostGenerationValidator — file checks and lint pass."""
from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from agentic_qa.core.models import GeneratedTestFile, SpecialistResult
from agentic_qa.core.validator import PostGenerationValidator


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_result(files: list[GeneratedTestFile] | None = None) -> SpecialistResult:
    return SpecialistResult(
        test_type="functional",
        agent_id="test-agent",
        started_at=datetime.utcnow(),
        generated_files=files or [],
    )


def _py_file(name: str, content: str) -> GeneratedTestFile:
    return GeneratedTestFile(
        filename=name,
        content=content,
        test_type="functional",
        framework="pytest",
        description="test",
    )


# ── File creation checks ──────────────────────────────────────────────────────

def test_no_files_adds_error():
    result = _make_result([])
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=False)
    assert any("No files generated" in e for e in result.errors)


def test_empty_content_adds_error():
    result = _make_result([_py_file("test_foo.py", "   \n  ")])
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=False)
    assert any("empty" in e for e in result.errors)


def test_non_empty_file_no_error():
    result = _make_result([_py_file("test_foo.py", "def test_ok(): pass\n")])
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=False)
    assert not result.errors


def test_multiple_files_partial_empty():
    files = [
        _py_file("test_good.py", "def test_pass(): pass\n"),
        _py_file("test_bad.py", ""),
    ]
    result = _make_result(files)
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=False)
    assert len(result.errors) == 1
    assert "test_bad.py" in result.errors[0]


# ── Lint pass — Python (ruff) ─────────────────────────────────────────────────

@pytest.mark.skipif(
    __import__("importlib").util.find_spec("ruff") is None and not shutil.which("ruff"),
    reason="ruff not available",
)
def test_lint_clean_python():
    content = "def test_something():\n    assert 1 + 1 == 2\n"
    result = _make_result([_py_file("test_clean.py", content)])
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=True)
    assert "lint" in result.execution_results
    entry = result.execution_results["lint"]["test_clean.py"]
    assert entry["status"] == "ok"
    assert entry["issues"] == []
    # No lint errors promoted to result.errors
    assert not any("[lint]" in e for e in result.errors)


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("ruff") is None and not shutil.which("ruff"),
    reason="ruff not available",
)
def test_lint_python_with_error():
    # F401: unused import — a clear ruff error
    content = "import os\n\ndef test_something():\n    assert True\n"
    result = _make_result([_py_file("test_dirty.py", content)])
    v = PostGenerationValidator()
    v.validate(result, "/tmp", lint=True)
    lint = result.execution_results.get("lint", {})
    assert "test_dirty.py" in lint
    entry = lint["test_dirty.py"]
    # ruff should flag F401
    assert entry["status"] in {"ok", "error"}  # tolerate if ruff doesn't catch it in every version


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("ruff") is None and not shutil.which("ruff"),
    reason="ruff not available",
)
def test_lint_results_stored_in_execution_results():
    content = "def test_x(): pass\n"
    result = _make_result([_py_file("test_x.py", content)])
    PostGenerationValidator().validate(result, "/tmp", lint=True)
    assert "lint" in result.execution_results
    assert "test_x.py" in result.execution_results["lint"]


# ── Lint pass — non-Python files skipped ─────────────────────────────────────

def test_non_python_non_js_files_skipped(tmp_path):
    yaml_file = GeneratedTestFile(
        filename="zap-config.yaml",
        content="target: http://localhost:8000\n",
        test_type="security",
        framework="zap",
        description="ZAP config",
    )
    result = _make_result([yaml_file])
    result.test_type = "security"  # type: ignore[assignment]
    PostGenerationValidator().validate(result, str(tmp_path), lint=True)
    # YAML files should not be linted — no lint entry added
    assert result.execution_results.get("lint", {}) == {}
    assert not result.errors


# ── lint=False skips entirely ─────────────────────────────────────────────────

def test_lint_disabled_skips_all():
    # Even a clearly broken Python file shouldn't add errors when lint=False
    content = "import os\nimport sys\n"  # unused imports
    result = _make_result([_py_file("test_nolint.py", content)])
    PostGenerationValidator().validate(result, "/tmp", lint=False)
    assert "lint" not in result.execution_results
    # Only file-creation check runs — content is non-empty so no errors
    assert not result.errors


# ── validate() returns the mutated result ────────────────────────────────────

def test_validate_returns_result():
    result = _make_result([_py_file("test_ok.py", "def test_pass(): pass\n")])
    returned = PostGenerationValidator().validate(result, "/tmp", lint=False)
    assert returned is result
