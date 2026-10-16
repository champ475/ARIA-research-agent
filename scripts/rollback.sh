#!/usr/bin/env bash
# Roll ARIA back to an earlier release (PRD TEST-07): point `:stable` at a
# previous version's image. No rebuild — the image that passed that version's
# release checks is the one that comes back.
#
#   scripts/rollback.sh v2.0.0                 # GHCR (needs `docker login ghcr.io`)
#   IMAGE=aria scripts/rollback.sh v2.0.0      # local images only (the drill)
#
# The database is NOT rolled back. Migrations are expand/contract
# (docs/RELEASING.md), so the previous image runs on the newer schema. Never
# run `alembic downgrade` against production.
set -euo pipefail

target="${1:-}"
if [[ ! "$target" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: $0 vX.Y.Z   (the release to go back to)" >&2
  exit 2
fi

image="${IMAGE:-ghcr.io/champ475/aria-research-agent}"

if [[ "$image" == */* ]]; then
  # Registry: retag remotely, by digest, without pulling.
  docker buildx imagetools inspect "$image:$target" >/dev/null \
    || { echo "no such release: $image:$target" >&2; exit 1; }
  previous="$(docker buildx imagetools inspect "$image:stable" --format '{{json .Manifest.Digest}}' 2>/dev/null || echo none)"
  docker buildx imagetools create -t "$image:stable" "$image:$target"
else
  # Local drill: plain `docker tag`.
  docker image inspect "$image:$target" >/dev/null 2>&1 \
    || { echo "no such local image: $image:$target" >&2; exit 1; }
  previous="$(docker image inspect "$image:stable" --format '{{.Id}}' 2>/dev/null || echo none)"
  docker tag "$image:$target" "$image:stable"
fi

echo "rolled back: $image:stable -> $target (was $previous)"
echo "next: restart the app and worker on :stable; record it in docs/drills/."
