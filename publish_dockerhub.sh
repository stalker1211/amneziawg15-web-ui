#!/usr/bin/env bash
set -euo pipefail

# Build + publish this repo's Docker image to Docker Hub.
#
# Simplified workflow:
#   - Always builds and pushes :latest
#   - If a version tag is provided, also builds and pushes that tag
#
# Examples:
#   ./publish_dockerhub.sh            # pushes :latest
#   ./publish_dockerhub.sh 1.4.1      # pushes :1.4.1 and :latest
#   TAG=1.4.1 ./publish_dockerhub.sh  # same as above

IMAGE_REPO="${IMAGE_REPO:-stalker1211/amneziawg15-web-ui}"
ARG_TAG="${1:-}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
CONTEXT_DIR="${CONTEXT_DIR:-.}"

usage() {
	cat <<EOF
Usage:
  $0 [tag]

Publishes:
  - Always publishes: ${IMAGE_REPO}:latest
  - If tag is provided: also publishes ${IMAGE_REPO}:<tag>

Env vars:
  IMAGE_REPO     Docker Hub repo (default: ${IMAGE_REPO})
  TAG            Optional version tag (overrides positional arg)
  DOCKERFILE     Dockerfile path (default: ${DOCKERFILE})
  CONTEXT_DIR    Build context dir (default: ${CONTEXT_DIR})

Examples:
  $0
	$0 1.4.1
	TAG=1.4.1 $0
EOF
}

if [[ "${ARG_TAG}" == "-h" || "${ARG_TAG}" == "--help" ]]; then
	usage
	exit 0
fi

TAG="${TAG:-${ARG_TAG:-}}"

if ! command -v docker >/dev/null 2>&1; then
	echo "Error: docker is not installed or not on PATH" >&2
	exit 1
fi

if ! docker info >/dev/null 2>&1; then
	echo "Error: docker daemon not reachable. Is Docker running?" >&2
	exit 1
fi

IMAGE_LATEST="${IMAGE_REPO}:latest"
IMAGE_TAGGED=""

echo "Publishing Docker image"
echo "  Repo:       ${IMAGE_REPO}"
echo "  Dockerfile: ${DOCKERFILE}"
echo "  Context:    ${CONTEXT_DIR}"

BUILD_TAGS=("-t" "${IMAGE_LATEST}")
if [[ -n "${TAG}" && "${TAG}" != "latest" ]]; then
	IMAGE_TAGGED="${IMAGE_REPO}:${TAG}"
	BUILD_TAGS+=("-t" "${IMAGE_TAGGED}")
fi

echo "Building image..."
docker build -f "${DOCKERFILE}" "${BUILD_TAGS[@]}" "${CONTEXT_DIR}"

if [[ -n "${IMAGE_TAGGED}" ]]; then
	echo "Pushing ${IMAGE_TAGGED}..."
	docker push "${IMAGE_TAGGED}"
fi

echo "Pushing ${IMAGE_LATEST}..."
docker push "${IMAGE_LATEST}"

echo "Done."
