#!/usr/bin/env bash
# deploy.sh — Set up PostgreSQL, install deps, and start the Todo app servers.
# Usage: bash deploy.sh [--stop]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
VENV_DIR="$SCRIPT_DIR/.venv"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/.logs"

DB_NAME="tododb"
DB_USER="todouser"
DB_PASS="todopass"
DB_PORT="5432"
API_PORT="8000"
WEB_PORT="3000"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[deploy]${NC} $*"; }
error()   { echo -e "${RED}[deploy]${NC} $*"; exit 1; }

mkdir -p "$PID_DIR" "$LOG_DIR"

# ── Stop mode ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
  info "Stopping servers..."
  for pidfile in "$PID_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    pid=$(cat "$pidfile")
    name=$(basename "$pidfile" .pid)
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && info "Stopped $name (PID $pid)"
    fi
    rm -f "$pidfile"
  done
  info "Stopping PostgreSQL container..."
  docker-compose -f "$SCRIPT_DIR/docker-compose.yml" stop
  info "Done. (Data volume preserved — run 'docker-compose down -v' to wipe it.)"
  exit 0
fi

# ── PostgreSQL via Docker ─────────────────────────────────────────────────────
info "Checking Docker..."
command -v docker &>/dev/null || error "Docker not found. Install Docker Desktop from https://docs.docker.com/get-docker/"
docker info &>/dev/null        || error "Docker daemon is not running. Please start Docker and retry."

info "Starting PostgreSQL container (docker-compose.yml)..."
docker-compose -f "$SCRIPT_DIR/docker-compose.yml" up -d --wait

info "Waiting for PostgreSQL to be healthy..."
for i in $(seq 1 30); do
  status=$(docker inspect --format='{{.State.Health.Status}}' todo-postgres 2>/dev/null || echo "missing")
  [[ "$status" == "healthy" ]] && break
  sleep 1
done
[[ "$(docker inspect --format='{{.State.Health.Status}}' todo-postgres 2>/dev/null)" == "healthy" ]] \
  || error "PostgreSQL container did not become healthy. Run: docker logs todo-postgres"

info "PostgreSQL is ready on port $DB_PORT."
export DATABASE_URL="postgresql+asyncpg://$DB_USER:$DB_PASS@localhost:$DB_PORT/$DB_NAME"

# ── Python virtual environment ────────────────────────────────────────────────
info "Setting up Python environment..."

PYTHON="python3"
command -v "$PYTHON" &>/dev/null || error "python3 not found. Please install Python 3.11+."

PY_VERSION=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')
if [[ "$PY_VERSION" -lt 11 ]]; then
  error "Python 3.11+ is required. Found: $("$PYTHON" --version)"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtual environment at $VENV_DIR..."
  "$PYTHON" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
info "Installing backend dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$BACKEND_DIR/requirements.txt"

# ── Start API server ──────────────────────────────────────────────────────────
API_PID_FILE="$PID_DIR/api.pid"
if [[ -f "$API_PID_FILE" ]] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
  warn "API server already running (PID $(cat "$API_PID_FILE")). Skipping."
else
  info "Starting API server on http://localhost:$API_PORT ..."
  DATABASE_URL="$DATABASE_URL" \
  uvicorn main:app \
    --host 0.0.0.0 \
    --port "$API_PORT" \
    --app-dir "$BACKEND_DIR" \
    --log-level info \
    >> "$LOG_DIR/api.log" 2>&1 &
  echo $! > "$API_PID_FILE"
  info "API server PID: $(cat "$API_PID_FILE") | Logs: $LOG_DIR/api.log"

  # Wait for it to be ready
  info "Waiting for API to be ready..."
  for i in $(seq 1 20); do
    if curl -sf "http://localhost:$API_PORT/health" > /dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  curl -sf "http://localhost:$API_PORT/health" > /dev/null \
    || error "API server did not start in time. Check $LOG_DIR/api.log"
  info "API server is ready."
fi

# ── Start web server ──────────────────────────────────────────────────────────
WEB_PID_FILE="$PID_DIR/web.pid"
if [[ -f "$WEB_PID_FILE" ]] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
  warn "Web server already running (PID $(cat "$WEB_PID_FILE")). Skipping."
else
  info "Starting web server on http://localhost:$WEB_PORT ..."
  "$PYTHON" -m http.server "$WEB_PORT" \
    --directory "$FRONTEND_DIR" \
    >> "$LOG_DIR/web.log" 2>&1 &
  echo $! > "$WEB_PID_FILE"
  info "Web server PID: $(cat "$WEB_PID_FILE") | Logs: $LOG_DIR/web.log"
  sleep 1
  kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null \
    || error "Web server failed to start. Check $LOG_DIR/web.log"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Todo App is running!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  UI  →  http://localhost:$WEB_PORT"
echo -e "  API →  http://localhost:$API_PORT"
echo -e "  API docs →  http://localhost:$API_PORT/docs"
echo ""
echo -e "  Stop servers: bash deploy.sh --stop"
echo -e "  API logs:     tail -f $LOG_DIR/api.log"
echo -e "  Web logs:     tail -f $LOG_DIR/web.log"
echo ""
