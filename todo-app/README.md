# Todo App

A small full-stack todo app used as an end-to-end test target for [agentic-qa](../README.md).
FastAPI backend + vanilla JS frontend, backed by PostgreSQL.

## Stack

- **Backend** — FastAPI, SQLAlchemy (async) + asyncpg, Pydantic v2 — `backend/`
- **Frontend** — Vanilla HTML/CSS/JS SPA (no build step) — `frontend/`
- **Database** — PostgreSQL 16, run via Docker Compose — `docker-compose.yml`

## Quick start

```bash
bash deploy.sh          # starts Postgres (Docker), API (uvicorn), and web server
bash deploy.sh --stop   # stops API + web server (Postgres data volume is kept)
```

`deploy.sh` will:
1. Start the `postgres` container from `docker-compose.yml` and wait for it to be healthy.
2. Create a Python virtualenv at `.venv/` and install `backend/requirements.txt`.
3. Start the API with `uvicorn` on port 8000.
4. Serve `frontend/` with `python3 -m http.server` on port 3000.

Requires Docker and Python 3.11+. PID files and logs are written to `.pids/` and `.logs/`.

| Service  | URL                          |
|----------|-------------------------------|
| UI       | http://localhost:3000        |
| API      | http://localhost:8000        |
| API docs | http://localhost:8000/docs   |

## Configuration

Copy `.env.example` to `.env` and adjust if needed:

```bash
DATABASE_URL=postgresql+asyncpg://todouser:todopass@localhost:5432/tododb
API_HOST=0.0.0.0
API_PORT=8000
```

## API

| Method | Path          | Description                                  |
|--------|---------------|-----------------------------------------------|
| GET    | `/todos`      | List all todos, newest first                  |
| POST   | `/todos`      | Create a todo (`title`, optional `description`) |
| PUT    | `/todos/{id}` | Update a todo's `title`, `description`, or `completed` |
| DELETE | `/todos/{id}` | Delete a todo                                  |
| GET    | `/health`     | Health check                                   |

A todo has: `id`, `title`, `description`, `completed`, `created_at`, `updated_at`.

## Project layout

```
todo-app/
├── backend/            # FastAPI app (main.py, models.py, schemas.py, database.py)
├── frontend/            # Static SPA (index.html, app.js, style.css)
├── docker-compose.yml   # PostgreSQL 16 container (todouser / tododb)
├── deploy.sh            # One-command setup and teardown
├── platform.yaml         # agentic-qa platform descriptor (backend + frontend services)
└── .env.example          # Sample environment configuration
```

## Using with agentic-qa

This app doubles as a sample target for exercising the agentic-qa test generator from the
repo root:

```bash
.venv/bin/agentic-qa plan todo-app/backend            # dry run — strategist only
.venv/bin/agentic-qa analyze todo-app/backend         # full single-repo test generation
.venv/bin/agentic-qa plan-platform todo-platform.yaml # platform dry run (discovers contracts)
.venv/bin/agentic-qa analyze-platform todo-platform.yaml --budget 5.00
```
