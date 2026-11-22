#!/bin/sh
# The image's entrypoint (under tini): `odcl` with the container's arguments. Before `serve`, bring the database's
# schema up to date, which changes nothing when it already is, unless ODCL_MIGRATE=false.
set -eu
if [ "${1:-}" = "serve" ] && [ "${ODCL_MIGRATE:-true}" != "false" ]; then
  odcl migrate
fi
exec odcl "$@"
