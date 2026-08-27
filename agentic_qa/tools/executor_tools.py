from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path


async def async_run_tests(
    test_dir: str,
    framework: str = "pytest",
    timeout: int = 120,
) -> dict[str, str]:
    """Run generated tests and return {stdout, stderr, returncode}."""
    cmd_map = {
        "pytest": ["python", "-m", "pytest", test_dir, "-v", "--tb=short"],
        "jest": ["npx", "jest", test_dir, "--no-coverage"],
    }
    cmd = cmd_map.get(framework, ["python", "-m", "pytest", test_dir])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(test_dir).parent),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": str(proc.returncode),
        }
    except asyncio.TimeoutError:
        return {"stdout": "", "stderr": "Test execution timed out.", "returncode": "-1"}
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": "-1"}
