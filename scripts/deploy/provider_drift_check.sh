#!/usr/bin/env bash
# Owner 2026-10-19: scheduled, read-only model provider address drift check on APP_HOST (nexloop-provider-drift.timer).
#
#   NEXLOOP_ROOT=<root> STAGE_ENV=<private stage env> BACKEND_IMAGE=<backend image by digest> bash scripts/deploy/provider_drift_check.sh
#
# Runs `nexloop-doctor --provider-drift` in the backend image (host network, so it resolves as the host and the containers do)
# against the private stage env (MODEL_PROVIDER_ALLOWLIST) and the rendered rules config/firewall/app-host.rules.nft, both
# mounted read only. No drift: exit 0, nothing logged. Drift (a provider host resolves to an address the allowlist lacks, a host
# does not resolve, or the rendered egress rules differ from the env): one structured journal line (tag nexloop-provider-drift,
# priority warning; addresses and codes only) and exit 1, so the unit fails visibly (systemctl --failed, OnFailure= if set).
# It never changes the firewall: the owner confirms, then the env is edited and the rules re-rendered and reloaded (OPERATIONS 8a).
set -euo pipefail
: "${NEXLOOP_ROOT:?NEXLOOP_ROOT}" "${STAGE_ENV:?STAGE_ENV}" "${BACKEND_IMAGE:?BACKEND_IMAGE}"
RULES=$NEXLOOP_ROOT/config/firewall/app-host.rules.nft
[ -f "$STAGE_ENV" ] || { echo "provider_drift_check: $STAGE_ENV missing" >&2; exit 2; }
[ -f "$RULES" ] || { echo "provider_drift_check: $RULES missing" >&2; exit 2; }
set +e
OUT=$(docker run --rm --network host --read-only --cap-drop ALL --security-opt no-new-privileges:true --user "$(stat -c '%u' "$STAGE_ENV")" \
  -v "$STAGE_ENV:/run/drift/stage.env:ro" -v "$RULES:/run/drift/app-host.rules.nft:ro" --entrypoint nexloop-doctor "$BACKEND_IMAGE" \
  --provider-drift --stage-env /run/drift/stage.env --nft-rules /run/drift/app-host.rules.nft)
CODE=$?
set -e
if [ "$CODE" -eq 0 ]; then exit 0; fi
SUMMARY=$(printf '%s' "$OUT" | python3 -c 'import json,sys
r=json.load(sys.stdin);d=r.get("details",{})
print(json.dumps({"event":"provider drift","checks":{k:v for k,v in r.get("checks",{}).items() if not v},"missing":d.get("missing",[]),
 "unresolved_hosts":d.get("unresolved_hosts",[]),"stale_rules":sorted((d.get("nft_rules") or {}).get("stale",{})),"error":d.get("provider_drift_error")},sort_keys=True))' \
  2>/dev/null || echo '{"event":"provider drift","error":"doctor output unreadable"}')
logger -t nexloop-provider-drift -p user.warning -- "$SUMMARY"
echo "$SUMMARY"
exit 1
