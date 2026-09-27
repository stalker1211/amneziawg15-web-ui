#!/usr/bin/env bash
set -euo pipefail

# Build the image for linux/amd64 + linux/arm64 and push it to Docker Hub.
#
# Every publish updates :latest. The version comes from git (see version.sh), never
# from what is typed:
#   - HEAD exactly on release tag vX.Y[.Z], clean tree -> also pushes :X.Y[.Z]
#   - rebuilding an older release (a newer tag exists) -> only :X.Y[.Z], so :latest
#     never goes backwards
# Each image carries a build label shown under the page heading, e.g.
# "v2.2 build 20260926.1" or "v2.2-3-gabc1234 build 20260926.2".
#
# Releasing 2.3:   git tag -a v2.3 -m 2.3 && ./publish_dockerhub.sh
#
# Usage:
#   ./publish_dockerhub.sh             publish HEAD as described above
#   ./publish_dockerhub.sh 2.3         same, but stop unless HEAD is release v2.3
#   ./publish_dockerhub.sh --dry-run   print version, label and tags; build nothing
#
# Env: IMAGE_REPO, DOCKERFILE, PLATFORMS; TAG=2.3 works like the argument.

cd "$(dirname "$0")"
# shellcheck source=version.sh
source ./version.sh

IMAGE_REPO="${IMAGE_REPO:-stalker1211/amneziawg15-web-ui}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

DRY_RUN=0
EXPECTED="${TAG:-}"
for arg in "$@"; do
	case "${arg}" in
	--dry-run) DRY_RUN=1 ;;
	-h | --help)
		sed -n '4,21p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	-*)
		echo "Unknown option: ${arg}" >&2
		exit 1
		;;
	*) EXPECTED="${arg}" ;;
	esac
done

VERSION="$(version_describe)"
RELEASE="$(version_release)"

if [[ -n "${EXPECTED}" && "v${EXPECTED#v}" != "${RELEASE}" ]]; then
	echo "Error: asked for v${EXPECTED#v}, but HEAD is ${VERSION}." >&2
	echo "Commit everything and tag the release first: git tag -a v${EXPECTED#v} -m ${EXPECTED#v}" >&2
	exit 1
fi

if [[ -z "${RELEASE}" ]]; then
	TAGS=(latest)
elif [[ "${RELEASE}" == "$(version_newest)" ]]; then
	TAGS=("${RELEASE#v}" latest)
else
	TAGS=("${RELEASE#v}") # older release: leave :latest where it is
fi

# Build label "<version> build <YYYYMMDD>.<n>": n counts publishes from this machine
# today (a failed build still uses a number, so gaps are normal). The counter lives in
# .cache/, outside the build context; the label reaches the image as a build arg.
COUNTER_FILE=".cache/build_counter"
TODAY="$(date +%Y%m%d)"
LAST="$(cat "${COUNTER_FILE}" 2>/dev/null || true)"
if [[ "${LAST%% *}" == "${TODAY}" ]]; then
	N=$((${LAST##* } + 1))
else
	N=1
fi
LABEL="${VERSION} build ${TODAY}.${N}"

IMAGES=()
for t in "${TAGS[@]}"; do
	IMAGES+=("${IMAGE_REPO}:${t}")
done

echo "Version:  ${VERSION}${RELEASE:+ (release ${RELEASE})}"
echo "Label:    ${LABEL}"
echo "Push:     ${IMAGES[*]}"
if [[ -z "${RELEASE}" ]]; then
	echo "          (not a clean release commit, so no version tag; tag vX.Y to publish one)"
fi
if [[ ${DRY_RUN} -eq 1 ]]; then
	echo "Dry run: nothing built or pushed."
	exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
	echo "Error: docker is not installed or not on PATH" >&2
	exit 1
fi
if ! docker info >/dev/null 2>&1; then
	echo "Error: docker daemon not reachable. Is Docker running?" >&2
	exit 1
fi

mkdir -p .cache
echo "${TODAY} ${N}" >"${COUNTER_FILE}"

BUILD_TAGS=()
for image in "${IMAGES[@]}"; do
	BUILD_TAGS+=(-t "${image}")
done

docker buildx build \
	--platform "${PLATFORMS}" \
	-f "${DOCKERFILE}" \
	"${BUILD_TAGS[@]}" \
	--build-arg "BUILD_LABEL=${LABEL}" \
	--push \
	.

echo "Done."
