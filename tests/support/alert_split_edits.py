"""NX-068 defect A+B (alert_evaluate_split): authz.nexloop_alert_evaluate is re-created to evaluate a snapshot taken in its own
transaction (T2) - the maintenance and the snapshot leave this authorized transaction, the snapshot is checked and consumed, the rules
use its taken_at, monotonic per state, and a body without snapshot_id keeps the old single transaction. For the copy guard: the whole
body of 0131 (old) and of alert_evaluate_split (new), read from the two migrations (old, new)."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / 'packages/eios-core/src/eios/migrations'
NAME = 'authz.nexloop_alert_evaluate'


def _body(stem):
    text = next(MIGRATIONS.glob(f'*_{stem}.sql')).read_text()
    head = re.search(r'create (?:or replace )?function ' + re.escape(NAME) + r'\(.*?\$\$', text, flags=re.S)
    return text[head.end():text.index('$$', head.end())]


EDITS = {NAME: [[_body('nx030_observability'), _body('alert_evaluate_split')]]}
