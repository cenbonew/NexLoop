#!/usr/bin/env bash
# Owner 2026-10-18 (option B: vector recall and candidate merge on stage): installs the embedding profile, its key and the owner's
# embedding tariff on APP_HOST for recall-indexer and claim-matcher.
#
#   security find-generic-password -a nexloop -s nexloop/model/ark-embedding -w \
#     | ssh <APP_HOST> 'sudo NEXLOOP_ROOT=<root> EMBEDDING_PRICE_PER_MILLION=<price> bash <release tree>/scripts/deploy/install_embedding_key.sh'
#
# The key arrives on stdin only: never as an argument, never echoed, never logged (one line of printable ASCII without spaces, at
# most 8192 characters, as provider_credentials.py accepts). The profile defaults to the one the merge configuration was calibrated
# on (deploy/ontology/merge-config.v1.json: doubao-embedding-vision-251215 @ 1024, the multimodal text path); EMBEDDING_MODEL,
# EMBEDDING_DIMENSION and EMBEDDING_LOCATION may override it. EMBEDDING_PRICE_PER_MILLION (the price of one million input tokens,
# from the owner's Ark price list; nothing here assumes one) and EMBEDDING_CURRENCY (default CNY, the tenant model budget's unit)
# form the tariff by which embedding calls are counted in the tenant's model budget (nexloop_eios.embedding_budget).
# Writes three files with install (owner 10001, mode 0600) and prints only names, owners, modes and sizes. Recreating the two
# services is a separate step (local-only/ops/stage-embedding-sheet.md).
set -euo pipefail
set +x
umask 077
export LC_ALL=C
: "${NEXLOOP_ROOT:?NEXLOOP_ROOT}"
: "${EMBEDDING_PRICE_PER_MILLION:?EMBEDDING_PRICE_PER_MILLION (owner price list)}"
MODEL=${EMBEDDING_MODEL:-doubao-embedding-vision-251215}
DIMENSION=${EMBEDDING_DIMENSION:-1024}
LOCATION=${EMBEDDING_LOCATION:-https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal}
CURRENCY=${EMBEDDING_CURRENCY:-CNY}
SECRETS=$NEXLOOP_ROOT/secrets
OWNER=10001

fail() { echo "install_embedding_key: $1" >&2; exit 2; }

[ "$(id -u)" = 0 ] || fail 'run as root (sudo)'
[ ! -t 0 ] || fail 'the key is read from a pipe, not a terminal'
case "$MODEL" in *[![:graph:]]*|'') fail 'model must be printable ASCII without spaces' ;; esac
case "$DIMENSION" in ''|*[!0-9]*) fail 'dimension must be a decimal number' ;; esac
case "$LOCATION" in https://ark.cn-beijing.volces.com/*/embeddings/multimodal) ;; *) fail 'location must be the Ark multimodal embeddings path' ;; esac
case "$CURRENCY" in [A-Z][A-Z][A-Z]) ;; *) fail 'currency must be three upper-case letters' ;; esac
case "$EMBEDDING_PRICE_PER_MILLION" in ''|*[!0-9.]*|*.*.*|.*|*.) fail 'price must be a decimal number such as 0.70' ;; esac

IFS= read -r KEY || [ -n "${KEY:-}" ] || fail 'no key on stdin'
if IFS= read -r _extra; then fail 'more than one line on stdin'; fi
case "$KEY" in *[![:graph:]]*|'') fail 'the key must be printable ASCII without spaces' ;; esac
[ "${#KEY}" -le 8192 ] || fail 'the key is longer than 8192 characters'

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
printf 'EMBEDDING_MODEL=%s\nEMBEDDING_MODE=multimodal\nEMBEDDING_LOCATION=%s\nEMBEDDING_DIMENSION=%s\nEMBEDDING_API_KEY=%s\n' \
  "$MODEL" "$LOCATION" "$DIMENSION" "$KEY" > "$T/embedding.env"
unset KEY
printf '{"schema": "nexloop-embedding-tariff/1", "model": "%s", "currency": "%s", "per_million_tokens": "%s"}\n' \
  "$MODEL" "$CURRENCY" "$EMBEDDING_PRICE_PER_MILLION" > "$T/embedding-tariff.json"

for service in recall-indexer claim-matcher; do
  install -o "$OWNER" -g "$OWNER" -m 0600 "$T/embedding.env" "$SECRETS/$service/embedding.env"
done
[ -d "$SECRETS/shared" ] || fail "$SECRETS/shared missing (stage secrets not installed)"
install -o "$OWNER" -g "$OWNER" -m 0600 "$T/embedding-tariff.json" "$SECRETS/shared/embedding-tariff.json"
shred -u "$T/embedding.env"

stat -c '%n %u %a %s' "$SECRETS/recall-indexer/embedding.env" "$SECRETS/claim-matcher/embedding.env" "$SECRETS/shared/embedding-tariff.json"
