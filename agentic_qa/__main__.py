"""
Entry point for agentic-qa.

`agentic-qa` (or `python -m agentic_qa`) starts the interactive session — the only
user-facing interface. Adding repos/docs, plans, analyses and platform runs are all
driven conversationally through SessionAgent.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console

from .config import QAConfig
from .session import InteractiveSession


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-qa",
        description="AI-powered QA test generation using Claude (interactive session).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    console = Console()

    try:
        config = QAConfig()  # type: ignore[call-arg]
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        console.print("Ensure ANTHROPIC_API_KEY is set in your environment or .env file.")
        return 1

    asyncio.run(InteractiveSession(config).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
