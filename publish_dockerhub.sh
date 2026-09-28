#!/usr/bin/env bash
set -euo pipefail

# Build the image for linux/amd64 + linux/arm64 and push it to Docker Hub.
#
# Every publish updates :latest. The version comes from git (see version.sh), never
# from what is typed:
#   - HEAD exactly on release tag vX.Y[.Z], clean tree -> also pushes :X.Y[.Z]
#   - rebuilding an older release (a newer tag exists) -> only :X.Y[.Z], so :latest
#     never goes backwards
# --publish runs only on master or on a clean release commit, so work on a feature
# branch never reaches :latest.
# Each image carries a build label shown under the page heading, e.g.
# "v2.2 build 20260926.1" or "v2.2-3-gabc1234 build 20260926.2".
#
# Releasing 2.3:   git tag -a v2.3 -m 2.3 && ./publish_dockerhub.sh --publish
#
# Usage:
#   ./publish_dockerhub.sh             dry run: print version, label and tags; build nothing
#   ./publish_dockerhub.sh --publish   build and push HEAD as described above
#   ./publish_dockerhub.sh --publish 2.3   same, but stop unless HEAD is release v2.3
#
# Env: IMAGE_REPO, DOCKERFILE, PLATFORMS; TAG=2.3 works like the argument.

cd "$(dirname "$0")"
# shellcheck source=version.sh
source ./version.sh

IMAGE_REPO="${IMAGE_REPO:-stalker1211/amneziawg15-web-ui}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

PUBLISH=0
EXPECTED="${TAG:-}"
for arg in "$@"; do
	case "${arg}" in
	--publish) PUBLISH=1 ;;
	-h | --help)
		sed -n '4,23p' "$0" | sed 's/^# \{0,1\}//'
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
	WANTED="v${EXPECTED#v}"
	WANTED_AT="$(git rev-parse -q --verify "refs/tags/${WANTED}^{commit}" 2>/dev/null || true)"
	echo "Error: asked for ${WANTED}, but HEAD is ${VERSION}." >&2
	if [[ -n "${WANTED_AT}" && "${WANTED_AT}" == "$(git rev-parse HEAD)" ]]; then
		echo "HEAD is ${WANTED}, but the tree has uncommitted or untracked changes; commit or remove them." >&2
	elif [[ -n "${WANTED_AT}" ]]; then
		echo "${WANTED} already exists on another commit ($(git log -1 --format='%h, %cs' "${WANTED_AT}"))." >&2
		echo "To publish HEAD, run without a version (pushes :latest), or tag a new release:" >&2
		echo "  git tag -a vX.Y -m X.Y" >&2
	else
		echo "Commit any changes and tag the release first: git tag -a ${WANTED} -m ${WANTED#v}" >&2
	fi
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

BRANCH="$(git symbolic-ref -q --short HEAD || true)"
OFF_MASTER=""
if [[ -z "${RELEASE}" && "${BRANCH}" != master ]]; then
	OFF_MASTER="HEAD is ${BRANCH:-detached}, not master; only master or a clean release commit is published"
fi

if [[ ${PUBLISH} -eq 0 ]]; then
	[[ -z "${OFF_MASTER}" ]] || echo "Note: --publish would refuse: ${OFF_MASTER}."
	echo "Dry run: nothing built or pushed. Add --publish to build and push."
	exit 0
fi
if [[ -n "${OFF_MASTER}" ]]; then
	echo "Error: ${OFF_MASTER}." >&2
	exit 1
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
