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
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

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

# Build label shown under the page heading: "v<tag> build <YYYYMMDD>.<n>", where
# n counts builds for the day. Written into the build context so the Dockerfile's
# existing `COPY web-ui` bakes it in; app.py falls back to "dev" when absent, so a
# plain `docker build` or a source bind-mount is never mislabelled as a release.
#
# The file has to exist before the build, so a failed build still consumes a
# number. Gaps in the sequence are expected and harmless.
BUILD_FILE="${CONTEXT_DIR}/web-ui/BUILD"
BUILD_DATE="$(date +%Y%m%d)"
PREV="$(cat "${BUILD_FILE}" 2>/dev/null || true)"
if [[ "${PREV}" == *" ${BUILD_DATE}."* ]]; then
	BUILD_N=$(( ${PREV##*.} + 1 ))
else
	BUILD_N=1
fi
BUILD_LABEL="v${TAG:-dev} build ${BUILD_DATE}.${BUILD_N}"
echo "${BUILD_LABEL}" > "${BUILD_FILE}"
echo "  Build:      ${BUILD_LABEL}"

echo "Building image..."
docker buildx build \
    --platform "${PLATFORMS}" \
    -f "${DOCKERFILE}" \
    "${BUILD_TAGS[@]}" \
    --push \
    "${CONTEXT_DIR}"

echo "Done."
