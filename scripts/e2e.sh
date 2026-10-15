#!/usr/bin/env bash
# Run the official contract script (stages E1 and E2) against a freshly built composed stack.
# Used by `make e2e` and by the CI `e2e` job, so both do exactly the same thing.
#
#   ZAI_ADMIN_PASSWORD=... scripts/e2e.sh     (a random password is generated if unset)
#
# Needs: docker compose, uv, node 22, python3, curl. Port 8000 must be free (stop `make dev` first).
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

echo "==> seeding the sample data through the generator (F11)"
# Synthetic (offline) so the job never depends on Open-Meteo being up; `make seed` uses real data.
make --no-print-directory seed ZAI_API_URL="$api_url" SEED_ARGS="--source synthetic"

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
# Contract fuzzing LAST: it deletes and changes data (series, sensors) at random. Logout and the
# password change are left out, they would end the admin session or change the admin password.
# The two accepted findings (extensions beyond the contract) are in the baseline file. The
# stateful phase is left out: with a baseline its exit status flipped between runs although every
# failure it reported was already accepted (measured: 1 of 2 runs), and the workflows it chains
# are covered by the official script and the pytest suite.
echo "==> contract fuzzing (Schemathesis)"
admin_token=$(curl -fsS -X POST "$api_url/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ZAI_ADMIN_PASSWORD\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])')
series_id=$(curl -fsS "$api_url/api/series" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
sensor_key=$(curl -fsS -X POST "$api_url/api/sensors" -H "Authorization: Bearer $admin_token" \
  -H 'Content-Type: application/json' -d "{\"name\":\"schemathesis\",\"seriesId\":$series_id}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["apiKey"])')
uv run --frozen --project backend --group fuzz schemathesis run contract/zai-api-26z.yaml \
  --url "$api_url" \
  -H "Authorization: Bearer $admin_token" -H "X-API-Key: $sensor_key" \
  --exclude-path /api/auth/logout --exclude-path /api/auth/password \
  --checks not_a_server_error,status_code_conformance,content_type_conformance,response_schema_conformance \
  --baseline scripts/schemathesis-baseline.json \
  --phases examples,coverage,fuzzing --seed 20261015 --no-color
echo "==> e2e OK"
