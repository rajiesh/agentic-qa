"""
platform_init.py — Bootstrap helper for platform.yaml generation.

Provides:
  detect_role(local_path)         — heuristic role detection from top-level files
  generate_platform_yaml(...)     — render a commented, human-readable platform.yaml string
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import ServiceRole


# ── Role detection ─────────────────────────────────────────────────────────────

def detect_role(local_path: str | Path) -> tuple[ServiceRole, str]:
    """
    Inspect the top level of *local_path* and return (role, reason).

    Detection priority (first match wins):
      infra    — terraform/, kubernetes/, k8s/, helm/ dirs, or *.tf at root
      frontend — package.json AND (react/vue/angular/svelte/next/nuxt dependency
                 OR next.config.*/vite.config.*/webpack.config.* at root
                 OR pages/ or public/ dirs at root)
      backend  — package.json without frontend signals (Node backend)
                 OR any of: pyproject.toml, requirements.txt, setup.py, setup.cfg,
                            go.mod, pom.xml, Cargo.toml, build.gradle, build.sbt
      default  — backend
    """
    root = Path(local_path)
    if not root.exists():
        return "backend", "path not found — defaulting to backend"

    try:
        children = list(root.iterdir())
    except PermissionError:
        return "backend", "permission error reading directory"

    entry_names = {p.name for p in children}
    dir_names   = {p.name for p in children if p.is_dir()}

    # ── infra ────────────────────────────────────────────────────────────────
    infra_dirs = {"terraform", "kubernetes", "k8s", "helm", "ansible", "charts"}
    if dir_names & infra_dirs:
        found = ", ".join(sorted(dir_names & infra_dirs))
        return "infra", f"directory found: {found}/"

    if any(root.glob("*.tf")):
        return "infra", "*.tf files at root"

    # ── package.json present → JavaScript / TypeScript project ───────────────
    if "package.json" in entry_names:
        try:
            pkg = json.loads((root / "package.json").read_text())
        except Exception:
            pkg = {}

        all_deps: set[str] = set()
        all_deps.update(pkg.get("dependencies", {}).keys())
        all_deps.update(pkg.get("devDependencies", {}).keys())

        frontend_pkg_signals = {"react", "vue", "@angular/core", "svelte", "next", "nuxt",
                                "@nuxt/core", "solid-js", "preact", "qwik"}
        frontend_file_globs = ["next.config.*", "vite.config.*", "webpack.config.*",
                               "angular.json", "svelte.config.*", "nuxt.config.*"]
        frontend_dirs = {"pages", "public", "src/components", "components", "views"}

        if all_deps & frontend_pkg_signals:
            matched = ", ".join(sorted(all_deps & frontend_pkg_signals))
            return "frontend", f"package.json dependency: {matched}"

        config_matches = [m for g in frontend_file_globs for m in root.glob(g)]
        if config_matches:
            return "frontend", f"config file: {config_matches[0].name}"

        if dir_names & frontend_dirs:
            found = ", ".join(sorted(dir_names & frontend_dirs))
            return "frontend", f"directory: {found}/"

        # JS project without frontend signals → Node backend
        backend_pkg = {"express", "fastify", "koa", "hapi", "@nestjs/core", "restify",
                       "feathers", "moleculer"}
        if all_deps & backend_pkg:
            matched = ", ".join(sorted(all_deps & backend_pkg))
            return "backend", f"Node backend dependency: {matched}"

        return "backend", "package.json without frontend signals"

    # ── language-specific backend markers ─────────────────────────────────────
    backend_markers = {
        "pyproject.toml": "pyproject.toml (Python)",
        "requirements.txt": "requirements.txt (Python)",
        "setup.py": "setup.py (Python)",
        "setup.cfg": "setup.cfg (Python)",
        "go.mod": "go.mod (Go)",
        "pom.xml": "pom.xml (Java/Maven)",
        "build.gradle": "build.gradle (Java/Kotlin/Gradle)",
        "build.sbt": "build.sbt (Scala)",
        "Cargo.toml": "Cargo.toml (Rust)",
        "mix.exs": "mix.exs (Elixir)",
        "composer.json": "composer.json (PHP)",
    }
    for filename, reason in backend_markers.items():
        if filename in entry_names:
            return "backend", reason

    # ── docs-only repo ────────────────────────────────────────────────────────
    code_extensions = {".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".php",
                       ".cs", ".cpp", ".c", ".h", ".swift", ".kt"}
    file_extensions = {p.suffix for p in children if p.is_file()}
    if file_extensions and not (file_extensions & code_extensions):
        doc_exts = {".md", ".rst", ".txt", ".adoc", ".html"}
        if file_extensions <= doc_exts:
            return "docs", "only documentation files at root"

    return "backend", "no specific signals — defaulting to backend"


def _derive_service_name(url: str) -> str:
    """Turn a repo URL or local path into a short service name."""
    # Strip trailing slash and .git suffix
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return Path(url).name or url


# ── YAML generation ───────────────────────────────────────────────────────────

def generate_platform_yaml(
    platform_name: str,
    services: list[dict],   # each: {name, url, role, branch, reason}
    global_docs: list[str],
) -> str:
    """
    Render a commented, human-readable platform.yaml.
    Uses string building (not yaml.dump) to preserve inline comments.
    """
    lines: list[str] = [
        "# Generated by `agentic-qa init-platform` — review and edit before committing.",
        "# Docs: https://github.com/your-org/agentic-qa#platformyaml",
        "",
        f"name: {_quote(platform_name)}",
        "",
        "services:",
    ]

    for svc in services:
        name   = svc["name"]
        url    = svc["url"]
        role   = svc["role"]
        branch = svc.get("branch", "main")
        reason = svc.get("reason", "")

        role_comment = f"  # detected: {reason}" if reason else ""
        lines += [
            f"  - name: {_quote(name)}",
            f"    url: {url}",
            f"    role: {role}{role_comment}",
            f"    branch: {branch}",
            f"    # doc_links: []",
            f"    # sparse_paths: []",
        ]

    if global_docs:
        lines += ["", "docs:"]
        for doc in global_docs:
            lines.append(f"  - {doc}")
    else:
        lines += [
            "",
            "# docs:",
            "#   - https://your-wiki/architecture",
        ]

    lines.append("")
    return "\n".join(lines)


def _quote(s: str) -> str:
    """Quote a YAML scalar only if it contains special characters."""
    need_quote = any(c in s for c in ' :{}[]#&*!|>\'"%@`')
    return f'"{s}"' if need_quote else s


# ── Async helper for CLI ──────────────────────────────────────────────────────

async def detect_all_roles(
    repo_urls: list[str],
    output_dir: str = "outputs",
    max_repo_size_mb: int = 500,
) -> list[dict]:
    """
    Shallow-clone all repos in parallel and detect their roles.
    Returns a list of service dicts ready for generate_platform_yaml().

    Does NOT require an Anthropic API key — only git and filesystem access.
    """
    import asyncio

    from ..config import RepoTarget
    from .repo_ingestor import RepoIngestor

    class _MinimalConfig:
        """Minimal config stub for RepoIngestor — no API key required."""
        def __init__(self, output_dir: str, max_repo_size_mb: int) -> None:
            self.output_dir = output_dir
            self.max_repo_size_mb = max_repo_size_mb

    ingestor = RepoIngestor(_MinimalConfig(output_dir, max_repo_size_mb))  # type: ignore[arg-type]

    async def _process(url: str) -> dict:
        name = _derive_service_name(url)
        try:
            local_path = await ingestor.prepare(RepoTarget(url=url))
            role, reason = detect_role(local_path)
        except Exception as exc:
            role, reason = "backend", f"clone failed ({exc}) — defaulting to backend"
            local_path = None

        return {
            "name": name,
            "url": url,
            "role": role,
            "branch": "main",
            "reason": reason,
            "local_path": str(local_path) if local_path else "",
        }

    # Bound clone concurrency to avoid hammering git hosts with 50+ simultaneous clones
    clone_sem = asyncio.Semaphore(10)

    async def _bounded_process(url: str) -> dict:
        async with clone_sem:
            return await _process(url)

    return list(await asyncio.gather(*[_bounded_process(url) for url in repo_urls]))
