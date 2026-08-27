from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .config import QAConfig, RepoTarget, SpecialistConfig, TestTypeConfig
from .orchestrator import QAOrchestrator

app = typer.Typer(
    name="agentic-qa",
    help="AI-powered QA test generation using Claude",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@app.command()
def analyze(
    repos: list[str] = typer.Argument(..., help="Repository URLs or local paths"),
    docs: list[str] = typer.Option([], "--doc", "-d", help="Documentation URLs (repeatable)"),
    output_dir: str = typer.Option("outputs", "--output", "-o", help="Output directory"),
    run_tests: bool = typer.Option(False, "--run-tests", help="Execute generated tests after creation"),
    no_security: bool = typer.Option(False, "--no-security", help="Disable security test generation"),
    no_performance: bool = typer.Option(False, "--no-perf", help="Disable performance test generation"),
    enable_integration: bool = typer.Option(False, "--integration", help="Enable integration tests"),
    enable_api: bool = typer.Option(False, "--api", help="Enable API-specific tests"),
    no_e2e: bool = typer.Option(False, "--no-e2e", help="Disable Playwright E2E tests (e.g. if Playwright is not installed)"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="Max parallel specialists"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Analyze repositories and generate a comprehensive test suite."""
    _setup_logging(verbose)

    try:
        config = QAConfig(  # type: ignore[call-arg]
            output_dir=output_dir,
            run_generated_tests=run_tests,
            concurrency_limit=concurrency,
        )
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        console.print("Ensure ANTHROPIC_API_KEY is set in your environment or .env file.")
        raise typer.Exit(1) from exc

    if no_security:
        config.specialists.security.enabled = False
    if no_performance:
        config.specialists.performance.enabled = False
    if enable_integration:
        config.specialists.integration.enabled = True
    if enable_api:
        config.specialists.api.enabled = True
    if no_e2e:
        config.specialists.e2e.enabled = False

    targets = [RepoTarget(url=repo, doc_links=docs) for repo in repos]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Running agentic QA analysis...", total=None)

        runs = asyncio.run(_run_orchestrator(config, targets))
        progress.update(task, completed=True)

    _print_summary(runs)


@app.command()
def plan(
    repos: list[str] = typer.Argument(..., help="Repository URLs or local paths"),
    docs: list[str] = typer.Option([], "--doc", "-d", help="Documentation URLs"),
    output_dir: str = typer.Option("outputs", "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate and display a test plan WITHOUT running specialist agents."""
    _setup_logging(verbose)

    try:
        config = QAConfig(output_dir=output_dir)  # type: ignore[call-arg]
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(1) from exc

    # Disable all specialists so only the strategist runs
    config.specialists = SpecialistConfig(
        functional=TestTypeConfig(enabled=False),
        performance=TestTypeConfig(enabled=False),
        security=TestTypeConfig(enabled=False),
        integration=TestTypeConfig(enabled=False),
        api=TestTypeConfig(enabled=False),
        e2e=TestTypeConfig(enabled=False),
    )

    targets = [RepoTarget(url=repo, doc_links=docs) for repo in repos]

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as progress:
        progress.add_task("Analyzing repositories...", total=None)
        runs = asyncio.run(_run_orchestrator(config, targets))

    for run in runs:
        if run.test_plan:
            console.print(f"\n[bold]Test Plan for[/bold] {run.repo_url}")
            console.print(f"Tech Stack: {', '.join(run.test_plan.tech_stack.languages + run.test_plan.tech_stack.frameworks)}")
            console.print(f"Risk Areas: {', '.join(run.test_plan.overall_risk_areas)}\n")

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Test Type")
            table.add_column("Priority")
            table.add_column("Framework")
            table.add_column("Est. Files")
            table.add_column("Rationale")
            for entry in run.test_plan.entries:
                table.add_row(
                    entry.test_type,
                    entry.priority,
                    entry.suggested_framework,
                    str(entry.estimated_files),
                    entry.rationale[:80] + "..." if len(entry.rationale) > 80 else entry.rationale,
                )
            console.print(table)

            if run.output_directory:
                plan_file = Path(run.output_directory) / "test_plan.json"
                console.print(f"\nTest plan saved to: {plan_file}")
        else:
            console.print(f"[red]No test plan generated for {run.repo_url}[/red]")


async def _run_orchestrator(config: QAConfig, targets: list[RepoTarget]) -> list:
    orchestrator = QAOrchestrator(config)
    return await orchestrator.run(targets)


def _print_summary(runs: list) -> None:
    table = Table(title="QA Run Summary", show_header=True, header_style="bold cyan")
    table.add_column("Repository")
    table.add_column("Test Types")
    table.add_column("Files Generated")
    table.add_column("Output Directory")
    table.add_column("Status")

    for run in runs:
        if not run.specialist_results:
            files_count = 0
            types = "[dim]none[/dim]"
        else:
            files_count = sum(len(r.generated_files) for r in run.specialist_results)
            types = ", ".join(r.test_type for r in run.specialist_results)

        status = "[green]success[/]" if run.success else "[yellow]partial[/]"
        if not run.test_plan:
            status = "[red]failed[/]"

        short_url = run.repo_url.split("/")[-1] if "/" in run.repo_url else run.repo_url
        table.add_row(short_url, types, str(files_count), run.output_directory, status)

    console.print("\n")
    console.print(table)

    for run in runs:
        if run.output_directory:
            console.print(f"\n[bold]Output:[/bold] {run.output_directory}")
            if run.specialist_results:
                for result in run.specialist_results:
                    for f in result.generated_files:
                        console.print(f"  [dim]{result.test_type}/[/dim]{f.filename}")
