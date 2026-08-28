"""Tests for platform_init.py — detect_role heuristics and YAML generation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_qa.core.platform_init import (
    _derive_service_name,
    detect_role,
    generate_platform_yaml,
)


# ── detect_role ───────────────────────────────────────────────────────────────

def _make_dir(tmp_path: Path, files: list[str], dirs: list[str] | None = None) -> Path:
    """Create a fake repo root with the given files and subdirectories."""
    for f in files:
        (tmp_path / f).touch()
    for d in dirs or []:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_detect_role_infra_terraform_dir(tmp_path):
    root = _make_dir(tmp_path, [], dirs=["terraform"])
    role, reason = detect_role(root)
    assert role == "infra"
    assert "terraform" in reason


def test_detect_role_infra_kubernetes(tmp_path):
    root = _make_dir(tmp_path, [], dirs=["kubernetes"])
    role, reason = detect_role(root)
    assert role == "infra"


def test_detect_role_infra_tf_file(tmp_path):
    _make_dir(tmp_path, ["main.tf"])
    role, _ = detect_role(tmp_path)
    assert role == "infra"


def test_detect_role_frontend_react(tmp_path):
    pkg = {"dependencies": {"react": "^18", "react-dom": "^18"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    role, reason = detect_role(tmp_path)
    assert role == "frontend"
    assert "react" in reason


def test_detect_role_frontend_next(tmp_path):
    pkg = {"dependencies": {"next": "^14"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    role, reason = detect_role(tmp_path)
    assert role == "frontend"
    assert "next" in reason


def test_detect_role_frontend_vite_config(tmp_path):
    pkg = {"dependencies": {"vite": "^5"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "vite.config.ts").touch()
    role, reason = detect_role(tmp_path)
    # vite alone is not a frontend signal via deps, but vite.config.* file is
    assert role == "frontend"


def test_detect_role_node_backend(tmp_path):
    pkg = {"dependencies": {"express": "^4"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    role, reason = detect_role(tmp_path)
    assert role == "backend"
    assert "express" in reason.lower() or "backend" in reason.lower()


def test_detect_role_python_backend_pyproject(tmp_path):
    _make_dir(tmp_path, ["pyproject.toml"])
    role, reason = detect_role(tmp_path)
    assert role == "backend"
    assert "pyproject.toml" in reason


def test_detect_role_python_backend_requirements(tmp_path):
    _make_dir(tmp_path, ["requirements.txt"])
    role, reason = detect_role(tmp_path)
    assert role == "backend"


def test_detect_role_go_backend(tmp_path):
    _make_dir(tmp_path, ["go.mod"])
    role, _ = detect_role(tmp_path)
    assert role == "backend"


def test_detect_role_rust_backend(tmp_path):
    _make_dir(tmp_path, ["Cargo.toml"])
    role, _ = detect_role(tmp_path)
    assert role == "backend"


def test_detect_role_default_empty(tmp_path):
    role, reason = detect_role(tmp_path)
    assert role == "backend"
    assert "default" in reason.lower()


def test_detect_role_nonexistent_path():
    role, reason = detect_role("/this/path/does/not/exist")
    assert role == "backend"
    assert "not found" in reason.lower()


# ── _derive_service_name ──────────────────────────────────────────────────────

def test_derive_name_github_url():
    assert _derive_service_name("https://github.com/org/auth-service") == "auth-service"


def test_derive_name_git_suffix():
    assert _derive_service_name("https://github.com/org/payments.git") == "payments"


def test_derive_name_local_path():
    assert _derive_service_name("/workspace/todo-app/backend") == "backend"


def test_derive_name_trailing_slash():
    assert _derive_service_name("https://github.com/org/web/") == "web"


# ── generate_platform_yaml ────────────────────────────────────────────────────

def test_generate_yaml_contains_name():
    yaml = generate_platform_yaml("my-platform", [], [])
    assert "name: my-platform" in yaml


def test_generate_yaml_services():
    services = [
        {"name": "api", "url": "https://github.com/org/api", "role": "backend",
         "branch": "main", "reason": "pyproject.toml (Python)"},
        {"name": "web", "url": "https://github.com/org/web", "role": "frontend",
         "branch": "main", "reason": "react"},
    ]
    yaml = generate_platform_yaml("demo", services, [])
    assert "name: api" in yaml
    assert "role: backend" in yaml
    assert "role: frontend" in yaml
    assert "pyproject.toml" in yaml   # comment preserved
    assert "react" in yaml


def test_generate_yaml_docs():
    yaml = generate_platform_yaml("demo", [], ["https://wiki/arch"])
    assert "docs:" in yaml
    assert "https://wiki/arch" in yaml


def test_generate_yaml_no_docs_has_comment():
    yaml = generate_platform_yaml("demo", [], [])
    assert "# docs:" in yaml


def test_generate_yaml_name_quoting():
    yaml = generate_platform_yaml("my platform: demo", [], [])
    # Name with special chars should be quoted
    assert '"my platform: demo"' in yaml
