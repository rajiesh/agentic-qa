from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .config import QAConfig, RepoTarget, SpecialistConfig, TestTypeConfig
from .core.platform_config import load_platform
from .core.platform_init import detect_all_roles, generate_platform_yaml
from .orchestrator import QAOrchestrator
from .platform_orchestrator import PlatformOrchestrator

app = typer.Typer(
    name="agentic-qa",
    help="AI-powered QA test generation using Claude",
    # no_args_is_help removed: bare `agentic-qa` launches interactive session instead
    invoke_without_command=True,
)
console = Console()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging", is_eager=True),
) -> None:
    """
    AI-powered QA test generation using Claude.

    Run without a subcommand to start an interactive session.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand was given — let it run; this callback is a no-op.
        return

    # No subcommand → launch interactive session
    _setup_logging(verbose)
    from .session import InteractiveSession  # lazy import avoids startup cost when using subcommands

    try:
        config = QAConfig()  # type: ignore[call-arg]
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        console.print("Ensure ANTHROPIC_API_KEY is set in your environment or .env file.")
        raise typer.Exit(1) from exc

    asyncio.run(InteractiveSession(config).run())


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ── Single-repo commands ───────────────────────────────────────────────────────

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
    no_e2e: bool = typer.Option(False, "--no-e2e", help="Disable Playwright E2E tests"),
    no_lint: bool = typer.Option(False, "--no-lint", help="Skip linting generated test files"),
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
    if no_lint:
        config.lint_generated = False

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
            console.print(
                f"Tech Stack: {', '.join(run.test_plan.tech_stack.languages + run.test_plan.tech_stack.frameworks)}"
            )
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


# ── Platform / multi-repo commands ─────────────────────────────────────────────

@app.command(name="init-platform")
def init_platform(
    repos: list[str] = typer.Argument(..., help="Repo URLs or local paths to include"),
    name: str = typer.Option("", "--name", "-n", help="Platform name (default: current directory name)"),
    output: str = typer.Option("platform.yaml", "--output", "-o", help="Output path for generated platform.yaml"),
    docs: list[str] = typer.Option([], "--doc", "-d", help="Global doc URLs to include (repeatable)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bootstrap a platform.yaml from repo URLs — detects service roles automatically."""
    _setup_logging(verbose)

    platform_name = name or Path.cwd().name
    # init-platform doesn't need an API key — it only reads files and runs git
    output_dir = "outputs"

    console.print(f"[bold]Detecting roles for {len(repos)} repo(s)...[/bold]")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as progress:
        progress.add_task("Cloning and inspecting repos...", total=None)
        services = asyncio.run(detect_all_roles(repos, output_dir=output_dir))

    # Print detection summary
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Service")
    table.add_column("Role")
    table.add_column("Detected from")
    for svc in services:
        table.add_row(svc["name"], svc["role"], svc["reason"])
    console.print(table)

    yaml_content = generate_platform_yaml(
        platform_name=platform_name,
        services=services,
        global_docs=docs,
    )

    output_path = Path(output)
    if output_path.exists():
        console.print(f"\n[yellow]Warning:[/yellow] {output} already exists — overwriting.")

    output_path.write_text(yaml_content)
    console.print(f"\n[green]✓[/green] Written to [bold]{output}[/bold]")
    console.print(
        f"\nNext steps:\n"
        f"  1. Review and edit [bold]{output}[/bold] — correct any wrong roles, add doc links\n"
        f"  2. [dim]agentic-qa plan-platform {output}[/dim]      — discover contracts (dry run)\n"
        f"  3. [dim]agentic-qa analyze-platform {output}[/dim]   — generate full test suite"
    )


@app.command(name="analyze-platform")
def analyze_platform(
    platform_file: str = typer.Argument(..., help="Path to platform.yaml descriptor"),
    output_dir: str = typer.Option("outputs", "--output", "-o", help="Output directory"),
    no_security: bool = typer.Option(False, "--no-security", help="Disable security tests"),
    no_performance: bool = typer.Option(False, "--no-perf", help="Disable performance tests"),
    no_e2e: bool = typer.Option(False, "--no-e2e", help="Disable Playwright E2E tests"),
    enable_integration: bool = typer.Option(False, "--integration", help="Enable integration tests"),
    enable_api: bool = typer.Option(False, "--api", help="Enable API-specific tests"),
    no_contract: bool = typer.Option(False, "--no-contract", help="Skip contract test generation"),
    no_per_service: bool = typer.Option(False, "--no-per-service", help="Skip per-service test generation"),
    no_lint: bool = typer.Option(False, "--no-lint", help="Skip linting generated test files"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="Max parallel agents"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Analyze a multi-service platform and generate per-service + contract tests."""
    _setup_logging(verbose)

    if not Path(platform_file).exists():
        console.print(f"[red]Platform file not found:[/red] {platform_file}")
        raise typer.Exit(1)

    try:
        platform_name, services, doc_links = load_platform(platform_file)
    except Exception as exc:
        console.print(f"[red]Failed to parse platform descriptor:[/red] {exc}")
        raise typer.Exit(1) from exc

    try:
        config = QAConfig(  # type: ignore[call-arg]
            output_dir=output_dir,
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
    if no_e2e:
        config.specialists.e2e.enabled = False
    if enable_integration:
        config.specialists.integration.enabled = True
    if enable_api:
        config.specialists.api.enabled = True
    if no_contract:
        config.specialists.contract.enabled = False
    if no_lint:
        config.lint_generated = False

    console.print(
        f"\n[bold cyan]Platform:[/bold cyan] {platform_name} "
        f"([dim]{len(services)} services[/dim])\n"
    )
    for svc in services:
        console.print(f"  • [bold]{svc.name}[/bold] ({svc.role})  {svc.repo_url}")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as progress:
        progress.add_task("Running platform QA analysis...", total=None)
        platform_run = asyncio.run(
            _run_platform_orchestrator(
                config,
                platform_name,
                services,
                doc_links,
                run_per_service=not no_per_service,
                run_contracts=not no_contract,
            )
        )

    _print_platform_summary(platform_run)


@app.command(name="plan-platform")
def plan_platform(
    platform_file: str = typer.Argument(..., help="Path to platform.yaml descriptor"),
    output_dir: str = typer.Option("outputs", "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Discover platform architecture and contracts WITHOUT generating test code."""
    _setup_logging(verbose)

    if not Path(platform_file).exists():
        console.print(f"[red]Platform file not found:[/red] {platform_file}")
        raise typer.Exit(1)

    try:
        platform_name, services, doc_links = load_platform(platform_file)
    except Exception as exc:
        console.print(f"[red]Failed to parse platform descriptor:[/red] {exc}")
        raise typer.Exit(1) from exc

    try:
        config = QAConfig(output_dir=output_dir)  # type: ignore[call-arg]
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(
        f"\n[bold cyan]Platform:[/bold cyan] {platform_name} "
        f"([dim]{len(services)} services[/dim])\n"
    )

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as progress:
        progress.add_task("Discovering platform architecture...", total=None)
        platform_run = asyncio.run(
            _run_platform_orchestrator(
                config,
                platform_name,
                services,
                doc_links,
                run_per_service=False,
                run_contracts=False,
            )
        )

    plan = platform_run.platform_plan  # type: ignore[attr-defined]
    if not plan:
        console.print("[red]Failed to build platform plan.[/red]")
        raise typer.Exit(1)

    arch = plan.architecture

    svc_table = Table(title="Services", show_header=True, header_style="bold cyan")
    svc_table.add_column("Name")
    svc_table.add_column("Role")
    svc_table.add_column("Repo")
    for svc in arch.services:
        svc_table.add_row(svc.name, svc.role, svc.repo_url.split("/")[-1])
    console.print(svc_table)

    if arch.topology_notes:
        console.print(f"\n[bold]Topology[/bold]\n{arch.topology_notes}\n")

    if arch.contracts:
        ctr_table = Table(title="Discovered Contracts", show_header=True, header_style="bold magenta")
        ctr_table.add_column("Consumer")
        ctr_table.add_column("Provider")
        ctr_table.add_column("Type")
        ctr_table.add_column("Endpoints / Topics")
        ctr_table.add_column("Description")
        for c in arch.contracts:
            ctr_table.add_row(
                c.consumer,
                c.provider,
                c.contract_type,
                ", ".join(c.endpoints[:3]) + ("…" if len(c.endpoints) > 3 else ""),
                c.description[:70] + "…" if len(c.description) > 70 else c.description,
            )
        console.print(ctr_table)
    else:
        console.print("[dim]No contracts discovered.[/dim]")

    if plan.contract_entries:
        console.print(f"\n[bold]Contract Test Tasks ({len(plan.contract_entries)})[/bold]")
        for entry in plan.contract_entries:
            console.print(
                f"  • {entry.contract.consumer} → {entry.contract.provider} "
                f"[{entry.contract.contract_type}]  "
                f"consumer_fw={entry.consumer_framework}  provider_fw={entry.provider_framework}"
            )

    if platform_run.output_directory:  # type: ignore[attr-defined]
        console.print(f"\nPlan saved to: {platform_run.output_directory}")  # type: ignore[attr-defined]


# ── Async helpers ──────────────────────────────────────────────────────────────

async def _run_orchestrator(config: QAConfig, targets: list[RepoTarget]) -> list:
    orchestrator = QAOrchestrator(config)
    return await orchestrator.run(targets)


async def _run_platform_orchestrator(
    config: QAConfig,
    platform_name: str,
    services: list,
    doc_links: list[str],
    run_per_service: bool,
    run_contracts: bool,
) -> object:
    orchestrator = PlatformOrchestrator(config)
    return await orchestrator.run(
        platform_name=platform_name,
        services=services,
        global_doc_links=doc_links,
        run_per_service=run_per_service,
        run_contracts=run_contracts,
    )


# ── Output printers ────────────────────────────────────────────────────────────

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


def _print_platform_summary(platform_run: object) -> None:
    from .core.models import PlatformRun as _PR

    run: _PR = platform_run  # type: ignore[assignment]
    status_color = "green" if run.success else "yellow"
    status_label = "success" if run.success else "partial"
    console.print(
        f"\n[bold]Platform Run:[/bold] {run.run_id[:8]}  |  "
        f"[{status_color}]{status_label}[/]"
    )

    if run.service_runs:
        svc_table = Table(title="Per-Service Results", header_style="bold cyan")
        svc_table.add_column("Service")
        svc_table.add_column("Test Types")
        svc_table.add_column("Files")
        svc_table.add_column("Status")
        for svc_name, qa_run in run.service_runs.items():
            types = ", ".join(r.test_type for r in qa_run.specialist_results) or "—"
            files = sum(len(r.generated_files) for r in qa_run.specialist_results)
            s = "[green]✓[/]" if qa_run.success else "[yellow]partial[/]"
            svc_table.add_row(svc_name, types, str(files), s)
        console.print(svc_table)

    if run.contract_results:
        ctr_table = Table(title="Contract Tests", header_style="bold magenta")
        ctr_table.add_column("Agent")
        ctr_table.add_column("Files Generated")
        ctr_table.add_column("Errors")
        for cr in run.contract_results:
            ctr_table.add_row(
                cr.agent_id,
                str(len(cr.generated_files)),
                str(len(cr.errors)) if cr.errors else "—",
            )
        console.print(ctr_table)

    if run.output_directory:
        console.print(f"\n[bold]Output:[/bold] {run.output_directory}")
