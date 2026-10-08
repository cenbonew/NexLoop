#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
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
    # Equivalent patterns use character classes to avoid matching their own source.
    local identifiers='192\.168\.31\.(140|73)|openclaw[-]001|openclaw[-]thinkpad|SHA256[:]kr1aSJ|SHA256[:]Ncgbuz'
    local status=0
    git grep -q -E "$identifiers" $revisions -- . || status=$?
    if [[ "$status" -eq 0 ]]; then printf 'FAIL restricted identifiers in outgoing commit trees (content redacted)\n'; return 1; fi
    if [[ "$status" -ne 1 ]]; then printf 'FAIL history scan could not complete\n'; return 1; fi
  fi
  printf 'PASS restricted identifiers in every outgoing commit\n'
  matches=$(git log -p "$base..$ref" | grep -E '^\+.*(sk-[A-Za-z0-9]{20,}|xz[z]n|(MODEL|EMBEDDING)_API_KEY="?[A-Za-z0-9-]{8,})' || test "$?" -eq 1)
  if [[ -n "$matches" ]]; then printf 'FAIL credential values in outgoing added lines (content redacted)\n'; return 1; fi
  printf 'PASS credential values in outgoing added lines\n'
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
else
  scan_ref "${1:-main}" "${2:-origin/main}"
fi
