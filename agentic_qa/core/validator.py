"""
validator.py — Post-generation validation for specialist outputs.

Runs after each specialist agent completes:
  1. File creation check  — verifies at least one file was generated with content
  2. Lint pass            — ruff for .py, eslint for .ts/.js (gracefully skipped if unavailable)

Results are stored in the existing SpecialistResult fields:
  errors            : list[str]     — lint errors + missing-file warnings
  execution_results : dict[str,Any] — {"lint": {filename: {status, issues}}}

Future path: when specialists run as Claude Code sub-agents, these checks move into a
`.claude/settings.json` Stop hook; the validator logic itself stays the same.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .models import GeneratedTestFile, SpecialistResult

logger = logging.getLogger(__name__)


class PostGenerationValidator:
    """Validate generated files in a SpecialistResult — mutates and returns the result."""

    def validate(
        self,
        result: SpecialistResult,
        output_dir: str,
        lint: bool = True,
    ) -> SpecialistResult:
        """
        Run file-creation check and optional lint pass.

        Parameters
        ----------
        result     : SpecialistResult to validate (mutated in place)
        output_dir : Root output directory for this run (used for disk-based lint)
        lint       : Whether to run linters (can be disabled via --no-lint)
        """
        self._check_files(result)
        if lint:
            self._lint_files(result, output_dir)
        result.completed_at = result.completed_at  # unchanged — don't touch timing
        return result

    # ── File creation check ───────────────────────────────────────────────────

    def _check_files(self, result: SpecialistResult) -> None:
        if not result.generated_files:
            msg = f"[{result.test_type}] No files generated"
            result.errors.append(msg)
            logger.warning(msg)
            return

        for f in result.generated_files:
            if not f.content.strip():
                msg = f"[{result.test_type}] {f.filename}: generated file is empty"
                result.errors.append(msg)
                logger.warning(msg)

    # ── Lint pass ─────────────────────────────────────────────────────────────

    def _lint_files(self, result: SpecialistResult, output_dir: str) -> None:
        lint_results: dict[str, dict] = {}

        for f in result.generated_files:
            ext = Path(f.filename).suffix.lower()
            if ext == ".py":
                entry = self._lint_python(f, output_dir)
            elif ext in {".ts", ".js", ".tsx", ".jsx"}:
                entry = self._lint_js(f, output_dir)
            else:
                continue  # skip config files, YAML, etc.

            lint_results[f.filename] = entry

            # Promote errors (not warnings) into result.errors
            for issue in entry.get("issues", []):
                if issue.get("type") == "error":
                    result.errors.append(
                        f"[lint] {f.filename}:{issue.get('line', '?')}: {issue.get('message', '')}"
                    )

        if lint_results:
            result.execution_results["lint"] = lint_results

    @staticmethod
    def _ruff_cmd() -> list[str] | None:
        """Return the ruff command list, preferring the current venv, then PATH."""
        # Prefer running via the same Python interpreter (always works inside the venv)
        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
        # Fall back to system PATH
        if shutil.which("ruff"):
            return ["ruff"]
        return None

    def _lint_python(self, f: GeneratedTestFile, output_dir: str) -> dict:
        """Run ruff on a Python file. ruff is in [dev] deps so usually available."""
        ruff = self._ruff_cmd()
        if ruff is None:
            logger.warning("ruff not found — skipping Python lint for %s", f.filename)
            return {"status": "skipped", "reason": "ruff not found", "issues": []}

        # Write content to a temp file so ruff can read it
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="agentic_qa_lint_"
        ) as tmp:
            tmp.write(f.content)
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                [*ruff, "check", "--select", "E,F", "--output-format", "json", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            raw = proc.stdout.strip()
            if not raw:
                return {"status": "ok", "issues": []}

            ruff_issues = json.loads(raw)
            issues = [
                {
                    "line": d.get("location", {}).get("row"),
                    "col": d.get("location", {}).get("column"),
                    "code": d.get("code", ""),
                    "message": d.get("message", ""),
                    "type": "error",  # ruff E/F are always errors
                }
                for d in ruff_issues
            ]
            status = "error" if issues else "ok"
            return {"status": status, "issues": issues}

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("ruff lint failed for %s: %s", f.filename, exc)
            return {"status": "skipped", "reason": str(exc), "issues": []}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _lint_js(self, f: GeneratedTestFile, output_dir: str) -> dict:
        """Run eslint on a JS/TS file — only if eslint is available."""
        if not shutil.which("eslint"):
            logger.debug("eslint not found on PATH — skipping JS/TS lint for %s", f.filename)
            return {"status": "skipped", "reason": "eslint not found", "issues": []}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=Path(f.filename).suffix, delete=False, prefix="agentic_qa_lint_"
        ) as tmp:
            tmp.write(f.content)
            tmp_path = tmp.name

        try:
            proc = subprocess.run(
                ["eslint", "--format", "json", "--no-eslintrc", "--env", "es2020,node",
                 "--parser-options", "ecmaVersion:2020", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            raw = proc.stdout.strip()
            if not raw:
                return {"status": "ok", "issues": []}

            eslint_output = json.loads(raw)
            issues = []
            for file_result in eslint_output:
                for msg in file_result.get("messages", []):
                    issues.append({
                        "line": msg.get("line"),
                        "col": msg.get("column"),
                        "code": msg.get("ruleId", ""),
                        "message": msg.get("message", ""),
                        "type": "error" if msg.get("severity") == 2 else "warning",
                    })
            status = "error" if any(i["type"] == "error" for i in issues) else \
                     "warning" if issues else "ok"
            return {"status": status, "issues": issues}

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("eslint lint failed for %s: %s", f.filename, exc)
            return {"status": "skipped", "reason": str(exc), "issues": []}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
