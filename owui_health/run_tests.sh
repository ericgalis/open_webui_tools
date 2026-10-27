#!/bin/sh
# Runs the tool tests inside the Open WebUI image (same Python, psycopg2, jsonschema and pydantic as in
# production), against a throwaway Postgres seeded by the health stack's own schema and loaders.
# Needs the compose repo's health/ stack (HEALTH_STACK_DIR); never touches health-db.
set -eu
cd "$(dirname "$0")"
stack="${HEALTH_STACK_DIR:-/bulk/virtualdisks/docker/compose/health}"
net="owui-health-test-$$"
pw="test-$$"
cleanup() { docker rm -f "$net-db" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t health-ingest:local "$stack" >/dev/null
docker network create "$net" >/dev/null
docker run -d --name "$net-db" --network "$net" -e POSTGRES_PASSWORD="$pw" \
  -v "$stack/db/init:/docker-entrypoint-initdb.d:ro" postgres:16-alpine >/dev/null
until docker exec "$net-db" pg_isready -U postgres -q 2>/dev/null \
      && docker exec "$net-db" psql -U postgres -tAc 'SELECT 1' >/dev/null 2>&1; do sleep 1; done
sleep 2   # the entrypoint restarts the server once after running the init scripts
until docker exec "$net-db" pg_isready -U postgres -q 2>/dev/null; do sleep 1; done

docker run --rm --network "$net" -v "$PWD/tests:/seed:ro" -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e HEALTH_DB_HOST="$net-db" -e HEALTH_TEST_ADMIN_PASSWORD="$pw" -e HEALTH_INBOX_DIR=/tmp/inbox \
  -e HEALTH_INGEST_PASSWORD_TESTER=ingest -e HEALTH_RO_PASSWORD_TESTER=ro \
  health-ingest:local python /seed/seed.py tester

docker run --rm --network "$net" --entrypoint sh -v "$PWD:/src:ro" \
  -v "$stack/food_constitution.json:/constitution.json:ro" -e HEALTH_CONSTITUTION=/constitution.json \
  -v "$stack/healthingest/foodmatch.py:/foodmatch.py:ro" -e HEALTH_FOODMATCH=/foodmatch.py \
  -e HEALTH_TEST_RO_DSN="postgresql://health_tester_ro:ro@$net-db:5432/health_tester" -e PYTHONDONTWRITEBYTECODE=1 \
  ghcr.io/open-webui/open-webui:main -c \
  "pip install -q --disable-pip-version-check pytest==9.1.1 >/dev/null 2>&1; cd /src && python -m pytest -q -p no:cacheprovider --rootdir=/tmp tests $*"
