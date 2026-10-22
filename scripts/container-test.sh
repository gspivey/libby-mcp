#!/usr/bin/env bash
# Container test for the standalone image: builds the root Dockerfile through
# docker-compose.yml (the same files a user runs) and drives it end to end.
#
#   1. Image shape: non-root uid 10001 (AC-E3.1), /app holds only src/
#      (multi-stage, AC-E3.5), port published on loopback only (AC-E3.3).
#   2. Static-Bearer mode (LIBBY_MCP_API_KEY set): healthy within 30s via the
#      compose healthcheck (AC-E3.4), then the MCP protocol suite (AC-E2.1/E2.4).
#   3. No-auth mode (LIBBY_MCP_API_KEY unset): healthy, then the protocol suite
#      without a Bearer token (AC-E2.2).
#
# Used by `make integration-test` and the `local-integration` CI job. Needs
# Docker with compose v2 and the dev requirements (pytest, httpx) installed.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml)
SERVICE=libby-mcp
ENDPOINT=http://127.0.0.1:8000/mcp
TEST_KEY=ci-test-key

finish() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- container logs ---"
    "${COMPOSE[@]}" logs --no-color || true
  fi
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  exit "$status"
}
trap finish EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Start the service and wait for the compose healthcheck to report healthy.
# $1 = API key, or empty for no-auth mode (the var is then removed from the
# host env so the list-form compose entry passes nothing into the container).
start() {
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  if [ -n "$1" ]; then
    LIBBY_MCP_API_KEY="$1" "${COMPOSE[@]}" up -d --wait --wait-timeout 30
  else
    env -u LIBBY_MCP_API_KEY "${COMPOSE[@]}" up -d --wait --wait-timeout 30
  fi
}

# $1 = auth mode (bearer|none), $2 = API key the suite sends in bearer mode.
run_suite() {
  RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT="$ENDPOINT" MCP_AUTH_MODE="$1" MCP_API_KEY="$2" \
    pytest tests/integration/test_mcp_protocol.py -v -m local_integration
}

echo "=== build standalone image (root Dockerfile)"
"${COMPOSE[@]}" build

echo "=== static-Bearer mode"
start "$TEST_KEY"

uid=$("${COMPOSE[@]}" exec -T "$SERVICE" id -u)
[ "$uid" = "10001" ] || fail "container runs as uid $uid, expected 10001 (appuser)"

contents=$("${COMPOSE[@]}" exec -T "$SERVICE" ls -A /app)
[ "$contents" = "src" ] || fail "/app should hold only src/, found: $(echo "$contents" | tr '\n' ' ')"

port=$("${COMPOSE[@]}" port "$SERVICE" 8000)
[ "$port" = "127.0.0.1:8000" ] || fail "port 8000 published on $port, expected 127.0.0.1:8000"

run_suite bearer "$TEST_KEY"

echo "=== no-auth mode"
start ""
run_suite none ""

echo "=== container test passed"
