"""NX-068 N1 (nx068_publish_gate_smoke): authz.nexloop_effect_scope_successor_proof is re-created to ask the single A' judgement
authz.nexloop_effect_successor_admissible for each candidate (behaviour unchanged, 0446's tests). For the copy guard: the whole
body of 0446 (old) and of nx068 (new), read from the two migrations (old, new)."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / 'packages/eios-core/src/eios/migrations'
NAME = 'authz.nexloop_effect_scope_successor_proof'


def _body(stem):
    text = next(MIGRATIONS.glob(f'*_{stem}.sql')).read_text()
    head = re.search(r'create (?:or replace )?function ' + re.escape(NAME) + r'\(.*?\$\$', text, flags=re.S)
    return text[head.end():text.index('$$', head.end())]


EDITS = {NAME: [[_body('effect_scope_successor_proof'), _body('nx068_publish_gate_smoke')]]}
