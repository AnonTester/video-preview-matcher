#!/usr/bin/env bash
# Rebuild the preview-matcher image and (re)start the container.
#
# Uses docker-preview-matcher.yml — a local, gitignored compose override
# with your actual library/staging/app-state paths (see README's "Docker
# deployment" section) — if present, falling back to the generic
# docker-compose.yml sample otherwise.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

COMPOSE_FILE="docker-compose.yml"
if [ -f "docker-preview-matcher.yml" ]; then
    COMPOSE_FILE="docker-preview-matcher.yml"
fi

docker compose -f "$COMPOSE_FILE" build
docker compose -f "$COMPOSE_FILE" up -d
docker compose -f "$COMPOSE_FILE" ps
