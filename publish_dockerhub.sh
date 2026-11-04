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
# The build is scanned with grype (brew install grype) before anything is pushed: all
# findings are printed, and a HIGH or CRITICAL one that has a fix stops the publish.
# Unfixed ones are shown as "(suppressed)"; no rebuild can clear them.
#
# --scan-remote [X.Y[.Z]] scans what is published instead of a build: :latest, or the
# tag given, is read from the registry (no docker, no build, nothing here changes).
# Its exit codes are grype's, for a cron watch: 0 clean, 2 a fixable HIGH/CRITICAL
# sits on the published image (Alpine moved on since the push; a republish from this
# tree clears it), 1 a grype error — inconclusive, never read as clean.
#
# Releasing 2.3:   git tag -a v2.3 -m 2.3 && ./publish_dockerhub.sh --publish
#
# Usage:
#   ./publish_dockerhub.sh             dry run: print version, label and tags; build nothing
#   ./publish_dockerhub.sh --scan      build locally and scan; push nothing
#   ./publish_dockerhub.sh --scan-remote        scan the published :latest on Docker Hub; build/push nothing
#   ./publish_dockerhub.sh --scan-remote 2.8.2  same for one published version tag
#   ./publish_dockerhub.sh --publish   build, scan and, if the scan passes, push HEAD as above
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
SCAN=0
SCAN_REMOTE=0
EXPECTED="${TAG:-}"
for arg in "$@"; do
	case "${arg}" in
	--publish) PUBLISH=1 ;;
	--scan) SCAN=1 ;;
	--scan-remote) SCAN_REMOTE=1 ;;
	-h | --help)
		sed -n '4,/^$/p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	-*)
		echo "Unknown option: ${arg}" >&2
		exit 1
		;;
	*) EXPECTED="${arg}" ;;
	esac
done

if [[ ${SCAN_REMOTE} -eq 1 ]]; then
	# The watch on the published image: grype reads it from the registry, so this
	# works on any branch, needs no docker daemon and changes nothing.
	if [[ ${PUBLISH} -eq 1 || ${SCAN} -eq 1 ]]; then
		echo "Error: --scan-remote scans what is published, not a build; it is not combined with --scan or --publish." >&2
		exit 1
	fi
	if ! command -v grype >/dev/null 2>&1; then
		echo "Error: grype not found. Install it with: brew install grype" >&2
		exit 1
	fi
	REMOTE_TAG="${EXPECTED:-latest}" # a positional argument or TAG= names the published tag
	TARGET="${IMAGE_REPO}:${REMOTE_TAG#v}"
	echo "Scanning the published ${TARGET} (registry read: no build, no push)."
	# grype exits 2 on a fixable HIGH/CRITICAL, 1 on its own errors; every platform is
	# scanned before deciding, so one failure does not hide another.
	FIXABLE=0
	ERRORED=0
	for platform in ${PLATFORMS//,/ }; do
		echo
		echo "== Scan ${platform}"
		code=0
		grype "registry:${TARGET}" --platform "${platform}" -q \
			--ignore-states not-fixed,wont-fix,unknown --show-suppressed \
			--fail-on high || code=$?
		if [[ ${code} -eq 2 ]]; then
			FIXABLE=1
		elif [[ ${code} -ne 0 ]]; then
			ERRORED=1
		fi
	done
	if [[ ${ERRORED} -eq 1 ]]; then
		echo "Scan INCONCLUSIVE: grype errored while reading ${TARGET}." >&2
		echo "The published image is unscanned: do not read this as clean." >&2
		exit 1
	fi
	if [[ ${FIXABLE} -eq 1 ]]; then
		echo "Scan FAILED: fixable HIGH/CRITICAL vulnerabilities sit on ${TARGET}." >&2
		echo "Alpine moved on since this image was pushed; a republish from this tree clears it." >&2
		exit 2
	fi
	echo "Scan clean: no fixable HIGH/CRITICAL vulnerabilities on ${TARGET}."
	exit 0
fi

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

if [[ ${PUBLISH} -eq 0 && ${SCAN} -eq 0 ]]; then
	[[ -z "${OFF_MASTER}" ]] || echo "Note: --publish would refuse: ${OFF_MASTER}."
	echo "Dry run: nothing built or pushed. Add --scan to build and scan, --publish to also push."
	exit 0
fi
if [[ ${PUBLISH} -eq 1 && -n "${OFF_MASTER}" ]]; then
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
# Local Docker only: a context or DOCKER_HOST pointing elsewhere (the NAS) would build and scan
# there. `docker context inspect` reports whichever of them wins.
DOCKER_ENDPOINT="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
case "${DOCKER_ENDPOINT}" in
unix://*) echo "Docker: ${DOCKER_ENDPOINT}" ;;
*)
	echo "Error: Docker is ${DOCKER_ENDPOINT}, not a local socket; switch the context (docker context use) or DOCKER_HOST" >&2
	exit 1
	;;
esac
if ! command -v grype >/dev/null 2>&1; then
	echo "Error: grype is not installed (brew install grype)" >&2
	exit 1
fi

# Only a publish uses up a build number; a scan's candidate carries the same label.
if [[ ${PUBLISH} -eq 1 ]]; then
	mkdir -p .cache
	echo "${TODAY} ${N}" >"${COUNTER_FILE}"
fi

# Built into the local image store (multi-platform --load needs the containerd store,
# as in OrbStack and Docker Desktop), scanned, and only then tagged and pushed, so
# what is pushed is exactly what was scanned. --pull and a fresh runtime stage make
# its apk upgrade/add fetch today's Alpine fixes; the pinned builder stages stay cached.
CANDIDATE="amneziawg-web-ui:candidate"
# No git information in the image's provenance attestation. buildx adds one to every
# image it loads and the push carries it to Docker Hub, by default with the build
# context's git remote and commit: `origin` here is the NAS clone
# (dsergeev@nas:/mnt/storage1/git/awg.git), and every tag up to 2.6 carries it.
export BUILDX_GIT_INFO=0
docker buildx build \
	--platform "${PLATFORMS}" \
	-f "${DOCKERFILE}" \
	-t "${CANDIDATE}" \
	--build-arg "BUILD_LABEL=${LABEL}" \
	--pull \
	--no-cache-filter runtime \
	--load \
	.

# grype exits 2 on a fixable HIGH/CRITICAL, 1 on its own errors; either stops here.
# Every platform is scanned before deciding, so one failure does not hide another.
FAILED=()
for platform in ${PLATFORMS//,/ }; do
	echo
	echo "== Scan ${platform}"
	grype "docker:${CANDIDATE}" --platform "${platform}" -q \
		--ignore-states not-fixed,wont-fix,unknown --show-suppressed \
		--fail-on high || FAILED+=("${platform}")
done
echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
	echo "Scan FAILED on ${FAILED[*]}: a fixable HIGH/CRITICAL finding (or a grype error) above; nothing pushed." >&2
	exit 1
fi
echo "Scan passed: no fixable HIGH/CRITICAL findings."

if [[ ${PUBLISH} -eq 0 ]]; then
	echo "Scan only: nothing pushed. The scanned image is ${CANDIDATE}."
	exit 0
fi

for image in "${IMAGES[@]}"; do
	docker tag "${CANDIDATE}" "${image}"
	docker push "${image}"
done

echo "Done."
