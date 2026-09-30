#!/bin/bash
# Deploy openaec-reports (bm-reports-api) op het platform.
#
# - docker compose via sudo: /opt/openaec/.env is root 600. Dit script leest
#   of toont die file nooit; compose gebruikt hem alleen intern.
# - GIT_COMMIT als build-arg, zodat /api/health de gedeployde commit toont
#   (Dockerfile: ENV OPENAEC_BUILD=${GIT_COMMIT}).
# - Voor de build wordt het draaiende image getagd als rollback-<datum>.
#   Terugzetten: sudo docker tag openaec-bm-reports-api:rollback-<datum> \
#   openaec-bm-reports-api:latest && sudo docker compose up -d bm-reports-api
set -euo pipefail

REPO_DIR=/opt/openaec/bm-reports-api
COMPOSE_DIR=/opt/openaec
SERVICE=bm-reports-api
HEALTH_URL=https://report.open-aec.com/api/health

echo "=== Deploy openaec-reports ==="

echo "1/5 Pulling latest code..."
cd "$REPO_DIR"
git pull --ff-only origin main
GIT_COMMIT=$(git rev-parse --short HEAD)
echo "    commit: $GIT_COMMIT"

echo "2/5 Rollback-tag van het draaiende image..."
STAMP=$(date +%Y%m%d-%H%M)
if IMG=$(sudo docker inspect --format '{{.Image}}' "$SERVICE" 2>/dev/null); then
    REPO=$(sudo docker inspect --format '{{.Config.Image}}' "$SERVICE" | cut -d: -f1)
    sudo docker tag "$IMG" "$REPO:rollback-$STAMP"
    echo "    $REPO:rollback-$STAMP -> $IMG"
else
    echo "    geen draaiende container gevonden, geen rollback-tag"
fi

echo "3/5 Building container..."
cd "$COMPOSE_DIR"
sudo docker compose build --no-cache --build-arg "GIT_COMMIT=$GIT_COMMIT" "$SERVICE"

echo "4/5 Deploying..."
sudo docker compose up -d "$SERVICE"

echo "5/5 Health check..."
for _ in $(seq 1 12); do
    sleep 5
    STATUS=$(sudo docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || true)
    [ "$STATUS" = "healthy" ] && break
done
HEALTH=$(curl -sf "$HEALTH_URL" || true)
if [ "$STATUS" = "healthy" ] && echo "$HEALTH" | grep -q "\"build\":\"$GIT_COMMIT\""; then
    echo "OK: API is live: $HEALTH"
else
    echo "FOUT: health check faalde (container: ${STATUS:-onbekend}, health: ${HEALTH:-geen antwoord})"
    sudo docker compose logs --tail=20 "$SERVICE"
    exit 1
fi

echo "=== Deploy complete ==="
sudo docker compose ps "$SERVICE"
