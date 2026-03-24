#!/bin/bash
# Upgrade (or initially deploy) the Jishi Docker container.
# Edit the variables below to match your environment, then:
#   chmod +x upgrade-sonos.sh
#   ./upgrade-sonos.sh
set -euo pipefail

IMAGE="chrisns/docker-node-sonos-http-api"
CONTAINER="sonos"
DATA_DIR="/docker/node-sonos-http-api"

docker pull "$IMAGE"
docker container stop "$CONTAINER" 2>/dev/null || true
docker container rm   "$CONTAINER" 2>/dev/null || true
docker run \
  --net=host \
  --name "$CONTAINER" \
  --restart=always \
  -d \
  -v "$DATA_DIR/settings.json:/app/settings.json" \
  -v "$DATA_DIR/clips:/app/static/clips" \
  -v "$DATA_DIR/cache:/app/cache" \
  -v "$DATA_DIR/presets:/app/presets" \
  "$IMAGE"

echo "Sonos container updated. Logs: docker logs -f $CONTAINER"
