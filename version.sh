#!/usr/bin/env bash
#
# The version of this checkout, taken from git release tags (vMAJOR.MINOR[.PATCH]).
# Sourced by run.sh and publish_dockerhub.sh; run it directly to see what they use.
#
#   version_describe   v2.2 on a release commit; v2.2-3-gabc1234 after one; a
#                      "-dirty" suffix when the tree has changes; "dev" without git
#   version_release    the release tag (v2.2) when HEAD is exactly on one and the tree
#                      is clean -- only then may an image carry that version; else empty
#   version_newest     the highest release tag in the repository
#
# "Dirty" counts untracked files too: they are in the Docker build context. Only
# v-prefixed tags are releases (the old "1.5.1" tag is ignored). Works with bash 3.2.

version_dirty() {
	[[ -n "$(git status --porcelain 2>/dev/null)" ]]
}

version_describe() {
	local described
	described="$(git describe --tags --match 'v[0-9]*' --always 2>/dev/null)" || {
		echo dev
		return 0
	}
	if version_dirty; then
		described="${described}-dirty"
	fi
	echo "${described}"
}

version_release() {
	local tag
	tag="$(git describe --tags --match 'v[0-9]*' --exact-match 2>/dev/null)" || return 0
	if ! version_dirty; then
		echo "${tag}"
	fi
}

version_newest() {
	git tag --list 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -n 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
	cd "$(dirname "$0")" || exit 1
	echo "version: $(version_describe)"
	echo "release: $(version_release)"
	echo "newest:  $(version_newest)"
fi
