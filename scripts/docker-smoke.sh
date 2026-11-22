#!/usr/bin/env bash
# Check a built image the way people run it: against a PostgreSQL in its own container, migrating on start, serving
# the API and the page, answering its health check, running as an unprivileged user under tini, and stopping quickly.
#
#   scripts/docker-smoke.sh IMAGE [PLATFORM]      e.g. scripts/docker-smoke.sh odcl:check linux/arm64
set -euo pipefail
image=${1:?name the image}
platform=${2:-}
run=(docker run ${platform:+--platform "$platform"})
id=$$
net=odcl-smoke-$id db=odcl-smoke-db-$id app=odcl-smoke-app-$id

cleanup() {
  [ "${ok:-}" = yes ] || docker logs "$app" 2>&1 | tail -40 || true
  docker rm -f "$app" "$db" > /dev/null 2>&1 || true
  docker network rm "$net" > /dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

docker network create "$net" > /dev/null
docker run -d --name "$db" --network "$net" -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=odcl postgres:16-alpine > /dev/null
for _ in $(seq 60); do docker exec "$db" pg_isready -U postgres -d odcl > /dev/null 2>&1 && break; sleep 1; done
docker exec "$db" pg_isready -U postgres -d odcl > /dev/null || fail "PostgreSQL did not start"
url=postgresql://postgres:smoke@$db/odcl

[ "$("${run[@]}" --rm --entrypoint id "$image" -u)" = 1000 ] || fail "the image does not run as uid 1000"
"${run[@]}" --rm "$image" --help | grep -q "odcl" || fail "odcl --help"

"${run[@]}" -d --name "$app" --network "$net" -e ODCL_DATABASE_URL="$url" -p 127.0.0.1::8080 "$image" > /dev/null
port=$(docker port "$app" 8080/tcp | head -1 | sed 's/.*://')
base=http://127.0.0.1:$port
for _ in $(seq 60); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$app")" = healthy ] && break
  [ "$(docker inspect --format '{{.State.Running}}' "$app")" = true ] || fail "the server stopped"
  sleep 1
done
[ "$(docker inspect --format '{{.State.Health.Status}}' "$app")" = healthy ] || fail "the health check did not pass"

[ "$(docker exec "$app" cat /proc/1/comm)" = tini ] || fail "tini is not PID 1"
curl -fsS "$base/v1/me" | grep -q '"user": null' || fail "GET /v1/me"
curl -fsS "$base/v1/overview" | grep -q '"counts"' || fail "GET /v1/overview: the database was not migrated"
curl -fsS "$base/" | grep -q "Open Data Context Layer" || fail "the web page"
status=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"name": "p", "description": "d"}' "$base/v1/projects")
[ "$status" = 401 ] || fail "a write without a token answered $status, not 401"
docker exec "$app" odcl add project smoke --description "Made inside the container" | grep -q "project:smoke  added" || fail "odcl inside the container"
curl -fsS "$base/v1/entries/project:smoke" | grep -q "Made inside the container" || fail "the server does not see what the command wrote"

start=$(date +%s)
docker stop -t 10 "$app" > /dev/null
[ $(( $(date +%s) - start )) -lt 8 ] || fail "docker stop took the full timeout: the signal did not reach odcl"
ok=yes
echo "ok: $image${platform:+ ($platform)}"
