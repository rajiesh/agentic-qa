# agentic-qa

Multi-agent Software Quality Analyst powered by Claude. Given a list of repository URLs and optional
documentation links, a Strategist Agent analyses the architecture and emits a structured TestPlan,
then Specialist Agents generate runnable test code (pytest, Locust, ZAP configs, Playwright).

## Architecture

### Single-repo path

```
QAOrchestrator (agentic_qa/orchestrator.py)
  └─► RepoIngestor              (agentic_qa/core/repo_ingestor.py)           — git clone / sparse-checkout
  └─► StrategistAgent           (agentic_qa/agents/strategist.py)            — explores repo, emits TestPlan JSON
  └─► [fan-out via asyncio.Semaphore(concurrency_limit=3)]
        ├─► FunctionalTestAgent   (agentic_qa/agents/specialists/functional.py)   — pytest / jest
        ├─► PerformanceTestAgent  (agentic_qa/agents/specialists/performance.py)  — Locust / k6
        ├─► SecurityTestAgent     (agentic_qa/agents/specialists/security.py)     — ZAP config / probes
        ├─► E2ETestAgent          (agentic_qa/agents/specialists/e2e.py)          — Playwright (web apps only)
        ├─► IntegrationTestAgent  (agentic_qa/agents/specialists/integration.py)  — disabled by default
        └─► ApiTestAgent          (agentic_qa/agents/specialists/api.py)          — disabled by default
```

### Multi-repo / platform path

```
PlatformOrchestrator (agentic_qa/platform_orchestrator.py)
  └─► RepoIngestor              — parallel clone of all service repos
  └─► PlatformStrategistAgent   (agentic_qa/agents/platform_strategist.py)
  │     — explores all services, discovers REST/gRPC/GraphQL/event/DB contracts
  │     — emits PlatformArchitecture JSON via emit_platform_architecture tool
  └─► [per-service fan-out]
  │     └─► QAOrchestrator._run_one(service)  — full per-service functional/perf/security/e2e
  └─► [contract fan-out]
        └─► ContractTestAgent   (agentic_qa/agents/specialists/contract.py)
              — generates Pact consumer + provider verification tests per contract
```

Platform descriptor: `platform.yaml` parsed by `agentic_qa/core/platform_config.py`.
Supports **multi-repo** (`services:` list) and **monorepo** (`repos:` with nested `services:`).

All agents extend `BaseAgent` (`agentic_qa/agents/base_agent.py`), which owns the Claude tool-use
agentic loop and applies `cache_control: ephemeral` to the system prompt for prompt caching.

## Key files

| File | Role |
|---|---|
| `agentic_qa/agents/base_agent.py` | Shared Claude tool-use loop + prompt caching |
| `agentic_qa/agents/strategist.py` | Architecture analysis → TestPlan via `emit_test_plan` tool |
| `agentic_qa/agents/platform_strategist.py` | Cross-service contract discovery → PlatformArchitecture |
| `agentic_qa/agents/specialists/*.py` | All follow identical BaseAgent pattern |
| `agentic_qa/agents/specialists/contract.py` | Pact consumer/provider contract test generator |
| `agentic_qa/core/models.py` | Pydantic types: TestPlan, SpecialistResult, QARun, PlatformRun, … |
| `agentic_qa/config.py` | QAConfig (BaseSettings), RepoTarget, SpecialistConfig |
| `agentic_qa/orchestrator.py` | Single-repo coordinator |
| `agentic_qa/platform_orchestrator.py` | Multi-repo / platform coordinator |
| `agentic_qa/core/platform_config.py` | Parse platform.yaml → list[ServiceDescriptor] |
| `agentic_qa/cli.py` | Typer CLI: `analyze`, `plan`, `analyze-platform`, `plan-platform` |
| `agentic_qa/tools/` | async_read_file, async_list_directory, async_search_code, async_fetch_url |

Output: single-repo → `outputs/<repo-name>/<run-id>/`; platform → `outputs/<platform-name>/<run-id>/`

## Dev setup

```bash
uv pip install -e ".[dev]"   # creates .venv/
cp .env.example .env         # set ANTHROPIC_API_KEY
```

## CLI usage

```bash
# ── Single repo ──────────────────────────────────────────────────────────────
# Generate full test suite (functional + performance + security + e2e by default)
.venv/bin/agentic-qa analyze <repo-url> [--doc <url>]

# Flags to control which specialists run
--no-security    disable security tests
--no-perf        disable performance tests
--no-e2e         disable E2E tests
--integration    enable integration tests
--api            enable API-specific tests

# Dry run — shows test plan only, no code generated
.venv/bin/agentic-qa plan <repo-url>

# ── Multi-repo / platform ────────────────────────────────────────────────────
# Discover contracts + generate per-service and contract tests
.venv/bin/agentic-qa analyze-platform platform.yaml

# Flags
--no-contract    skip Pact contract test generation
--no-per-service skip per-service test generation (contracts only)

# Dry run — shows discovered architecture + contracts only
.venv/bin/agentic-qa plan-platform platform.yaml
```

Example `platform.yaml`:
```yaml
name: my-platform
services:
  - name: auth-service
    url: https://github.com/org/auth
    role: backend
  - name: web-frontend
    url: https://github.com/org/frontend
    role: frontend
docs:
  - https://wiki.internal/architecture

# Or monorepo style:
repos:
  - url: https://github.com/org/monorepo
    services:
      - name: api
        path: services/api
        role: backend
      - name: web
        path: apps/web
        role: frontend
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
- **Platform contract discovery**: `PlatformStrategistAgent` uses extended thinking + 40-iteration loop to sweep all service repos and find REST/gRPC/GraphQL/event/DB contracts; emits validated `PlatformArchitecture` JSON
- **Consumer-driven contracts**: `ContractTestAgent` generates Pact consumer tests + provider verification tests for each discovered contract; detects JS vs Python via `package.json` presence
- **No re-cloning**: `PlatformOrchestrator` clones all repos once; per-service QA runs reuse `svc.local_path`
- **platform.yaml**: supports both multi-repo (`services:`) and monorepo (`repos: … services:`) shapes; see `agentic_qa/core/platform_config.py`
- **todo-platform.yaml**: example descriptor for the local todo-app (both backend + frontend as monorepo services)

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
