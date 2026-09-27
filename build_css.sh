#!/usr/bin/env bash
#
# Build web-ui/static/css/tailwind.css with Tailwind's standalone CLI (no Node).
#
#   ./build_css.sh            build once, minified (the Dockerfile runs this)
#   ./build_css.sh --watch    rebuild on every change, for a bind-mounted dev container
#
# The CLI is downloaded once per platform into .cache/ and checked against the
# SHA-256 published with the release. Bumping it: change VERSION and the four hashes
# (from the release's sha256sums.txt), then compare the UI in both themes.
set -euo pipefail

cd "$(dirname "$0")"

VERSION=3.4.19
case "$(uname -s)-$(uname -m)" in
Linux-x86_64) ASSET=tailwindcss-linux-x64 SHA256=4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e ;;
Linux-aarch64 | Linux-arm64) ASSET=tailwindcss-linux-arm64 SHA256=e5b2d27694daa80cc52ec29553ba2c6bd43d86bd51a9d633ed24058b9c05a676 ;;
Darwin-arm64) ASSET=tailwindcss-macos-arm64 SHA256=7fdeb00818b6214a337383063282b2361ecb08bbc08f8c8a7ba97ee1e2eaa4fe ;;
Darwin-x86_64) ASSET=tailwindcss-macos-x64 SHA256=a597f407e0f1f03535731f5b42f1576a8152cb5fffc2f38e754722bc0c280045 ;;
*)
	echo "build_css.sh: no Tailwind CLI for $(uname -s)-$(uname -m)" >&2
	exit 1
	;;
esac

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	else
		shasum -a 256 "$1" | cut -d' ' -f1
	fi
}

CLI=".cache/${ASSET}-${VERSION}"
if [[ ! -x "${CLI}" ]]; then
	mkdir -p .cache
	curl -fsSL -o "${CLI}.download" \
		"https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/${ASSET}"
	if [[ "$(sha256_of "${CLI}.download")" != "${SHA256}" ]]; then
		rm -f "${CLI}.download"
		echo "build_css.sh: checksum mismatch for ${ASSET} ${VERSION}" >&2
		exit 1
	fi
	chmod +x "${CLI}.download"
	mv "${CLI}.download" "${CLI}"
fi

exec "${CLI}" -c tailwind.config.js -i tailwind.input.css -o web-ui/static/css/tailwind.css --minify "$@"
