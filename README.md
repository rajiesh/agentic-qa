# agentic-qa

AI-powered QA test generator. Point it at a repository (or an entire multi-service platform) and it analyses the codebase, builds a test plan, then writes runnable test code — pytest, Locust, OWASP ZAP configs, Playwright E2E specs, and Pact contract tests.

Run `agentic-qa` with no arguments to start an **interactive session**: have a conversation with the assistant, add repos and docs as you go, ask for a plan first or jump straight to code generation — no flags required.

## How it works

### Single repo

A **Strategist Agent** reads the repo, understands the architecture, and produces a structured test plan. **Specialist Agents** then run in parallel to write the actual test files:

| Specialist | Output | On by default |
|---|---|---|
| Functional | pytest / Jest | ✅ |
| Performance | Locust / k6 | ✅ |
| Security | ZAP config + probes | ✅ |
| E2E | Playwright (web apps only) | ✅ |
| Integration | pytest with containers | ❌ |
| API | pytest + httpx | ❌ |

### Multi-service platform

For applications spread across multiple repos (microservices, frontend + backend, infra), a **Platform Strategist Agent** explores all services simultaneously, discovers inter-service contracts (REST, gRPC, GraphQL, events, DB), and a **Contract Test Agent** generates Pact consumer/provider tests for each.

Describe your services once in a `platform.yaml`, then ask the session for a platform plan (contract map, no code written) or a full platform analysis (per-service + contract tests).

## Setup

```bash
git clone <this-repo> && cd agentic-qa
uv pip install -e ".[dev]"
cp .env.example .env        # add your ANTHROPIC_API_KEY
```

## Interactive session

Run `agentic-qa` with no arguments to start a conversational session. Claude acts as your QA assistant — you describe what you want in plain language; it handles the flags.

```
$ agentic-qa

 ╭─ agentic-qa ─────────────────────────────────────────────╮
 │  Multi-agent QA Analyst  •  type /help or just chat      │
 ╰───────────────────────────────────────────────────────────╯

You › I want to test https://github.com/acme/api — skip security

Agent › Got it. Added acme/api and disabled security tests.
        Want to see the test plan first, or generate the tests now?

You › Plan first

Agent › Running the strategist on acme/api …
        [progress]
        Found 3 test types: functional (critical), performance (high), e2e (medium).
        Ready to generate the tests?

You › Yes, go ahead

Agent › Generating tests …
        Done. Output in outputs/api-…/
        3 functional tests · 1 Locust script · 2 Playwright specs

You › /exit
```

### Slash commands

| Command | Description |
|---|---|
| `/repos` | List repos in the current session |
| `/docs` | List doc links in the current session |
| `/config` | Show which test types are enabled / disabled |
| `/runs` | Summarise completed runs this session |
| `/clear` | Reset repos, docs, and overrides (conversation history kept) |
| `/reset` | Full reset — state and conversation history |
| `/help` | Show all slash commands |
| `/exit` | Exit |

The conversation history is maintained for the life of the session so Claude has full context of everything you've added and run. To add repos or docs mid-session, just tell Claude ("also add https://…") or use `add_repos` naturally in conversation.

## Single-repo usage

All functionality is driven from the interactive session — just describe what you want:

```
You › Add https://github.com/org/myapp, docs are at https://docs.myapp.com
You › Show me the test plan first          # strategist only, no code generated
You › Skip security and perf, enable API tests
You › Go ahead and generate the tests
```

Local paths work too (e.g. `todo-app/backend`). Generated files land in `outputs/<repo-name>/<run-id>/` organised by test type.

## Multi-service platform workflow

### Step 1 — Write `platform.yaml`

```yaml
name: my-platform

services:
  - name: auth-service
    url: https://github.com/org/auth-service
    role: backend
    branch: main
  - name: web-frontend
    url: https://github.com/org/web-frontend
    role: frontend
    branch: main
docs:
  - https://wiki.internal/architecture
```

Add `doc_links` or `sparse_paths` per service where useful, then commit it alongside your code.

### Step 2 — Discover contracts (dry run)

```
You › Plan the platform in platform.yaml
```

Summarises all discovered inter-service contracts with no code generated — a good sanity check before a full run.

### Step 3 — Generate the full test suite

```
You › Run the full platform analysis for platform.yaml
```

Runs per-service functional/performance/security/E2E tests **and** Pact contract tests for every discovered contract. Output lands in `outputs/<platform-name>/<run-id>/`. Ask the session to skip test types (e.g. "skip contract tests") before running.

Platform runs resume from their last checkpoint. Say "start the platform run fresh" to ignore saved progress.

### Monorepo variant

If multiple services live in one repo, use the `repos:` shape:

```yaml
name: mono-platform
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

## Configuration

There are no command-line flags (apart from `-v` for debug logs). Runtime settings come from environment variables or `.env`:

| Variable | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Claude API key |
| `COST_BUDGET_USD` | unlimited | Abort a run once this USD spend is exceeded |
| `OUTPUT_DIR` | `outputs` | Where generated tests are written |
| `LINT_GENERATED` | `true` | Run ruff / eslint on generated files |
| `CONCURRENCY_LIMIT` | `3` | Parallel specialists per repo |
| `REPO_CONCURRENCY_LIMIT` | `5` | Parallel repos / services |

Test types are toggled conversationally ("skip security", "enable API tests").

## Run agentic-qa's own tests

```bash
.venv/bin/python -m pytest tests/ -v
```
