#!/usr/bin/env bash
# Run the official contract script (stages E1 and E2) against a freshly built composed stack.
# Used by `make e2e` and by the CI `e2e` job, so both do exactly the same thing.
#
#   ZAI_ADMIN_PASSWORD=... scripts/e2e.sh     (a random password is generated if unset)
#
# Needs: docker compose, uv, node 22, python3. Port 8000 must be free (stop `make dev` first).
# E2E_KEEP=1 leaves the stack running afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."

# Hard-coded on purpose: the cleanup runs `down -v`, so a stray COMPOSE_PROJECT_NAME in the
# shell must never point it at the dev stack and its database volume.
export COMPOSE_PROJECT_NAME=zai-e2e
compose=(docker compose -f docker-compose.yml -f compose.ci.yml)
api_url="http://localhost:8000"
out_dir="$(mktemp -d)"

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i.bak "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env && rm -f .env.bak
fi
ZAI_ADMIN_PASSWORD="${ZAI_ADMIN_PASSWORD:-$(openssl rand -hex 12)}"
export ZAI_ADMIN_PASSWORD

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "::group::docker compose logs"
    "${compose[@]}" logs --no-color --tail=300 || true
    echo "::endgroup::"
  fi
  if [ "${E2E_KEEP:-0}" != "1" ]; then
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$out_dir"
  exit "$status"
}
trap cleanup EXIT

echo "==> starting the stack (runtime image)"
"${compose[@]}" up -d --build --wait

echo "==> migrating"
"${compose[@]}" exec -T api alembic upgrade head

echo "==> creating the admin"
"${compose[@]}" exec -T -e ZAI_ADMIN_PASSWORD api python -m app.cli create-admin --username admin

echo "==> seeding the sample data (F11)"
make --no-print-directory seed ZAI_API_URL="$api_url"

for stage in E1 E2; do
  echo "==> official suite, stage $stage"
  node contract/zai-tests.mjs --app "$api_url" --user admin --pass "$ZAI_ADMIN_PASSWORD" \
    --stage "$stage" | tee "$out_dir/$stage.txt"
  if [ "$stage" = "E1" ]; then
    python3 backend/scripts/check_official_output.py --min-tests 23 "$out_dir/$stage.txt"
  else
    python3 backend/scripts/check_official_output.py --min-tests 45 --allow-deployment-only \
      "$out_dir/$stage.txt"
  fi
done
echo "==> e2e OK"
