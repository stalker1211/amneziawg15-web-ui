#!/usr/bin/env bash
set -euo pipefail

# Publish master and tags to GitHub without the private docs.
#
# The NAS remote (`local`) keeps the full history. GitHub gets a copy in which
# PRIVATE_FILES were never committed: each affected commit is re-created with
# the files dropped from its tree and every other byte (author, committer,
# dates, message) unchanged. That makes the copy deterministic -- every run
# yields the same commit IDs -- so GitHub pushes stay plain fast-forwards.
# Commits that never contained the files keep their original IDs.
#
# Only refs are pushed; no local branch or tag is created or changed.
# Pushes go to the remote's fetch URL, so `remote.origin.pushurl` can be set to
# a dummy value to make a plain `git push origin` fail.
#
# Usage:
#   ./publish_github.sh             # push master + all tags
#   ./publish_github.sh --dry-run   # show what would be pushed
#   ./publish_github.sh --force     # replace history already on GitHub

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-master}"
PRIVATE_FILES=(CLAUDE.md DEVELOPMENT.md)

PUSH_FLAGS=()
for arg in "$@"; do
	case "${arg}" in
		--dry-run) PUSH_FLAGS+=(--dry-run) ;;
		--force) PUSH_FLAGS+=(--force) ;;
		-h|--help) sed -n '4,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "Unknown argument: ${arg}" >&2; exit 1 ;;
	esac
done

cd "$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir "${WORK}/map"

has_private() { [[ -n "$(git ls-tree -r --name-only "$1" -- "${PRIVATE_FILES[@]}")" ]]; }

# Rewrite oldest first, so every parent is already mapped. The map is one file
# per commit (old id -> new id) because macOS bash 3.2 has no associative arrays.
for c in $(git rev-list --reverse --topo-order "${BRANCH}"); do
	parents="" changed=0
	for p in $(git rev-parse "${c}^@"); do
		np="$(cat "${WORK}/map/${p}")"
		[[ "${np}" != "${p}" ]] && changed=1
		parents+="parent ${np}"$'\n'
	done
	tree="$(git rev-parse "${c}^{tree}")"
	if has_private "${c}"; then
		GIT_INDEX_FILE="${WORK}/index" git read-tree "${c}"
		GIT_INDEX_FILE="${WORK}/index" git update-index --force-remove -- "${PRIVATE_FILES[@]}"
		tree="$(GIT_INDEX_FILE="${WORK}/index" git write-tree)"
		changed=1
	fi
	if [[ ${changed} -eq 0 ]]; then
		echo "${c}" > "${WORK}/map/${c}"
		continue
	fi
	raw="$(git cat-file commit "${c}"; echo x)"; raw="${raw%x}"
	if grep -q '^gpgsig' <<<"${raw%%$'\n\n'*}"; then
		echo "Error: ${c} is signed; rewriting it would invalidate the signature" >&2
		exit 1
	fi
	# Replace only the tree and parent headers; the rest is copied byte for byte.
	new="$({ printf 'tree %s\n%s' "${tree}" "${parents}"; printf '%s' "${raw}" | sed '1,/^$/{/^tree /d;/^parent /d;}'; } | git hash-object -t commit -w --stdin)"
	echo "${new}" > "${WORK}/map/${c}"
done

TIP="$(cat "${WORK}/map/$(git rev-parse "${BRANCH}")")"

for c in $(git rev-list "${TIP}"); do
	if has_private "${c}"; then
		echo "Error: ${c} in the GitHub history still contains a private file" >&2
		exit 1
	fi
done

REFSPECS=("${TIP}:refs/heads/${BRANCH}")
while read -r name type target peeled; do
	commit="${peeled:-${target}}"
	[[ -f "${WORK}/map/${commit}" ]] || { REFSPECS+=("${target}:refs/tags/${name}"); continue; }
	mapped="$(cat "${WORK}/map/${commit}")"
	if [[ "${mapped}" == "${commit}" ]]; then
		REFSPECS+=("${target}:refs/tags/${name}")
	elif [[ "${type}" == commit ]]; then
		REFSPECS+=("${mapped}:refs/tags/${name}")
	else
		body="$(git cat-file tag "${target}"; echo x)"; body="${body%x}"
		if grep -q 'BEGIN PGP SIGNATURE' <<<"${body}"; then
			echo "Error: tag ${name} is signed; rewriting it would invalidate the signature" >&2
			exit 1
		fi
		newtag="$(printf '%s' "${body}" | sed "1s/^object .*/object ${mapped}/" | git hash-object -t tag -w --stdin)"
		REFSPECS+=("${newtag}:refs/tags/${name}")
	fi
done < <(git for-each-ref refs/tags --format='%(refname:short) %(objecttype) %(objectname) %(*objectname)')

URL="$(git remote get-url "${REMOTE}")"

echo "Publishing to ${URL} without: ${PRIVATE_FILES[*]}"
echo "  ${BRANCH}: $(git rev-parse --short "${BRANCH}") -> $(git rev-parse --short "${TIP}")"
echo "  files dropped at tip: $(git diff --name-only "${TIP}" "${BRANCH}" | tr '\n' ' ')"

git push ${PUSH_FLAGS[@]+"${PUSH_FLAGS[@]}"} "${URL}" "${REFSPECS[@]}"
