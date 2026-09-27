#!/usr/bin/env bash
# The single entrypoint for Incident Operations and local monitoring validation.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
MODE=local
STATIC=1
VALIDATE=0
for arg in "$@"; do
  case "$arg" in
    --mock|--configured)
      selected="${arg#--}"
      if [ "$MODE" != local ] && [ "$MODE" != "$selected" ]; then
        echo "error: --mock and --configured are mutually exclusive" >&2; exit 2
      fi
      MODE="$selected" ;;
    --dev) STATIC=0 ;;
    --static) STATIC=1 ;;
    --validate-monitoring) VALIDATE=1 ;;
    -h|--help)
      cat <<'HELP'
Usage: ./start.sh [--mock | --configured] [--dev | --static]
       ./start.sh --validate-monitoring
Default: local Prometheus :9090 + Alertmanager :9093, static UI :8100.
--mock: source fixtures :9999; Fake notifications and Fake model.
--configured: saved registry; real configured notification/model providers.
--dev: Vite :5174; --static: same-origin UI on backend port (default).
--validate-monitoring: validate bundled monitoring configs and exit (Docker).
Optional backend/.env: four INCIDENT_OPERATIONS bootstrap values only.
Process environment overrides the file. Relative database paths use repo root.
Local/mock startup reapplies demo sources and monitoring connections.
Ctrl-C stops only this launcher's processes/created monitoring resources.
Databases and master key persist. Reused monitoring containers stay running.
HELP
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
CONFIG_DIR="$ROOT/local-monitoring"
PIDS=()
CREATED_CONTAINERS=()
NETWORK_CREATED=0
LAUNCH_ID="$$-$RANDOM-$RANDOM"
LAUNCH_LABEL="com.ai-observability-workbench.launch-id"
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    for _ in {1..50}; do
      if ! kill -0 -- "-$pid" 2>/dev/null && ! kill -0 "$pid" 2>/dev/null; then break; fi
      sleep 0.1
    done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  for name in "${CREATED_CONTAINERS[@]:-}"; do
    [ -n "$name" ] || continue
    remove_owned_container "$name" || true
  done
  if [ "$NETWORK_CREATED" -eq 1 ] && [ "$(docker network inspect --format "{{ index .Labels \"$LAUNCH_LABEL\" }}" "$NETWORK" 2>/dev/null || true)" = "$LAUNCH_ID" ]; then docker network rm "$NETWORK" >/dev/null || true; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.13.1"
ALERTMANAGER_IMAGE="quay.io/prometheus/alertmanager:v0.32.1"
PROMETHEUS_CONTAINER="awo-local-prometheus"
ALERTMANAGER_CONTAINER="awo-local-alertmanager"
NETWORK="awo-local-monitoring"
STACK_LABEL="com.ai-observability-workbench.local-monitoring=true"
LABEL_KEY="${STACK_LABEL%%=*}"

die() {
  echo "error: $*" >&2
  exit 1
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is required for the local monitoring stack"
  docker info >/dev/null 2>&1 || die "Docker is installed but the Docker engine is not running"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_is_ours() {
  [ "$(docker container inspect --format "{{ index .Config.Labels \"$LABEL_KEY\" }}" "$1" 2>/dev/null || true)" = "true" ]
}

container_running() {
  [ "$(docker container inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)" = "true" ]
}

network_is_ours() {
  [ "$(docker network inspect --format "{{ index .Labels \"$LABEL_KEY\" }}" "$NETWORK" 2>/dev/null || true)" = "true" ]
}

port_available() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    [ -z "$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)" ]
    return
  fi
  ! python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(.2); result=s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); raise SystemExit(0 if result == 0 else 1)' "$port"
}

wait_for_port_release() {
  local port="$1"
  for _ in {1..20}; do
    if port_available "$port"; then
      return
    fi
    sleep 0.25
  done
  return 1
}

remove_owned_container() {
  local name="$1"
  if ! container_exists "$name"; then
    return
  fi
  [ "$(docker container inspect --format "{{ index .Config.Labels \"$LAUNCH_LABEL\" }}" "$name" 2>/dev/null || true)" = "$LAUNCH_ID" ] || return 0
  if container_running "$name"; then
    docker stop --time 10 "$name" >/dev/null
  fi
  docker rm "$name" >/dev/null
}

status_stack() {
  require_docker
  container_running "$PROMETHEUS_CONTAINER" && container_is_ours "$PROMETHEUS_CONTAINER" \
    && container_running "$ALERTMANAGER_CONTAINER" && container_is_ours "$ALERTMANAGER_CONTAINER"
}

wait_for_url() {
  local url="$1"
  local label="$2"
  for _ in {1..60}; do
    if curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1; then
      return
    fi
    sleep 0.5
  done
  die "$label did not become ready at $url"
}

start_stack() {
  require_docker
  command -v curl >/dev/null 2>&1 || die "curl is required for local readiness checks"
  if status_stack; then
    echo "local monitoring stack is already running"
    return
  fi

  if container_exists "$PROMETHEUS_CONTAINER" || container_exists "$ALERTMANAGER_CONTAINER"; then
    die "partial or stopped monitoring stack exists; inspect its containers before restarting"
  fi
  wait_for_port_release 9090 || die "Prometheus port 9090 is already in use"
  wait_for_port_release 9093 || die "Alertmanager port 9093 is already in use"

  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    network_is_ours || die "network $NETWORK exists but is not owned by this repository"
  else
    NETWORK_CREATED=1
    docker network create --label "$STACK_LABEL" --label "$LAUNCH_LABEL=$LAUNCH_ID" "$NETWORK" >/dev/null
  fi

  CREATED_CONTAINERS+=("$ALERTMANAGER_CONTAINER")
  docker run --detach \
    --name "$ALERTMANAGER_CONTAINER" \
    --network "$NETWORK" \
    --network-alias alertmanager \
    --label "$STACK_LABEL" --label "$LAUNCH_LABEL=$LAUNCH_ID" \
    --publish 127.0.0.1:9093:9093 \
    --volume "$CONFIG_DIR/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
    "$ALERTMANAGER_IMAGE" \
    --config.file=/etc/alertmanager/alertmanager.yml \
    --storage.path=/alertmanager >/dev/null

  CREATED_CONTAINERS+=("$PROMETHEUS_CONTAINER")
  docker run --detach \
    --name "$PROMETHEUS_CONTAINER" \
    --network "$NETWORK" \
    --label "$STACK_LABEL" --label "$LAUNCH_LABEL=$LAUNCH_ID" \
    --publish 127.0.0.1:9090:9090 \
    --volume "$CONFIG_DIR/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    --volume "$CONFIG_DIR/alerts.yml:/etc/prometheus/alerts.yml:ro" \
    "$PROMETHEUS_IMAGE" \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/prometheus \
    --web.enable-lifecycle >/dev/null

  wait_for_url "http://127.0.0.1:9090/-/ready" "Prometheus"
  wait_for_url "http://127.0.0.1:9093/-/ready" "Alertmanager"
  echo "local monitoring stack started"
  echo "Prometheus  http://127.0.0.1:9090"
  echo "Alertmanager http://127.0.0.1:9093"
}

validate_configs() {
  require_docker
  docker run --rm \
    --entrypoint /bin/promtool \
    --volume "$CONFIG_DIR/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    --volume "$CONFIG_DIR/alerts.yml:/etc/prometheus/alerts.yml:ro" \
    "$PROMETHEUS_IMAGE" check config /etc/prometheus/prometheus.yml
  docker run --rm \
    --entrypoint /bin/amtool \
    --volume "$CONFIG_DIR/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
    "$ALERTMANAGER_IMAGE" check-config /etc/alertmanager/alertmanager.yml
}

if [ "$VALIDATE" -eq 1 ]; then validate_configs; exit 0; fi
PY="$ROOT/.venv/bin/python"
PIP="$ROOT/.venv/bin/pip"
[ -x "$PY" ] || die "Python venv missing; create .venv with Python 3.14 and install backend dependencies"
command -v npm >/dev/null 2>&1 || die "npm not found on PATH"
command -v curl >/dev/null 2>&1 || die "curl is required for readiness checks"
if ! "$PY" -c 'import fastapi, cryptography, dotenv' >/dev/null 2>&1; then
  (cd backend && "$PIP" install -e '.[dev]')
fi
# Parse data, never source/eval the file. Unknown legacy bootstrap keys fail clearly.
BOOTSTRAP_FILE="$(mktemp)"
if ! "$PY" - "$ROOT/backend/.env" > "$BOOTSTRAP_FILE" <<'PY'
import sys
from pathlib import Path
from dotenv import dotenv_values
values = dotenv_values(sys.argv[1], interpolate=False) if Path(sys.argv[1]).exists() else {}
keys = ('DATABASE_PATH', 'HOST', 'PORT', 'FRONTEND_PORT')
if any('ALERT_WORKBENCH_' + key in values for key in keys):
    sys.exit('error: legacy ALERT_WORKBENCH bootstrap keys in backend/.env; migrate using backend/.env.example')
for key in keys:
    name = 'INCIDENT_OPERATIONS_' + key
    value = values.get(name)
    if value is not None:
        if not value or '\n' in value or '\r' in value or '\0' in value:
            sys.exit('error: invalid bootstrap value: ' + name)
        sys.stdout.write(name + '\0' + value + '\0')
PY
then rm -f "$BOOTSTRAP_FILE"; exit 1; fi
while IFS= read -r -d '' name && IFS= read -r -d '' value; do
  if ! printenv "$name" >/dev/null; then export "$name=$value"; fi
done < "$BOOTSTRAP_FILE"
rm -f "$BOOTSTRAP_FILE"
BACKEND_HOST="${INCIDENT_OPERATIONS_HOST:-127.0.0.1}"
BACKEND_PORT="${INCIDENT_OPERATIONS_PORT:-8100}"
FRONTEND_PORT="${INCIDENT_OPERATIONS_FRONTEND_PORT:-5174}"
TRUSTED_HOSTS="${INCIDENT_OPERATIONS_TRUSTED_HOSTS:-}"
DATABASE="${INCIDENT_OPERATIONS_DATABASE_PATH:-$ROOT/backend/data/workbench.db}"
MASTER_KEY="${INCIDENT_OPERATIONS_MASTER_KEY_PATH:-$ROOT/backend/data/master.key}"
if [ -z "${INCIDENT_OPERATIONS_DATABASE_PATH:-}" ]; then
  case "$MODE" in
    local) DATABASE="$ROOT/backend/data/incident-operations-local.db" ;;
    mock) DATABASE="$ROOT/backend/data/incident-operations-mock.db" ;;
  esac
fi
case "$DATABASE" in /*) ;; *) DATABASE="$ROOT/$DATABASE" ;; esac
case "$MASTER_KEY" in /*) ;; *) MASTER_KEY="$ROOT/$MASTER_KEY" ;; esac
if [ "$BACKEND_HOST" = '0.0.0.0' ] && [ -z "$TRUSTED_HOSTS" ]; then die '0.0.0.0 requires INCIDENT_OPERATIONS_TRUSTED_HOSTS'; fi
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  case "$port" in ''|*[!0-9]*) die 'ports must be integers between 1 and 65535' ;; esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die 'ports must be between 1 and 65535'
done
port_available "$BACKEND_PORT" || die "backend port $BACKEND_PORT is already in use"
if [ "$STATIC" -eq 0 ]; then
  [ "$BACKEND_PORT" != "$FRONTEND_PORT" ] || die 'backend and frontend ports must differ'
  port_available "$FRONTEND_PORT" || die "frontend port $FRONTEND_PORT is already in use"
fi
if [ "$MODE" = mock ]; then port_available 9999 || die 'mock port 9999 is already in use'; fi
printf '\nMode            %s\nDatabase        %s\nBackend port    %s\n' "$MODE" "$DATABASE" "$BACKEND_PORT"
case "$MODE" in
  local) echo 'Notifications   Fake'; echo 'Model           real provider, explicit operator action only' ;;
  mock) echo 'Notifications   Fake'; echo 'Model           Fake' ;;
  configured) echo 'Notifications   real saved providers (pending deliveries may resume)'; echo 'Model           real provider, explicit operator action only' ;;
esac
if [ "$MODE" != configured ]; then echo 'Demo seed       reapplies named demo sources/connections; re-enables sources'; fi
echo 'On exit         database/key persist; only owned processes and newly created monitoring resources stop'
[ "$BACKEND_HOST" = '127.0.0.1' ] || echo 'Network         trusted network only; no account authentication or TLS'
if [ ! -d operations-console/node_modules ]; then npm --prefix operations-console install; fi
if [ "$STATIC" -eq 1 ] || [ ! -f operations-console/dist/index.html ]; then npm --prefix operations-console run build; fi
if [ ! -s "$MASTER_KEY" ] && [ "$MASTER_KEY" = "$ROOT/backend/data/master.key" ]; then
  (cd backend && "$PY" -c 'from app.master_key import master_key; master_key()')
fi
[ -s "$MASTER_KEY" ] || die "master key missing or empty: $MASTER_KEY"
export INCIDENT_OPERATIONS_DATABASE_PATH="$DATABASE" INCIDENT_OPERATIONS_MASTER_KEY_PATH="$MASTER_KEY"
export INCIDENT_OPERATIONS_HOST="$BACKEND_HOST" INCIDENT_OPERATIONS_PORT="$BACKEND_PORT"
export INCIDENT_OPERATIONS_FRONTEND_PORT="$FRONTEND_PORT" INCIDENT_OPERATIONS_TRUSTED_HOSTS="$TRUSTED_HOSTS"
# A process group includes npm's Vite child, so shutdown does not strand it.
spawn() {
  "$PY" -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1],sys.argv[1:])' "$@" &
  PIDS+=("$!")
}
case "$MODE" in
  local)
    export INCIDENT_OPERATIONS_NOTIFICATION_FAKE=1
    unset INCIDENT_OPERATIONS_MODEL_FAKE
    start_stack ;;
  mock)
    export INCIDENT_OPERATIONS_NOTIFICATION_FAKE=1 INCIDENT_OPERATIONS_MODEL_FAKE=1
    spawn "$PY" scripts/mock_alertmanager.py ;;
  configured) unset INCIDENT_OPERATIONS_NOTIFICATION_FAKE INCIDENT_OPERATIONS_MODEL_FAKE ;;
esac
spawn "$PY" -m uvicorn app.operations_console:create_app --factory --timeout-graceful-shutdown 5 --app-dir "$ROOT/backend" --host "$BACKEND_HOST" --port "$BACKEND_PORT"
check_children() {
  local pid status
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      status=0; wait "$pid" || status=$?
      echo "error: child process $pid exited unexpectedly ($status)" >&2
      [ "$status" -ne 0 ] || status=1
      exit "$status"
    fi
  done
}
CONNECT_HOST="$BACKEND_HOST"
DISPLAY_HOST="$BACKEND_HOST"
if [ "$BACKEND_HOST" = '0.0.0.0' ]; then CONNECT_HOST=127.0.0.1; DISPLAY_HOST="${TRUSTED_HOSTS%%,*}"; fi
wait_app() {
  local port="$1" ready=0
  for _ in {1..60}; do
    check_children
    if curl --fail --silent --max-time 1 -H "Host: $DISPLAY_HOST:$port" "http://$CONNECT_HOST:$port/" >/dev/null; then ready=1; break; fi
    sleep 0.5
  done
  [ "$ready" -eq 1 ] || die "application did not become ready on port $port"
  check_children
}
wait_app "$BACKEND_PORT"
case "$MODE" in
  local) "$PY" scripts/seed_operations_console.py --local ;;
  mock) "$PY" scripts/seed_operations_console.py ;;
esac
UI_PORT="$BACKEND_PORT"
if [ "$STATIC" -eq 0 ]; then
  spawn npm --prefix operations-console run dev
  UI_PORT="$FRONTEND_PORT"
  wait_app "$UI_PORT"
fi
check_children
printf '\nOperations API  http://%s:%s\nOperations UI   http://%s:%s\nReady. Press Ctrl-C to stop.\n' "$DISPLAY_HOST" "$BACKEND_PORT" "$DISPLAY_HOST" "$UI_PORT"
while true; do check_children; sleep 0.5; done
