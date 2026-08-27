from __future__ import annotations

import json
from pathlib import Path

import aiofiles

from .models import QARun


class OutputManager:
    def __init__(self, base_dir: str, repo_name: str, run_id: str) -> None:
        self.root = Path(base_dir) / repo_name / run_id
        self.root.mkdir(parents=True, exist_ok=True)

    async def write(self, test_type: str, filename: str, content: str) -> Path:
        dest = self.root / test_type / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(dest, "w") as f:
            await f.write(content)
        return dest

    async def write_test_plan(self, plan_json: str) -> None:
        dest = self.root / "test_plan.json"
        async with aiofiles.open(dest, "w") as f:
            await f.write(plan_json)

    async def write_run_summary(self, run: QARun) -> None:
        dest = self.root / "run_summary.json"
        async with aiofiles.open(dest, "w") as f:
            await f.write(run.model_dump_json(indent=2))
