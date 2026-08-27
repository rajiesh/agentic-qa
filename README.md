# agentic-qa

AI-powered QA test generator. Point it at a repository and it analyses the codebase, builds a test plan, then writes runnable test code for you — pytest, Locust, OWASP ZAP configs, and Playwright E2E specs.

## How it works

A **Strategist Agent** reads the repo, understands the architecture, and produces a structured test plan. From that plan, **Specialist Agents** run in parallel to generate the actual test files:

| Specialist | Output | On by default |
|---|---|---|
| Functional | pytest / Jest | ✅ |
| Performance | Locust / k6 | ✅ |
| Security | ZAP config + probes | ✅ |
| E2E | Playwright (web apps only) | ✅ |
| Integration | pytest with containers | ❌ |
| API | pytest + httpx | ❌ |

## Setup

```bash
git clone <this-repo> && cd agentic-qa
uv pip install -e ".[dev]"
cp .env.example .env        # add your ANTHROPIC_API_KEY
```

## Usage

```bash
# Generate tests for a repo
agentic-qa analyze https://github.com/org/myapp

# Add a documentation URL for extra context
agentic-qa analyze https://github.com/org/myapp --doc https://docs.myapp.com

# Preview the test plan without generating any code
agentic-qa plan https://github.com/org/myapp

# Control which specialists run
agentic-qa analyze <repo> --no-security --no-perf --integration --api --no-e2e
```

Generated files land in `outputs/<repo-name>/<run-id>/` organised by test type.

## Run the tests for agentic-qa itself

```bash
.venv/bin/python -m pytest tests/ -v
```
