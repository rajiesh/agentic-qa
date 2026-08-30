# agentic-qa

Multi-agent Software Quality Analyst powered by Claude. Given repository URLs and optional
documentation links, a Strategist Agent analyses the architecture and emits a structured TestPlan,
then Specialist Agents generate runnable test code (pytest, Locust, ZAP configs, Playwright, Pact).

---

## Architecture

### Single-repo path

```
CLI: analyze / plan
  └─► QAOrchestrator           (agentic_qa/orchestrator.py)
        └─► CostTracker               — shared USD budget across all agents in this batch
        └─► asyncio.Semaphore(repo_concurrency_limit=5)   — outer repo parallelism gate
        └─► RepoIngestor              (agentic_qa/core/repo_ingestor.py)    — git clone / sparse-checkout
        └─► StrategistAgent           (agentic_qa/agents/strategist.py)
        │     — tools: read_file, list_directory, search_code, fetch_url
        │     — emits TestPlan JSON via emit_test_plan tool (validated Pydantic)
        │     — thinking: enabled  ·  max_iter=25  ·  E2E gate: only emits e2e entry for web frontends
        └─► OutputManager             — creates outputs/<repo>/<run-id>/
        └─► [fan-out via asyncio.Semaphore(concurrency_limit=3)]
              ├─► FunctionalTestAgent    (specialists/functional.py)   — pytest / jest / vitest
              ├─► PerformanceTestAgent   (specialists/performance.py)  — Locust / k6
              ├─► SecurityTestAgent      (specialists/security.py)     — ZAP config / OWASP probes
              ├─► E2ETestAgent           (specialists/e2e.py)          — Playwright (web apps only)
              ├─► IntegrationTestAgent   (specialists/integration.py)  — disabled by default (--integration)
              └─► ApiTestAgent           (specialists/api.py)          — disabled by default (--api)
        └─► PostGenerationValidator   — ruff (Python) / eslint (JS/TS) lint on every generated file
```

### Multi-repo / platform path

```
CLI: analyze-platform / plan-platform / init-platform
  └─► PlatformOrchestrator     (agentic_qa/platform_orchestrator.py)
        └─► CheckpointManager         (agentic_qa/core/checkpoint_manager.py)
        │     — load/save PlatformCheckpoint atomically (os.replace .tmp → .json)
        │     — reset_crashed_states(): "running" → "pending" on resume
        └─► CostTracker               — single shared instance for the entire platform run
        │
        │ ── 1. Clone ────────────────────────────────────────────────────────────
        └─► RepoIngestor × N          — parallel clone, bounded by asyncio.Semaphore(concurrency_limit)
        │     — skip if checkpoint.cloned_services[name] already exists on disk
        │
        │ ── 2a. Scan (per-service, parallel) ───────────────────────────────────
        └─► ServiceScannerAgent × N   (agentic_qa/agents/service_scanner.py)
        │     — tools: read_file, list_directory, search_code
        │     — emits ServiceSummary (~600 tokens) via emit_service_summary tool
        │     — extracts: outbound_http_urls, env_service_refs, event_patterns,
        │                 proto_files, openapi_files, exposed_endpoints, shared_db_hints
        │     — bounded by asyncio.Semaphore(scanner_concurrency_limit=10)
        │     — max_iter=15 per service  ·  result cached in checkpoint.scan_results
        │
        │ ── 2b. Synthesize (single agent, summaries as input) ──────────────────
        └─► PlatformSynthesizerAgent  (agentic_qa/agents/platform_synthesizer.py)
        │     — NO file-access tools — reasons over compact ServiceSummary objects only
        │     — 50 services × ~600 tok = ~30K context (well within 200K limit)
        │     — emits PlatformArchitecture via emit_platform_architecture tool
        │     — thinking: enabled  ·  max_iter=8
        │     — result cached in checkpoint.architecture_json
        │
        │ ── 3. Per-service QA ───────────────────────────────────────────────────
        └─► QAOrchestrator._run_one(svc)  — full functional/perf/security/e2e per service
        │     — bounded by asyncio.Semaphore(repo_concurrency_limit=5)
        │     — skips if checkpoint.service_qa_states[name].status == "completed"
        │
        │ ── 4. Contract tests ───────────────────────────────────────────────────
        └─► ContractTestAgent × N     (specialists/contract.py)
              — generates Pact consumer tests + provider verification per discovered contract
              — detects JS vs Python via package.json presence → picks @pact-foundation/pact or pact-python
              — bounded by asyncio.Semaphore(concurrency_limit=3)
              — skips if checkpoint.contract_states[key].status == "completed"
```

Platform descriptor: `platform.yaml` parsed by `agentic_qa/core/platform_config.py`.
Supports **multi-repo** (`services:` list) and **monorepo** (`repos:` with nested `services:`).

### Interactive session (no subcommand)

```
agentic-qa          (no arguments)
  └─► InteractiveSession  (agentic_qa/session.py)
        └─► SessionAgent  (agentic_qa/agents/session_agent.py)
              — conversational REPL: add repos, ask for plans or full analysis
              — maintains SessionState: repos, doc_links, config_overrides, qa_runs
              — conversation_history trimmed to session_history_max_turns (default 20)
              — slash commands: /repos, /runs, /config, /clear, /help
```

### BaseAgent — shared foundation for ALL agents

Every agent (`StrategistAgent`, `ServiceScannerAgent`, `PlatformSynthesizerAgent`, all 7 specialists,
`SessionAgent`) extends `BaseAgent` (`agentic_qa/agents/base_agent.py`), which provides:

| Concern | Mechanism |
|---|---|
| **Retry / backoff** | `max_retries` exponential attempts on `RateLimitError` / `APIStatusError`; wait = `min(base × 2ⁿ, max_wait)` |
| **Sliding window context** | After each tool exchange, if `tool_pairs > max_context_tool_pairs`, evicts oldest pair (messages[1:3]); initial user message always preserved |
| **Prompt caching** | System prompt sent with `cache_control: ephemeral` → ~90% input token savings on repeated iterations |
| **Cost enforcement** | `await cost_tracker.record(delta)` then `cost_tracker.check_budget()` after every API response; raises `BudgetExceededError` if over cap |

---

## Key files

| File | Role |
|---|---|
| `agentic_qa/cli.py` | Typer CLI: `analyze`, `plan`, `init-platform`, `analyze-platform`, `plan-platform`; bare invocation → interactive session |
| `agentic_qa/session.py` | `InteractiveSession` + `SessionState` — REPL loop and session memory |
| `agentic_qa/config.py` | `QAConfig` (BaseSettings), `RepoTarget`, `SpecialistConfig`, `TestTypeConfig` |
| `agentic_qa/orchestrator.py` | Single-repo coordinator: clone → strategist → fan-out → validate |
| `agentic_qa/platform_orchestrator.py` | Multi-repo coordinator: clone → scan → synthesize → per-service QA → contract tests |
| **Agents** | |
| `agentic_qa/agents/base_agent.py` | Abstract base: tool-use loop, retry, sliding window, prompt caching, cost enforcement |
| `agentic_qa/agents/strategist.py` | Single-repo architecture analysis → `TestPlan` via `emit_test_plan` |
| `agentic_qa/agents/service_scanner.py` | Per-service contract-signal extraction → `ServiceSummary` via `emit_service_summary` |
| `agentic_qa/agents/platform_synthesizer.py` | Cross-service reasoning over compact summaries → `PlatformArchitecture` |
| `agentic_qa/agents/platform_strategist.py` | Legacy monolithic platform strategist (kept; deprecated in favour of scanner+synthesizer) |
| `agentic_qa/agents/session_agent.py` | Conversational QA orchestration agent for interactive session |
| `agentic_qa/agents/specialists/functional.py` | pytest / jest / vitest test generation |
| `agentic_qa/agents/specialists/performance.py` | Locust / k6 load test generation |
| `agentic_qa/agents/specialists/security.py` | ZAP config + OWASP probe generation |
| `agentic_qa/agents/specialists/e2e.py` | Playwright E2E tests (web apps only) |
| `agentic_qa/agents/specialists/contract.py` | Pact consumer + provider verification tests |
| `agentic_qa/agents/specialists/integration.py` | httpx / docker-compose integration tests (disabled by default) |
| `agentic_qa/agents/specialists/api.py` | OpenAPI-driven API tests (disabled by default) |
| **Core** | |
| `agentic_qa/core/models.py` | Pydantic types: `TestPlan`, `ServiceSummary`, `PlatformArchitecture`, `QARun`, `PlatformRun`, … |
| `agentic_qa/core/checkpoint.py` | `PlatformCheckpoint`, `ServiceRunState`, `ServiceScanResult` — checkpoint data model |
| `agentic_qa/core/checkpoint_manager.py` | Atomic checkpoint read/write; `mark_clone_complete`, `mark_scan_complete`, `mark_architecture_complete`, `mark_service_qa_*`, `mark_contract_*` |
| `agentic_qa/core/cost_tracker.py` | `CostTracker` (asyncio.Lock); `BudgetExceededError`; pricing: input $3/M, output $15/M, cache-write $3.75/M, cache-read $0.30/M |
| `agentic_qa/core/output_manager.py` | Creates output directories; writes test files, test_plan.json, run_summary.json |
| `agentic_qa/core/validator.py` | `PostGenerationValidator`: file-creation check + ruff (Python) / eslint (JS/TS) lint |
| `agentic_qa/core/repo_ingestor.py` | git clone / sparse-checkout, size cap (`max_repo_size_mb`) |
| `agentic_qa/core/platform_config.py` | Parse `platform.yaml` → `list[ServiceDescriptor]`; supports multi-repo and monorepo shapes |
| `agentic_qa/core/platform_init.py` | `detect_role(path)` heuristics; `generate_platform_yaml(...)` for `init-platform` command |
| **Tools** | |
| `agentic_qa/tools/repo_tools.py` | `async_read_file(path, offset=0, max_lines=300)` with pagination hint; `async_list_directory`; `async_search_code` (ripgrep preferred) |
| `agentic_qa/tools/web_tools.py` | `async_fetch_url` — httpx; used for doc links and OpenAPI specs |
| `agentic_qa/tools/write_tools.py` | `async_write_file` — used by specialist agents to emit generated test files |
| `agentic_qa/tools/executor_tools.py` | Reserved: test execution hooks (unused in current flow) |

**Output layout:**
- Single-repo: `outputs/<repo-name>/<run-id>/` — `test_plan.json`, `functional/`, `performance/`, `security/`, `e2e/`, `run_summary.json`
- Platform: `outputs/<platform-name>/<run-id>/` — `platform_run_summary.json` (incl. `cost_summary`), `contracts/`, `<svc-name>/…`, `checkpoint.json`

---

## Complete QAConfig reference

All fields are settable as environment variables (snake_case → UPPER_SNAKE_CASE, e.g. `COST_BUDGET_USD`).

| Field | Default | Notes |
|---|---|---|
| `anthropic_api_key` | *(required)* | `ANTHROPIC_API_KEY` env var |
| `model` | `"claude-sonnet-4-6"` | |
| `max_tokens_strategist` | `8192` | |
| `max_tokens_specialist` | `16384` | |
| `output_dir` | `"outputs"` | |
| `max_repo_size_mb` | `500` | Repo clone size cap |
| `run_generated_tests` | `False` | Execute generated tests after creation (reserved) |
| `lint_generated` | `True` | Run ruff/eslint on each generated file |
| `concurrency_limit` | `3` | Specialist fan-out parallelism within one repo; also contract sem |
| `repo_concurrency_limit` | `5` | Outer repo/service-QA parallelism gate |
| `max_retries` | `5` | API retry attempts on rate-limit/server errors |
| `retry_base_wait_secs` | `5.0` | Exponential backoff base (seconds) |
| `retry_max_wait_secs` | `120.0` | Backoff ceiling (seconds) |
| `max_tokens_session` | `4096` | `max_tokens` for interactive session agent turns |
| `max_context_tool_pairs` | `10` | Sliding window: max tool-use/result pairs kept in messages list |
| `session_history_max_turns` | `20` | Interactive session conversation window (turns) |
| `enable_checkpointing` | `True` | Save/load checkpoint.json during platform runs |
| `checkpoint_dir` | `None` | Override checkpoint location (default: `output_dir/<platform_name>`) |
| `scanner_concurrency_limit` | `10` | Parallel `ServiceScannerAgent` instances |
| `scanner_max_iterations` | `15` | Per-service scanner loop cap |
| `synthesizer_max_iterations` | `8` | Synthesis-only loop cap (no file reads) |
| `cost_budget_usd` | `None` | USD cap; `None` = unlimited. Raises `BudgetExceededError` if exceeded. |

---

## Dev setup

```bash
uv pip install -e ".[dev]"   # creates .venv/
cp .env.example .env         # set ANTHROPIC_API_KEY
```

Optional (contract tests only):
```bash
uv pip install -e ".[contract]"   # adds pact-python
```

---

## CLI usage

```bash
# ── Interactive session (no subcommand) ──────────────────────────────────────
.venv/bin/agentic-qa
# Launches conversational REPL — add repos, ask for plans or analyses
# Slash commands: /repos  /runs  /config  /clear  /help

# ── Single repo ──────────────────────────────────────────────────────────────
.venv/bin/agentic-qa analyze <repo-url> [<repo-url>...] [--doc <url>]
  --concurrency 3        # parallel specialists per repo (default 3)
  --repo-concurrency 5   # parallel repos (default 5)
  --budget 5.00          # USD cost cap (aborts with BudgetExceededError if exceeded)
  --no-security          # disable security tests
  --no-perf              # disable performance tests
  --no-e2e               # disable Playwright E2E tests
  --no-lint              # skip ruff/eslint on generated files
  --integration          # enable integration tests (disabled by default)
  --api                  # enable API-specific tests (disabled by default)
  --run-tests            # execute generated tests after creation

.venv/bin/agentic-qa plan <repo-url>   # dry run — strategist only, no code generated

# ── Platform / multi-repo ────────────────────────────────────────────────────
.venv/bin/agentic-qa init-platform <repo-url>... [--name my-platform] [--output platform.yaml]
# Auto-detects service roles (frontend/backend/infra) → generates platform.yaml

.venv/bin/agentic-qa analyze-platform platform.yaml
  --budget 20.00             # USD cap for the entire platform run
  --resume / --no-resume     # resume from last checkpoint (default: --resume)
  --scanner-concurrency 10   # parallel service scanners (default 10)
  --repo-concurrency 5       # parallel per-service QA runs (default 5)
  --concurrency 3            # parallel specialists within each service
  --no-contract              # skip Pact contract test generation
  --no-per-service           # skip per-service test generation (contracts only)
  --no-security --no-perf --no-e2e --no-lint --integration --api

.venv/bin/agentic-qa plan-platform platform.yaml [--resume/--no-resume]
# Dry run — discovers architecture + contracts only, no code generated
```

Example `platform.yaml`:
```yaml
name: my-platform

# Multi-repo style:
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

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v

# Key test files:
# tests/test_models.py          — Pydantic model validation
# tests/test_tools.py           — repo_tools pagination, search_code
# tests/test_checkpoint.py      — checkpoint save/load/resume/reset_crashed
# tests/test_context_window.py  — sliding window eviction
# tests/test_cost_tracker.py    — CostTracker accumulation, budget enforcement, BaseAgent integration
# tests/test_phase5.py          — ServiceScannerAgent, PlatformSynthesizerAgent, ServiceSummary
# tests/test_validator.py       — PostGenerationValidator ruff/eslint
# tests/test_session.py         — InteractiveSession, SessionState
# tests/test_platform.py        — PlatformOrchestrator flow
# tests/test_platform_init.py   — detect_role heuristics
# tests/test_repo_ingestor.py   — RepoIngestor clone logic
```

---

## Design decisions

### Core pipeline
- **Structured output**: every "emit" tool (emit_test_plan, emit_service_summary, emit_platform_architecture) takes validated Pydantic JSON — no free-text parsing
- **E2E gate**: StrategistAgent only emits an `e2e` plan entry when a web frontend is detected (React/Vue/Angular/Next.js/Svelte); pure API backends never get E2E entries
- **Tool injection**: tools are injected per agent via `functools.partial(fn, repo_root=path)` — each agent is scoped to its own service directory
- **Post-generation validation**: `PostGenerationValidator` runs after every specialist; ruff (E,F rules) for Python, eslint for JS/TS; lint errors surface in `SpecialistResult.errors`

### Concurrency model
- **Three semaphore layers**: `repo_concurrency_limit` (outer repos) → `scanner_concurrency_limit` (scanners in parallel) → `concurrency_limit` (specialists within one repo / contracts)
- **`return_exceptions=True`**: `asyncio.gather` never aborts healthy tasks on a single failure; `BudgetExceededError` is explicitly re-raised to propagate up
- **CostTracker thread-safety**: `asyncio.Lock` inside `record()`; `check_budget()` is sync (called after `await record()` so values are settled)

### Context management
- **Sliding window**: oldest (assistant tool_use, user tool_result) pair evicted whenever `len(messages) - 1 // 2 > max_context_tool_pairs`; initial user message always preserved
- **Two-tier scanner/synthesizer**: ServiceScannerAgent extracts compact ~600-token summaries per service; PlatformSynthesizerAgent reasons across all summaries (~30K total for 50 services) — this replaces the monolithic PlatformStrategistAgent approach which would overflow context at ~15 services
- **Prompt caching**: system prompt of every agent sent with `cache_control: ephemeral` → ~90% input token savings on repeated iterations

### Resilience
- **Exponential backoff**: `base × 2^attempt`, capped at `retry_max_wait_secs`; handles `RateLimitError` and `APIStatusError`; single code path in `BaseAgent._run_loop`, all agents benefit automatically
- **Checkpointing**: `PlatformCheckpoint` written atomically (`.tmp` → `os.replace`) after every significant phase; `reset_crashed_states()` detects "running" → "pending" on resume; `--no-resume` deletes checkpoint and starts fresh
- **Scanner resume**: completed `ServiceScannerAgent` results cached in `checkpoint.scan_results` — re-scans skipped on resume

### Role detection (init-platform)
- `detect_role(path)` heuristics: infra dirs (terraform/k8s) → `infra`; `package.json` with React/Vue/Next.js dep or `pages/public` dirs → `frontend`; any Python/Go/Java/Rust build file → `backend`; default → `backend`

---

## todo-app (end-to-end test target)

Sample app in `todo-app/` for testing agentic-qa against a real project.

- `todo-app/backend/` — FastAPI with POST/PUT/DELETE /todos, asyncpg + SQLAlchemy async
- `todo-app/frontend/` — Vanilla HTML/CSS/JS SPA (add / edit / delete / filter todos)
- `todo-app/docker-compose.yml` — PostgreSQL 16 container (user: todouser, db: tododb)
- `todo-app/deploy.sh` — one-command setup: starts Docker postgres, Python venv, uvicorn (8000), http.server (3000)
- `todo-platform.yaml` — platform descriptor treating todo-app as a monorepo with `api` + `web` services

```bash
cd todo-app
bash deploy.sh        # start postgres container + API + web server
bash deploy.sh --stop # stop API + web server (postgres data volume kept)
```

URLs when running: UI → http://localhost:3000 · API → http://localhost:8000 · Docs → http://localhost:8000/docs

```bash
# Test against the todo-app:
.venv/bin/agentic-qa plan todo-app/backend            # single-service dry run
.venv/bin/agentic-qa analyze todo-app/backend         # full single-repo test gen
.venv/bin/agentic-qa plan-platform todo-platform.yaml # platform dry run (discovers contracts)
.venv/bin/agentic-qa analyze-platform todo-platform.yaml --budget 5.00
```
