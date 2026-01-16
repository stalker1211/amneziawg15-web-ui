
#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="amnezia-web-ui"
IMAGE_NAME="amneziawg-web-ui:local"
INTERACTIVE="${INTERACTIVE:-0}"
ENTRYPOINT="${ENTRYPOINT:-}"
CMD_ARGS=("$@")

# Optional: set API_TOKEN on the host to enable app-layer token auth for /api/*
# Example: API_TOKEN=$(openssl rand -hex 32) ./run.sh

# Build image by default (set BUILD=0 to skip).
BUILD="${BUILD:-1}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"

if [ "${BUILD}" = "1" ]; then
	echo "Building image ${IMAGE_NAME} (dockerfile: ${DOCKERFILE})..."
	docker build -f "${DOCKERFILE}" -t "${IMAGE_NAME}" .
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

docker run "${RUN_FLAGS[@]}" \
	--name "${CONTAINER_NAME}" \
	"${ENTRYPOINT_FLAGS[@]}" \
	--cap-add=NET_ADMIN \
	--cap-add=SYS_MODULE \
	--device /dev/net/tun \
	--sysctl net.ipv4.ip_forward=1 \
	--sysctl net.ipv4.conf.all.src_valid_mark=1 \
	-p 8090:8090/tcp \
	-p 51820:51820/udp \
	-e ENABLE_NAT=1 \
	-e NGINX_PORT=8090 \
	-e NGINX_USER=admin \
	-e NGINX_PASSWORD=changeme \
	-e API_TOKEN="${API_TOKEN:-}" \
	-v amnezia-data:/etc/amnezia \
	"${IMAGE_NAME}" \
	"${CMD_ARGS[@]}"

