# syntax=docker/dockerfile:1
# odcl on Debian 13 (trixie) and Python 3.14, published to ghcr.io/lawwu/open-data-context-layer by
# .github/workflows/docker.yml. Both stages start from the same official Python image, pinned by digest;
# Dependabot proposes newer ones (Python 3.14 only: a new minor waits for a person).
#
#   docker run -d --name odcl -p 127.0.0.1:8080:8080 \
#     -e ODCL_DATABASE_URL=postgresql://odcl:secret@db.example.com/odcl ghcr.io/lawwu/open-data-context-layer
#
# The catalog lives in PostgreSQL, never in the container: give it ODCL_DATABASE_URL. Starting `serve` runs
# `odcl migrate` first (ODCL_MIGRATE=false to leave that to you). Other commands run as `docker run … IMAGE find plan`.

# Build: odcl and every extra into one virtual environment, from the lock file. git is for Sounding, which the
# bigquery extra installs from GitHub.
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=docker.io/astral/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /usr/local/bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/odcl
WORKDIR /src
# The dependencies first, so a change to odcl's code does not rebuild them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --all-extras --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --all-extras --no-editable \
 && /opt/odcl/bin/odcl --help > /dev/null

# Run: the environment, tini, and nothing to build with.
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# tini passes `docker stop` on to odcl (as PID 1, Python would ignore it and be killed 10 s later).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin odcl
COPY --from=build /opt/odcl /opt/odcl
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH=/opt/odcl/bin:$PATH PYTHONUNBUFFERED=1
USER odcl
EXPOSE 8080
# /v1/me answers without signing in, from the database: so healthy means serving and able to reach PostgreSQL.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/me', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["tini", "--", "docker-entrypoint.sh"]
# Inside its container the server has to listen on every interface for -p to reach it.
CMD ["serve", "--host", "0.0.0.0"]
