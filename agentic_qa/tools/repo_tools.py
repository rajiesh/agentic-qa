from __future__ import annotations

import asyncio
import fnmatch
import subprocess
from pathlib import Path


async def async_read_file(
    path: str,
    repo_root: str = ".",
    max_lines: int = 300,
    offset: int = 0,
) -> str:
    """Read up to *max_lines* lines from *path* starting at line *offset* (0-indexed).

    If the file has more lines beyond the window a hint is appended so the agent
    can page forward with a follow-up call using the next offset value.
    """
    full = Path(repo_root) / path
    if not full.exists():
        return f"[error] File not found: {path}"
    if not full.is_file():
        return f"[error] Not a file: {path}"
    try:
        text = await asyncio.to_thread(full.read_text, errors="replace")
        lines = text.splitlines()
        total = len(lines)

        # Clamp offset to valid range
        offset = max(0, min(offset, total))
        window = lines[offset : offset + max_lines]

        remaining = total - (offset + len(window))
        if remaining > 0:
            next_offset = offset + max_lines
            window.append(
                f"\n... [{remaining} more lines not shown — "
                f"call read_file with offset={next_offset} to continue] ..."
            )
        elif offset > 0:
            # At the end of the file; let the agent know
            window.append(f"\n... [end of file — {total} lines total] ...")

        return "\n".join(window)
    except Exception as exc:
        return f"[error] Could not read {path}: {exc}"


async def async_list_directory(
    path: str = ".",
    repo_root: str = ".",
    depth: int = 2,
) -> str:
    root = Path(repo_root) / path
    if not root.exists():
        return f"[error] Directory not found: {path}"

    def _build_tree(p: Path, current_depth: int, prefix: str = "") -> list[str]:
        if current_depth > depth:
            return []
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
        except PermissionError:
            return [f"{prefix}[permission denied]"]
        lines = []
        for i, entry in enumerate(entries):
            connector = "└── " if i == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and current_depth < depth:
                extension = "    " if i == len(entries) - 1 else "│   "
                lines.extend(_build_tree(entry, current_depth + 1, prefix + extension))
        return lines

    lines = [str(root)] + _build_tree(root, 1)
    return "\n".join(lines)


async def async_search_code(
    pattern: str,
    repo_root: str = ".",
    file_glob: str = "**/*",
    max_results: int = 20,
) -> str:
    # Prefer ripgrep if available, fall back to Python grep
    rg = await asyncio.to_thread(_try_ripgrep, pattern, repo_root, max_results)
    if rg is not None:
        return rg

    root = Path(repo_root)
    matches: list[str] = []

    def _search() -> list[str]:
        results = []
        for filepath in root.glob(file_glob):
            if not filepath.is_file():
                continue
            try:
                text = filepath.read_text(errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.lower() in line.lower():
                        rel = filepath.relative_to(root)
                        results.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_results:
                            return results
            except Exception:
                continue
        return results

    matches = await asyncio.to_thread(_search)
    if not matches:
        return f"[no results] Pattern '{pattern}' not found."
    return "\n".join(matches)


def _try_ripgrep(pattern: str, repo_root: str, max_results: int) -> str | None:
    try:
        result = subprocess.run(
            ["rg", "--line-number", "--max-count", str(max_results), pattern],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode in (0, 1):
            return result.stdout or f"[no results] Pattern '{pattern}' not found."
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
