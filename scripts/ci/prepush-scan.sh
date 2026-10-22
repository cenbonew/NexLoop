#!/usr/bin/env bash
set -euo pipefail
# The pinned tool versions come from the checkout this script belongs to, not from the tree being scanned.
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# Work from the repository top level, or from the git directory of a bare repository (the CI controller's merge gate and the
# GitHub sync run on the bare repository). No gate reads the working tree: every one reads the scanned commits through git
# (ls-tree, log, grep, show), so a bare repository is scanned exactly like a checkout. Anything else fails.
if [[ "$(git rev-parse --is-bare-repository 2>/dev/null || true)" == true ]]; then
  cd "$(git rev-parse --absolute-git-dir)" || { printf 'FAIL prepush-scan: git directory unavailable\n'; exit 1; }
elif top=$(git rev-parse --show-toplevel 2>/dev/null) && [[ -n "$top" ]]; then
  cd "$top" || { printf 'FAIL prepush-scan: top level unavailable\n'; exit 1; }
else
  printf 'FAIL prepush-scan: not inside a git repository\n'; exit 1
fi
# Gate 4 (NX-060): gitleaks at exactly the pinned version over the outgoing commits (or, with --full, the whole history).
# Trust: the configuration (.gitleaks.toml) and the pin (versions.lock.json release_tools.gitleaks) are taken from the base
# commit, which is already published, and only where the base has none (first introduction, or --full) from the checkout
# this script belongs to; never from the commits being scanned. A change to .gitleaks.toml in those commits is reported for
# owner ack and does not apply to this scan. The configuration must pass scripts/ci/check_gitleaks_config.py. gitleaks runs
# from an empty private directory on the git directory, with an empty ignore path and --ignore-gitleaks-allow, so neither a
# .gitleaksignore nor a `gitleaks:allow` comment can hide a finding; a tracked .gitleaksignore fails. Fails closed on a
# missing tool, another version, a binary digest that differs from or (on linux_amd64) is not yet in the pin, an
# unsupported platform, a configuration that is missing, breaks the structure rules or no longer detects a synthetic
# sentinel, or a scan that does not complete. Findings are counted, never printed. Runs in a subshell (own trap).
gitleaks_gate() (
  local ref="$1" range="$2" label="$3" base="${4:-}" bin pin pinned expected version found work status count gitdir
  local source=checkout platform
  umask 077
  work=$(mktemp -d "${TMPDIR:-/tmp}/nexloop-gitleaks.XXXXXX")
  trap 'rm -rf "$work"' EXIT
  mkdir "$work/run"
  # The trusted pin and configuration: the base commit's when it has them, else this checkout's.
  # A trusted CI installation carries scripts/ci only (OPERATIONS §7.2): there both come from the base commit.
  cp "$script_root/versions.lock.json" "$work/lock.json" 2>/dev/null || rm -f "$work/lock.json"
  cp "$script_root/.gitleaks.toml" "$work/config.toml" 2>/dev/null || rm -f "$work/config.toml"
  if [[ -n "$base" ]] && git show "$base:.gitleaks.toml" >"$work/base-config.toml" 2>/dev/null \
     && git show "$base:versions.lock.json" >"$work/base-lock.json" 2>/dev/null \
     && python3 -c 'import json,sys;json.load(open(sys.argv[1]))["release_tools"]["gitleaks"]["version"]' "$work/base-lock.json" 2>/dev/null; then
    mv "$work/base-config.toml" "$work/config.toml"; mv "$work/base-lock.json" "$work/lock.json"; source=base
  fi
  case "$(uname -s)_$(uname -m)" in Linux_x86_64) platform=linux_amd64 ;; Darwin_arm64) platform=darwin_arm64 ;; *) platform= ;; esac
  if [[ -z "$platform" ]]; then printf 'FAIL gitleaks gate: platform %s is not pinned\n' "$(uname -s)_$(uname -m)"; exit 1; fi
  pin=$(python3 -c 'import json,sys
tool = json.load(open(sys.argv[1]))["release_tools"]["gitleaks"]
print(tool["version"], tool.get("binary_sha256_" + sys.argv[2]) or "-")' "$work/lock.json" "$platform"
  ) || { printf 'FAIL gitleaks pin unreadable (no trusted versions.lock.json with release_tools.gitleaks)\n'; exit 1; }
  read -r pinned expected <<<"$pin"
  bin="${NEXLOOP_GITLEAKS:-$(command -v gitleaks || true)}"
  if [[ -z "$bin" || ! -x "$bin" ]]; then printf 'FAIL gitleaks not installed (pinned %s)\n' "$pinned"; exit 1; fi
  version=$("$bin" version 2>/dev/null | head -n 1) || version=unknown
  version=${version#v}
  if [[ "$version" != "$pinned" ]]; then printf 'FAIL gitleaks version %s is not the pinned %s\n' "$version" "$pinned"; exit 1; fi
  # linux_amd64 (the CI and release hosts) runs only the binary whose digest is recorded; the Homebrew build of the
  # development machine has no release digest and is checked by version (docs/implementation/NX-060-gitleaks.md §4).
  if [[ "$expected" == - && "$platform" == linux_amd64 ]]; then
    printf 'FAIL gitleaks binary digest not recorded for linux_amd64 (install with scripts/ci/install-gitleaks.sh, record it)\n'; exit 1
  fi
  if [[ "$expected" != - ]]; then
    found=$( { sha256sum "$bin" 2>/dev/null || shasum -a 256 "$bin"; } | cut -d' ' -f1)
    if [[ "$found" != "$expected" ]]; then printf 'FAIL gitleaks binary digest is not the recorded one\n'; exit 1; fi
  fi
  if [[ ! -f "$work/config.toml" ]]; then printf 'FAIL trusted .gitleaks.toml missing\n'; exit 1; fi
  if ! python3 "$script_root/scripts/ci/check_gitleaks_config.py" "$work/config.toml" "$work/lock.json" >"$work/structure" 2>&1; then
    printf 'FAIL gitleaks configuration (%s) breaks the structure rules: %s\n' "$source" "$(head -n 1 "$work/structure")"; exit 1
  fi
  if [[ -n "$base" ]] && ! git diff --quiet "$base" "$ref" -- .gitleaks.toml; then
    printf 'NOTE .gitleaks.toml changes in the outgoing commits: owner ack required; scanned with the %s configuration\n' "$source"
  fi
  gitdir=$(git rev-parse --absolute-git-dir)
  if git -c core.quotePath=false ls-tree -r -z --name-only "$ref" | tr '\0' '\n' | grep -qE '(^|/)\.gitleaksignore$' || [[ -e .gitleaksignore || -e "$gitdir/.gitleaksignore" ]]; then
    printf 'FAIL a .gitleaksignore is present (tracked in %s, in the worktree or in the git directory)\n' "$ref"; exit 1
  fi
  run() { (cd "$work/run" && env -u GITLEAKS_CONFIG -u GITLEAKS_CONFIG_TOML "$bin" "$@" --config "$work/config.toml" \
            --gitleaks-ignore-path "$work/run" --ignore-gitleaks-allow --no-banner --redact --log-level error --exit-code 17); }
  # A synthetic access key id, assembled here so that this file never contains it whole.
  status=0
  printf 'aws_access_key_id = %s%s%s\n' AKIA Z7Q3XW2V Y6TR4PMN | run stdin >/dev/null 2>&1 || status=$?
  if [[ "$status" -ne 17 ]]; then printf 'FAIL gitleaks configuration no longer detects the sentinel key\n'; exit 1; fi
  status=0
  run git --log-opts="-m $range" --report-format json --report-path "$work/report.json" "$gitdir" >/dev/null 2>&1 || status=$?
  if [[ "$status" -eq 17 ]]; then
    count=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$work/report.json" 2>/dev/null || echo some)
    printf 'FAIL gitleaks: %s finding(s) in %s (content redacted)\n' "$count" "$label"; exit 1
  fi
  if [[ "$status" -ne 0 ]]; then printf 'FAIL gitleaks scan could not complete\n'; exit 1; fi
  printf 'PASS gitleaks %s in %s (%s configuration)\n' "$version" "$label" "$source"
)
# Gate 1's rule over NUL-separated paths on stdin: exit 1 when any path is private or runtime data (docs/tmp, local-only, a
# .env file in any directory, an SQLite file, anything under a secrets/ directory), 0 when none is. The caller's pipeline
# (pipefail) turns a failed git listing into its own non-zero status.
path_rule() {
  python3 -c 'import re, sys
rule = re.compile(r"(docs/tmp|local-only|(.*/)?\.env\Z|.*\.sqlite|.*secrets/)", re.S)
paths = sys.stdin.buffer.read().decode("utf-8", "surrogateescape").split("\0")
sys.exit(1 if any(rule.match(p) for p in paths if p) else 0)' || return $?
}
# git grep over the trees of many commits in batches of 200 (a whole history stays under the argument limit):
# 0 a match in some tree, 1 none, 2 a git error.
grep_revisions() {
  local pattern="$1" rev status n=0
  local -a batch
  while read -r rev; do
    [[ -n "$rev" ]] || continue
    batch[n]="$rev"; n=$((n + 1))
    if [[ "$n" -eq 200 ]]; then
      status=0; git grep -q -E "$pattern" "${batch[@]}" -- . || status=$?
      [[ "$status" -eq 1 ]] || return $(( status == 0 ? 0 : 2 ))
      batch=(); n=0
    fi
  done <<<"$2"
  if [[ "$n" -gt 0 ]]; then
    status=0; git grep -q -E "$pattern" "${batch[@]}" -- . || status=$?
    [[ "$status" -eq 1 ]] || return $(( status == 0 ? 0 : 2 ))
  fi
  return 1
}
# One scan of the commits <base>..<ref>, or with an empty base of everything reachable from <ref> (--root, first publication).
scan_ref() {
  local ref="$1" base="$2" range revisions matches status=0
  git rev-parse --verify -q "$ref^{commit}" >/dev/null || { printf 'FAIL prepush-scan: %s is not a commit\n' "$ref"; return 1; }
  if [[ -n "$base" ]]; then
    git rev-parse --verify -q "$base^{commit}" >/dev/null || { printf 'FAIL prepush-scan: base %s is not a commit\n' "$base"; return 1; }
    range="$base..$ref"
    printf 'prepush-scan: %s against %s\n' "$ref" "$base"
  else
    range="$ref"
    printf 'prepush-scan: %s from the root (whole history)\n' "$ref"
  fi
  # Gate 1: no private or runtime path in the scanned commit's tree, among the paths any outgoing commit touches, merges
  # against each parent included (-m: a path added and removed again inside the range is still published in history), or,
  # in a checkout, in the index (that can refuse for a staged path the scanned ref does not contain: fail-closed). Every
  # listing is NUL-separated with core.quotePath=false, so a path with non-ASCII bytes, quotes, spaces or newlines is
  # matched as itself, never as git's quoted form. Matching is in path_rule.
  local status=0
  {
    git -c core.quotePath=false ls-tree -r -z --name-only "$ref" || exit 2
    git -c core.quotePath=false log -z -m --format= --name-only --no-renames "$range" || exit 2
    if [[ "$(git rev-parse --is-bare-repository)" != true ]]; then git -c core.quotePath=false ls-files -z || exit 2; fi
  } | path_rule || status=$?
  if [[ "$status" -eq 1 ]]; then printf 'FAIL tracked private/runtime paths\n'; return 1; fi
  if [[ "$status" -ne 0 ]]; then printf 'FAIL tracked path listing could not complete\n'; return 1; fi
  printf 'PASS tracked private/runtime paths\n'
  revisions=$(git rev-list "$range") || { printf 'FAIL commit listing could not complete\n'; return 1; }
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
    status=0
    grep_revisions "$identifiers" "$revisions" || status=$?
    if [[ "$status" -eq 0 ]]; then printf 'FAIL restricted identifiers in outgoing commit trees (content redacted)\n'; return 1; fi
    if [[ "$status" -ne 1 ]]; then printf 'FAIL history scan could not complete\n'; return 1; fi
  fi
  printf 'PASS restricted identifiers in every outgoing commit\n'
  # -m: a merge's own changes (an "evil" conflict resolution) against each parent; plain git log -p shows no merge diff.
  # grep decides a match on its own (git may then end on SIGPIPE); with no match, git log itself must have succeeded.
  local codes
  set +e
  git log -p -m "$range" | grep -qE '^\+.*(sk-[A-Za-z0-9]{20,}|xz[z]n|(MODEL|EMBEDDING)_API_KEY="?[A-Za-z0-9-]{8,})'
  codes="${PIPESTATUS[0]} ${PIPESTATUS[1]}"
  set -e
  if [[ "${codes#* }" == 0 ]]; then printf 'FAIL credential values in outgoing added lines (content redacted)\n'; return 1; fi
  if [[ "$codes" != '0 1' ]]; then printf 'FAIL credential scan could not complete\n'; return 1; fi
  printf 'PASS credential values in outgoing added lines\n'
  gitleaks_gate "$ref" "$range" 'outgoing added lines' "$base"
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
elif [[ "${1:-}" == '--root' ]]; then
  # All four gates over the whole history reachable from the ref (a first publication with no synced base).
  scan_ref "${2:-HEAD}" ''
elif [[ "${1:-}" == '--full' ]]; then
  # Optional full mode: gate 4 alone over every commit reachable from the ref (default HEAD), with this checkout's
  # configuration, for audits and first publication. It does not replace the three gates of the normal mode.
  ref="${2:-HEAD}"
  git rev-parse --verify "$ref^{commit}" >/dev/null
  printf 'prepush-scan: full history of %s\n' "$ref"
  gitleaks_gate "$ref" "$ref" 'the full history'
else
  scan_ref "${1:-main}" "${2:-origin/main}"
fi
