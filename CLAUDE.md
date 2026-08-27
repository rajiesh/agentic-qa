# agentic-qa

Multi-agent Software Quality Analyst powered by Claude. Given a list of repository URLs and optional
documentation links, a Strategist Agent analyses the architecture and emits a structured TestPlan,
then Specialist Agents generate runnable test code (pytest, Locust, ZAP configs, Playwright).

## Architecture

```
QAOrchestrator (agentic_qa/orchestrator.py)
  └─► RepoIngestor         (agentic_qa/core/repo_ingestor.py)           — git clone / sparse-checkout
  └─► StrategistAgent      (agentic_qa/agents/strategist.py)            — explores repo, emits TestPlan JSON
  └─► [fan-out via asyncio.Semaphore(concurrency_limit=3)]
        ├─► FunctionalTestAgent   (agentic_qa/agents/specialists/functional.py)   — pytest / jest
        ├─► PerformanceTestAgent  (agentic_qa/agents/specialists/performance.py)  — Locust / k6
        ├─► SecurityTestAgent     (agentic_qa/agents/specialists/security.py)     — ZAP config / probes
        ├─► E2ETestAgent          (agentic_qa/agents/specialists/e2e.py)          — Playwright (web apps only)
        ├─► IntegrationTestAgent  (agentic_qa/agents/specialists/integration.py)  — disabled by default
        └─► ApiTestAgent          (agentic_qa/agents/specialists/api.py)          — disabled by default
```

All agents extend `BaseAgent` (`agentic_qa/agents/base_agent.py`), which owns the Claude tool-use
agentic loop and applies `cache_control: ephemeral` to the system prompt for prompt caching.

## Key files

| File | Role |
|---|---|
| `agentic_qa/agents/base_agent.py` | Shared Claude tool-use loop + prompt caching |
| `agentic_qa/agents/strategist.py` | Architecture analysis → TestPlan via `emit_test_plan` tool |
| `agentic_qa/agents/specialists/*.py` | All follow identical BaseAgent pattern |
| `agentic_qa/core/models.py` | Pydantic types: TestPlan, SpecialistResult, QARun |
| `agentic_qa/config.py` | QAConfig (BaseSettings), RepoTarget, SpecialistConfig |
| `agentic_qa/orchestrator.py` | Top-level coordinator |
| `agentic_qa/cli.py` | Typer CLI: `analyze` and `plan` commands |
| `agentic_qa/tools/` | async_read_file, async_list_directory, async_search_code, async_fetch_url |

Output lands in `outputs/<repo-name>/<run-id>/` organised by test type.

## Dev setup

```bash
uv pip install -e ".[dev]"   # creates .venv/
cp .env.example .env         # set ANTHROPIC_API_KEY
```

## CLI usage

```bash
# Generate full test suite (functional + performance + security by default)
.venv/bin/agentic-qa analyze <repo-url> [--doc <url>]

# Flags to control which specialists run
--no-security    disable security tests
--no-perf        disable performance tests
--e2e            enable Playwright E2E tests (web apps only, auto-detected by Strategist)
--integration    enable integration tests
--api            enable API-specific tests

# Dry run — shows test plan only, no code generated
.venv/bin/agentic-qa plan <repo-url>
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

## Design decisions

- **Strategist thinking**: uses `thinking: enabled` for deep multi-step architecture reasoning
- **Prompt caching**: system prompt of every agent is sent with `cache_control: ephemeral`,
  cutting ~90% of input token cost on repeated tool-use loop iterations
- **Structured output**: Strategist must call `emit_test_plan` tool with validated JSON — not
  free text — eliminating fragile regex parsing
- **E2E gate**: Strategist only includes an `e2e` plan entry when it detects a web frontend
  (React/Vue/Angular/Next.js/Svelte); pure API backends never get E2E entries
- **Concurrency**: specialist fan-out is bounded by `asyncio.Semaphore(concurrency_limit)` (default 3)

## todo-app (end-to-end test target)

Sample app in `todo-app/` used to test agentic-qa against a real project.

- `todo-app/backend/` — FastAPI with POST/PUT/DELETE /todos, asyncpg + SQLAlchemy async
- `todo-app/frontend/` — Vanilla HTML/CSS/JS SPA (add / edit / delete / filter todos)
- `todo-app/docker-compose.yml` — PostgreSQL 16 container (user: todouser, db: tododb)
- `todo-app/deploy.sh` — one-command setup: starts Docker postgres, Python venv, uvicorn (8000), http.server (3000)

```bash
cd todo-app
bash deploy.sh        # start postgres container + API + web server
bash deploy.sh --stop # stop API + web server; keep postgres data volume
```

URLs when running: UI → http://localhost:3000 · API → http://localhost:8000 · Docs → http://localhost:8000/docs
