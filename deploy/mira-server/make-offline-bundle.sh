#!/usr/bin/env bash
# Build an offline bundle of Mira Server for Ubuntu hosts without access to
# GitHub or PyPI. Run it on any machine with network access (Linux or macOS,
# Python 3.11+ with pip), from a mira checkout:
#
#   deploy/mira-server/make-offline-bundle.sh                   # x86_64, Python 3.11 and 3.12
#   deploy/mira-server/make-offline-bundle.sh --arch aarch64
#   deploy/mira-server/make-offline-bundle.sh --wheel dist/mira_engine-0.3.5-py3-none-any.whl
#
# The result, mira-server-offline-<version>-<arch>.tar.gz, holds:
#
#   install.sh, uninstall.sh   the installer, which installs from this bundle
#   wheels/                    mira-engine and every dependency, for the chosen
#                              Python versions and CPU (pip --no-index)
#   python/                    a Python 3.11 build for Ubuntu 22.04, whose own
#                              Python is too old (python-build-standalone)
#   debs/                      Caddy, which Ubuntu 22.04 does not package
#
# On the server:
#
#   tar -xzf mira-server-offline-<version>-<arch>.tar.gz
#   cd mira-server-offline-<version>-<arch> && sudo bash install.sh
#
# The installer still uses apt for a few Ubuntu packages (sudo, bubblewrap,
# python3-venv, and caddy on 24.04): the host needs its usual (or a local)
# Ubuntu mirror, or those packages installed beforehand.
#
# Works with the bash 3.2 that macOS ships.

set -euo pipefail

ARCH=x86_64
PYTHONS="3.11 3.12"
WHEEL=""
PRETEND_VERSION=0.3.5.dev0
WITH_PYTHON=1
WITH_CADDY=1
OUT_DIR=$PWD
PYTHON=${PYTHON:-python3}

usage() {
	cat <<EOF
Usage: $0 [options]

  --arch x86_64|aarch64      CPU of the server (default: $ARCH)
  --python "3.11 3.12"       Python versions to include wheels for (default: "$PYTHONS";
                             Ubuntu 22.04 uses 3.11 from python/, 24.04 its own 3.12)
  --wheel FILE               Use this mira_engine wheel instead of building one
  --pretend-version VERSION  Version of the wheel built from this checkout when it
                             has no release tag (default: $PRETEND_VERSION)
  --no-python                Leave out python/ (servers on Ubuntu 24.04 only)
  --no-caddy                 Leave out debs/ (24.04, or no Caddy)
  -o, --output DIR           Where to write the bundle (default: current directory)
  -h, --help                 This text

Environment: PYTHON (default python3) runs pip; it needs Python 3.11+.
EOF
}

while (($#)); do
	case $1 in
	--arch) ARCH=$2; shift ;;
	--python) PYTHONS=$2; shift ;;
	--wheel) WHEEL=$2; shift ;;
	--pretend-version) PRETEND_VERSION=$2; shift ;;
	--no-python) WITH_PYTHON=0 ;;
	--no-caddy) WITH_CADDY=0 ;;
	-o | --output) OUT_DIR=$2; shift ;;
	-h | --help) usage; exit 0 ;;
	*) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
	esac
	shift
done

case $ARCH in
x86_64) DEB_ARCH=amd64 ;;
aarch64) DEB_ARCH=arm64 ;;
*) printf -- '--arch must be x86_64 or aarch64\n' >&2; exit 2 ;;
esac

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
	|| die "$PYTHON must be Python 3.11 or newer (set PYTHON=...)"
"$PYTHON" -m pip --version >/dev/null 2>&1 || die "$PYTHON has no pip"
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/mira-bundle.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# ---- the mira-engine wheel
if [[ -z $WHEEL ]]; then
	[[ -f $REPO/pyproject.toml ]] || die "run this from a mira checkout, or pass --wheel"
	[[ -f $REPO/mira_engine/skills/README.md ]] \
		|| die "the skills are missing: git -C $REPO submodule update --init --depth 1 mira_engine/skills"
	say "building the mira-engine wheel from $REPO"
	# Release tags give the version; a checkout without one gets --pretend-version.
	if git -C "$REPO" describe --tags --exact-match --match 'v*' >/dev/null 2>&1; then
		"$PYTHON" -m pip wheel -q --disable-pip-version-check --no-deps -w "$WORK/dist" "$REPO"
	else
		SETUPTOOLS_SCM_PRETEND_VERSION=$PRETEND_VERSION "$PYTHON" -m pip wheel -q --disable-pip-version-check --no-deps -w "$WORK/dist" "$REPO"
	fi
	WHEEL=$(ls "$WORK"/dist/mira_engine-*.whl | head -n 1)
fi
[[ -f $WHEEL && $(basename "$WHEEL") == mira_engine-*.whl ]] || die "not a mira_engine wheel: $WHEEL"
NAME=$(basename "$WHEEL")
VERSION=${NAME#mira_engine-}
VERSION=${VERSION%%-*}
BUNDLE=mira-server-offline-$VERSION-$ARCH
DEST=$WORK/$BUNDLE
mkdir -p "$DEST/wheels"
cp "$WHEEL" "$DEST/wheels/"
say "mira-engine $VERSION"

# ---- dependencies, for each Python version
for py in $PYTHONS; do
	say "downloading the dependencies for Python $py on $ARCH"
	"$PYTHON" -m pip download -q --disable-pip-version-check --dest "$DEST/wheels" \
		--only-binary=:all: --implementation cp --python-version "$py" \
		--abi "cp${py/./}" --abi abi3 --abi none \
		--platform "manylinux_2_28_$ARCH" --platform "manylinux_2_17_$ARCH" \
		--platform "manylinux2014_$ARCH" --platform "linux_$ARCH" --platform any \
		"$DEST/wheels/$NAME[server]"
done

# ---- GitHub release assets (python-build-standalone, Caddy)
release_asset() { # repo, Python regex for the asset name -> download URL
	curl -fsSL "https://api.github.com/repos/$1/releases/latest" | "$PYTHON" -c '
import json, re, sys
pattern = re.compile(sys.argv[1])
names = [a for a in json.load(sys.stdin)["assets"] if pattern.fullmatch(a["name"])]
if not names:
    sys.exit("no release asset matches " + sys.argv[1])
print(sorted(names, key=lambda a: a["name"])[-1]["browser_download_url"])' "$2"
}

if ((WITH_PYTHON)); then
	say "downloading Python 3.11 for Ubuntu 22.04 (python-build-standalone)"
	url=$(release_asset astral-sh/python-build-standalone \
		"cpython-3\\.11\\.[0-9]+\\+[0-9]+-$ARCH-unknown-linux-gnu-install_only_stripped\\.tar\\.gz")
	mkdir -p "$DEST/python"
	name=$(basename "$url" | sed 's/%2[Bb]/+/g')  # the URL encodes the "+" in the name
	curl -fsSL -o "$DEST/python/$name" "$url"
fi

if ((WITH_CADDY)); then
	say "downloading Caddy for Ubuntu 22.04"
	url=$(release_asset caddyserver/caddy "caddy_[0-9.]+_linux_$DEB_ARCH\\.deb")
	mkdir -p "$DEST/debs"
	curl -fsSL -o "$DEST/debs/$(basename "$url")" "$url"
fi

# ---- installer, notes, checksums
cp "$HERE/install.sh" "$HERE/uninstall.sh" "$DEST/"
chmod 0755 "$DEST/install.sh" "$DEST/uninstall.sh"
cat >"$DEST/README.txt" <<EOF
Mira Server offline bundle: mira-engine $VERSION for Ubuntu 22.04/24.04 on $ARCH
(wheels for Python $PYTHONS).

Install (no access to GitHub or PyPI needed):

  sudo bash install.sh

The installer uses this bundle automatically. apt still has to work for a few
Ubuntu packages (sudo, bubblewrap, python3-venv, and caddy on 24.04): use the
host's usual or a local Ubuntu mirror, or install them beforehand.

Upgrade: unpack a newer bundle and run its install.sh again.
Uninstall: sudo mira-server-uninstall
EOF
(cd "$DEST" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while read -r file; do
	if command -v sha256sum >/dev/null; then sha256sum "$file"; else shasum -a 256 "$file"; fi
done >SHA256SUMS)

tar -C "$WORK" -czf "$OUT_DIR/$BUNDLE.tar.gz" "$BUNDLE"
say "wrote $OUT_DIR/$BUNDLE.tar.gz ($(du -h "$OUT_DIR/$BUNDLE.tar.gz" | cut -f1))"
