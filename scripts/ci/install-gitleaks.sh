#!/usr/bin/env bash
# NX-060: install the pinned gitleaks from an official release archive the operator has already fetched. Never downloads.
#
#   scripts/ci/install-gitleaks.sh <archive> <checksums file> [<destination directory, default ~/.local/bin>]
#
# The archive must be the one named in versions.lock.json (release_tools.gitleaks.archives.<platform>) and the checksums
# file the release's published one (release_tools.gitleaks.checksums_file), both from release_tools.gitleaks.source.
# Checks the archive against the published checksum and, once recorded, against archive_sha256_<platform>; extracts only the
# gitleaks binary, installs it for this account only (no root, mode 0755), confirms its version and prints the archive and
# binary digests to record. Any mismatch stops before anything is installed.
set -euo pipefail
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
archive="${1:?archive path}" checksums="${2:?checksums file path}" dest="${3:-$HOME/.local/bin}"
digest() { { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; } | cut -d' ' -f1; }
pin=$(python3 -c 'import json,sys
tool = json.load(open(sys.argv[1]))["release_tools"]["gitleaks"]
platform = {"Linux_x86_64": "linux_amd64", "Darwin_arm64": "darwin_arm64"}.get(sys.argv[2])
if platform is None or platform not in tool["archives"]:
    sys.exit("unsupported platform " + sys.argv[2])
print(tool["version"], tool["archives"][platform], tool["checksums_file"], tool.get("archive_sha256_" + platform) or "-")' "$script_root/versions.lock.json" "$(uname -s)_$(uname -m)")
read -r version name checksums_name recorded <<<"$pin"
[[ "$(basename "$archive")" == "$name" ]] || { echo "refused: archive must be $name" >&2; exit 2; }
[[ "$(basename "$checksums")" == "$checksums_name" ]] || { echo "refused: checksums file must be $checksums_name" >&2; exit 2; }
published=$(awk -v n="$name" '$2 == n || $2 == "*" n {print $1}' "$checksums")
[[ "$published" =~ ^[0-9a-f]{64}$ ]] || { echo "refused: $name not listed once in $checksums_name" >&2; exit 2; }
found=$(digest "$archive")
[[ "$found" == "$published" ]] || { echo 'refused: archive digest differs from the published checksum' >&2; exit 2; }
[[ "$recorded" == - || "$found" == "$recorded" ]] || { echo 'refused: archive digest differs from versions.lock.json' >&2; exit 2; }
work=$(mktemp -d "${TMPDIR:-/tmp}/nexloop-gitleaks-install.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar -xzf "$archive" -C "$work" gitleaks
[[ -f "$work/gitleaks" && ! -L "$work/gitleaks" ]] || { echo 'refused: archive has no regular gitleaks file' >&2; exit 2; }
chmod 0755 "$work/gitleaks"
[[ "$("$work/gitleaks" version)" == "$version" ]] || { echo "refused: binary is not gitleaks $version" >&2; exit 2; }
mkdir -p "$dest"
install -m 0755 "$work/gitleaks" "$dest/gitleaks"
printf 'installed gitleaks %s at %s\narchive_sha256 %s\nbinary_sha256 %s\n' "$version" "$dest/gitleaks" "$found" "$(digest "$dest/gitleaks")"
