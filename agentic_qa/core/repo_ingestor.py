from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from ..config import QAConfig, RepoTarget

logger = logging.getLogger(__name__)


class RepoIngestor:
    def __init__(self, config: QAConfig) -> None:
        self.config = config
        self._work_dir = Path(config.output_dir) / ".clones"
        self._work_dir.mkdir(parents=True, exist_ok=True)

    async def prepare(self, target: RepoTarget) -> Path:
        """Return a local path to the repository, cloning if necessary."""
        local = Path(target.url)
        if local.exists() and local.is_dir():
            logger.info("Using local repo: %s", local)
            return local

        dest = self._work_dir / _repo_name(target.url)
        if dest.exists():
            logger.info("Repo already cloned at %s, pulling latest", dest)
            try:
                await _git(["pull", "--ff-only"], cwd=dest)
            except subprocess.CalledProcessError:
                logger.warning("Pull failed, using existing clone")
            return dest

        logger.info("Cloning %s → %s", target.url, dest)
        clone_args = ["git", "clone", "--depth", "1", "--branch", target.branch]

        if target.sparse_paths:
            clone_args += ["--no-checkout", "--filter=blob:none"]
            clone_args += [target.url, str(dest)]
            await _git_raw(clone_args)
            await _apply_sparse_checkout(dest, target.sparse_paths)
        else:
            clone_args += [target.url, str(dest)]
            await _git_raw(clone_args)

            size_mb = _dir_size_mb(dest)
            if size_mb > self.config.max_repo_size_mb:
                logger.info(
                    "Repo is %.1f MB > %d MB limit, applying auto-sparsification",
                    size_mb,
                    self.config.max_repo_size_mb,
                )
                await _apply_sparse_checkout(dest, [
                    "src/", "app/", "lib/", "tests/", "docs/",
                    "*.json", "*.toml", "*.yaml", "*.yml", "*.md",
                    "requirements*.txt", "Dockerfile*",
                ])

        return dest


async def _git(args: list[str], cwd: Path) -> None:
    await asyncio.to_thread(
        subprocess.run,
        ["git"] + args,
        cwd=cwd,
        check=True,
        capture_output=True,
    )


async def _git_raw(args: list[str]) -> None:
    await asyncio.to_thread(subprocess.run, args, check=True, capture_output=True)


async def _apply_sparse_checkout(dest: Path, patterns: list[str]) -> None:
    await _git(["sparse-checkout", "init", "--cone"], cwd=dest)
    await _git(["sparse-checkout", "set"] + patterns, cwd=dest)
    await _git(["checkout"], cwd=dest)


def _repo_name(url: str) -> str:
    return url.rstrip("/").split("/")[-1].replace(".git", "")


def _dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)
