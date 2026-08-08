#!/usr/bin/env bash
# Build and push the Arc image to a registry, for both architectures.
#
#   scripts/publish-image.sh [tag]
#
# Why this exists as a script rather than a one-liner in a runbook: the amd64
# build has never actually run, and a multi-arch push either lands both manifests
# or is silently useful for only half the customers. This checks.
#
# Log in first, yourself, so the token never lands in a transcript:
#   echo $GITHUB_PAT | docker login ghcr.io -u <user> --password-stdin
# The PAT needs scope write:packages.

set -euo pipefail

IMAGE="${ARC_IMAGE_REPO:-ghcr.io/joshuamschultz/arc}"
TAG="${1:-latest}"
REF="$IMAGE:$TAG"
PLATFORMS="linux/amd64,linux/arm64"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

command -v docker >/dev/null || fail "docker is not installed"
docker buildx version >/dev/null 2>&1 || fail "docker buildx is not available"

# Fail before a 20-minute build rather than after it.
log "Checking registry credentials for ${IMAGE%%/*}..."
if ! docker manifest inspect "$REF" >/dev/null 2>&1; then
  # A missing tag and a missing login look the same here, so only warn.
  echo "  ! $REF is not readable yet (expected on a first push)"
fi

# The default docker driver cannot build more than one architecture. A dedicated
# builder is what makes the amd64 half possible on an Apple Silicon machine.
if ! docker buildx inspect arc-multiarch >/dev/null 2>&1; then
  log "Creating the arc-multiarch builder..."
  docker buildx create --name arc-multiarch --driver docker-container --bootstrap
fi

log "Building $REF for $PLATFORMS (this is slow; amd64 is emulated)..."
docker buildx build \
  --builder arc-multiarch \
  --platform "$PLATFORMS" \
  --tag "$REF" \
  --push \
  "$REPO_ROOT"

log "Verifying both architectures are in the pushed manifest..."
MANIFEST="$(docker manifest inspect "$REF")"
for arch in amd64 arm64; do
  echo "$MANIFEST" | grep -q "\"architecture\": \"$arch\"" \
    || fail "$arch is missing from $REF — customers on that arch would get nothing"
  ok "$arch present"
done

echo
echo "Pushed $REF"
echo
echo "One thing left, and it is not optional:"
echo "  Make the package PUBLIC at https://github.com/users/joshuamschultz/packages"
echo "  Otherwise every customer server needs a registry pull secret baked into it."
echo
echo "Confirm an anonymous pull works before selling anything:"
echo "  docker logout ghcr.io && docker pull $REF"
