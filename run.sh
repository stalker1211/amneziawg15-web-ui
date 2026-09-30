#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=version.sh
source ./version.sh

CONTAINER_NAME="amnezia-web-ui"
IMAGE_NAME="amneziawg-web-ui:local"
INTERACTIVE="${INTERACTIVE:-0}"
ENTRYPOINT="${ENTRYPOINT:-}"
CMD_ARGS=("$@")

# Build image by default (set BUILD=0 to skip).
BUILD="${BUILD:-1}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"

if [ "${BUILD}" = "1" ]; then
	# Same version as publish_dockerhub.sh would use, marked local so a dev image is
	# never mistaken for a published one.
	# The branch too, unless it is master or a detached HEAD: "v2.3-12-g13431fc dev-2.4 (local)".
	BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
	case "${BRANCH}" in
	"" | HEAD | master) LABEL="$(version_describe) (local)" ;;
	*) LABEL="$(version_describe) ${BRANCH} (local)" ;;
	esac
	echo "Building image ${IMAGE_NAME}: ${LABEL} (dockerfile: ${DOCKERFILE})..."
	docker build -f "${DOCKERFILE}" --build-arg "BUILD_LABEL=${LABEL}" -t "${IMAGE_NAME}" .
fi

RUN_FLAGS=(-d)
if [ "${INTERACTIVE}" = "1" ]; then
	RUN_FLAGS=(-it)
fi

ENTRYPOINT_FLAGS=()
if [ -n "${ENTRYPOINT}" ]; then
	ENTRYPOINT_FLAGS=(--entrypoint "${ENTRYPOINT}")
fi

# Make the script re-runnable: replace existing container if present.
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
	echo "Removing existing container ${CONTAINER_NAME}..."
	docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

HOST_PORT="${HOST_PORT:-8090}"

docker run "${RUN_FLAGS[@]}" \
	--name "${CONTAINER_NAME}" \
	"${ENTRYPOINT_FLAGS[@]+"${ENTRYPOINT_FLAGS[@]}"}" \
	--cap-add=NET_ADMIN \
	--device /dev/net/tun \
	--sysctl net.ipv4.ip_forward=1 \
	--sysctl net.ipv4.conf.all.src_valid_mark=1 \
	-p "${HOST_PORT}:8090/tcp" \
	-p 51820-51830:51820-51830/udp \
	-e NGINX_PORT=8090 \
	-e NGINX_PASSWORD="changeme" \
	-e AWG_LOG_LEVEL="${AWG_LOG_LEVEL:-}" \
	-e AWG_LOG_FILE="${AWG_LOG_FILE:-}" \
	-v amnezia-data:/etc/amnezia \
	"${IMAGE_NAME}" \
	"${CMD_ARGS[@]+"${CMD_ARGS[@]}"}"

