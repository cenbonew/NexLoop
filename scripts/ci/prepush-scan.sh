#!/usr/bin/env bash
set -euo pipefail
# The pinned tool versions come from the checkout this script belongs to, not from the tree being scanned.
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$(git rev-parse --show-toplevel)"
# Gate 4 (NX-060): gitleaks at exactly the version pinned in versions.lock.json (release_tools.gitleaks), with the
# configuration of the scanned commit itself. Missing tool, other version, other binary digest (when one is recorded for
# this platform), missing configuration, a configuration that no longer detects a synthetic sentinel key, or a scan that
# does not complete all fail closed. Findings are counted, never printed (--redact; the report stays in a private
# temporary directory that is removed). Runs in a subshell so its trap cleans up on every exit.
gitleaks_gate() (
  local ref="$1" range="$2" label="$3" bin pin pinned expected version found work status count
  bin="${NEXLOOP_GITLEAKS:-$(command -v gitleaks || true)}"
  pin=$(python3 -c 'import json,sys
tool = json.load(open(sys.argv[1]))["release_tools"]["gitleaks"]
platform = {"Linux_x86_64": "linux_amd64", "Darwin_arm64": "darwin_arm64"}.get(sys.argv[2], "")
print(tool["version"], tool.get("binary_sha256_" + platform) or "-")' "$script_root/versions.lock.json" "$(uname -s)_$(uname -m)"
  ) || { printf 'FAIL gitleaks pin unreadable in versions.lock.json\n'; exit 1; }
  read -r pinned expected <<<"$pin"
  if [[ -z "$bin" || ! -x "$bin" ]]; then printf 'FAIL gitleaks not installed (pinned %s)\n' "$pinned"; exit 1; fi
  version=$("$bin" version 2>/dev/null | head -n 1) || version=unknown
  version=${version#v}
  if [[ "$version" != "$pinned" ]]; then printf 'FAIL gitleaks version %s is not the pinned %s\n' "$version" "$pinned"; exit 1; fi
  if [[ "$expected" != - ]]; then
    found=$( { sha256sum "$bin" 2>/dev/null || shasum -a 256 "$bin"; } | cut -d' ' -f1)
    if [[ "$found" != "$expected" ]]; then printf 'FAIL gitleaks binary digest is not the recorded one\n'; exit 1; fi
  fi
  umask 077
  work=$(mktemp -d "${TMPDIR:-/tmp}/nexloop-gitleaks.XXXXXX")
  trap 'rm -rf "$work"' EXIT
  if ! git show "$ref:.gitleaks.toml" >"$work/config.toml" 2>/dev/null; then printf 'FAIL .gitleaks.toml missing in %s\n' "$ref"; exit 1; fi
  # A synthetic access key id, assembled here so that this file never contains it whole.
  status=0
  printf 'aws_access_key_id = %s%s%s\n' AKIA Z7Q3XW2V Y6TR4PMN | "$bin" stdin --config "$work/config.toml" --no-banner \
    --redact --log-level error --exit-code 17 >/dev/null 2>&1 || status=$?
  if [[ "$status" -ne 17 ]]; then printf 'FAIL gitleaks configuration no longer detects the sentinel key\n'; exit 1; fi
  status=0
  "$bin" git --config "$work/config.toml" --no-banner --redact --log-level error --exit-code 17 --log-opts="$range" \
    --report-format json --report-path "$work/report.json" . >/dev/null 2>&1 || status=$?
  if [[ "$status" -eq 17 ]]; then
    count=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$work/report.json" 2>/dev/null || echo some)
    printf 'FAIL gitleaks: %s finding(s) in %s (content redacted)\n' "$count" "$label"; exit 1
  fi
  if [[ "$status" -ne 0 ]]; then printf 'FAIL gitleaks scan could not complete\n'; exit 1; fi
  printf 'PASS gitleaks %s in %s\n' "$version" "$label"
)
scan_ref() {
  local ref="$1" base="$2" revisions matches
  git rev-parse --verify "$ref^{commit}" >/dev/null
  git rev-parse --verify "$base^{commit}" >/dev/null
  printf 'prepush-scan: %s against %s\n' "$ref" "$base"
  matches=$(git ls-files | grep -E '^(docs/tmp|local-only|\.env$|.*\.sqlite|.*secrets/)' || test "$?" -eq 1)
  if [[ -n "$matches" ]]; then printf 'FAIL tracked private/runtime paths\n'; return 1; fi
  printf 'PASS tracked private/runtime paths\n'
  revisions=$(git rev-list "$base..$ref")
  if [[ -n "$revisions" ]]; then
    # The restricted identifiers (private LAN addresses, host names and aliases, host key fingerprints, local user paths)
    # are kept out of the public repository: one extended regular expression per line in a private list, by default
    # local-only/restricted-identifiers.txt at the root of the main checkout (git-ignored), or NEXLOOP_RESTRICTED_IDENTIFIERS_FILE.
    # A missing or empty list fails closed.
    local common list identifiers
    common=$(git rev-parse --path-format=absolute --git-common-dir)
    list="${NEXLOOP_RESTRICTED_IDENTIFIERS_FILE:-$(dirname "$common")/local-only/restricted-identifiers.txt}"
    identifiers=$( { grep -vE '^[[:space:]]*(#|$)' "$list" 2>/dev/null || true; } | paste -sd'|' -)
    if [[ -z "$identifiers" ]]; then printf 'FAIL restricted identifier list missing or empty (content redacted)\n'; return 1; fi
    local status=0
    git grep -q -E "$identifiers" $revisions -- . || status=$?
    if [[ "$status" -eq 0 ]]; then printf 'FAIL restricted identifiers in outgoing commit trees (content redacted)\n'; return 1; fi
    if [[ "$status" -ne 1 ]]; then printf 'FAIL history scan could not complete\n'; return 1; fi
  fi
  printf 'PASS restricted identifiers in every outgoing commit\n'
  matches=$(git log -p "$base..$ref" | grep -E '^\+.*(sk-[A-Za-z0-9]{20,}|xz[z]n|(MODEL|EMBEDDING)_API_KEY="?[A-Za-z0-9-]{8,})' || test "$?" -eq 1)
  if [[ -n "$matches" ]]; then printf 'FAIL credential values in outgoing added lines (content redacted)\n'; return 1; fi
  printf 'PASS credential values in outgoing added lines\n'
  gitleaks_gate "$ref" "$base..$ref" 'outgoing added lines'
}
if [[ "${1:-}" == '--hook' ]]; then
  remote="${2:-}"
  [[ "$remote" == origin ]] || { printf 'FAIL only origin synchronization is authorized\n'; exit 1; }
  while read -r local_ref local_sha remote_ref remote_sha; do
    [[ "$local_ref" != *local-only* && "$remote_ref" != *local-only* ]] || { printf 'FAIL private branch publication\n'; exit 1; }
    [[ "$local_sha" != 0000000000000000000000000000000000000000 ]] || { printf 'FAIL ref deletion is not authorized\n'; exit 1; }
    if [[ "$remote_ref" == refs/heads/main && "$remote_sha" != 0000000000000000000000000000000000000000 ]]; then
      git merge-base --is-ancestor "$remote_sha" "$local_sha" || { printf 'FAIL non-fast-forward main\n'; exit 1; }
    fi
    scan_ref "$local_sha" origin/main
  done
elif [[ "${1:-}" == '--full' ]]; then
  # Optional full mode: gate 4 over every commit reachable from the ref (default HEAD), for audits and first publication.
  ref="${2:-HEAD}"
  git rev-parse --verify "$ref^{commit}" >/dev/null
  printf 'prepush-scan: full history of %s\n' "$ref"
  gitleaks_gate "$ref" "$ref" 'the full history'
else
  scan_ref "${1:-main}" "${2:-origin/main}"
fi
