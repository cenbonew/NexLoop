"""NX-042 T1: Python mirrors of the consumer twin rules in SQL (0275, temporary number): the minimum interval width (D4), the
real-content guard (D5) and the definition digest (sha256 of the jsonb text form)."""
from decimal import Decimal
import hashlib
import json
import re

MIN_INTERVAL_WIDTH = Decimal('0.01')
_LOOKS_REAL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}|[0-9]{7,}|[A-Za-z]+-[0-9]{3,}|[0-9A-Fa-f]{32,}')


def text_looks_real(text):
    return bool(_LOOKS_REAL.search(text or ''))


def definition_problems(d):
    """The interval and real-content rules the JSON Schema cannot express; an empty list when they hold."""
    problems = []
    for name, b in ((d.get('parameters') or {}).get('behaviors') or {}).items():
        if type(b) is dict and all(type(b.get(k)) in (int, float) for k in ('low', 'high')):
            if Decimal(str(b['high'])) - Decimal(str(b['low'])) < MIN_INTERVAL_WIDTH:
                problems.append(f'behaviour {name}: interval narrower than {MIN_INTERVAL_WIDTH}')
    texts = [d.get('description')] + [h.get('statement') for h in d.get('hypotheses') or () if type(h) is dict]
    if any(type(t) is str and text_looks_real(t) for t in texts):
        problems.append('real-looking content')
    return problems


def _jsonb_text(v):
    # PostgreSQL's jsonb output: object keys ordered by byte length then bytes, ", " and ": " separators.
    if type(v) is dict:
        keys = sorted(v, key=lambda k: (len(k.encode()), k.encode()))
        return '{' + ', '.join(json.dumps(k, ensure_ascii=False) + ': ' + _jsonb_text(v[k]) for k in keys) + '}'
    if type(v) is list:
        return '[' + ', '.join(_jsonb_text(x) for x in v) + ']'
    return json.dumps(v, ensure_ascii=False)


def definition_digest(d):
    return hashlib.sha256(_jsonb_text(d).encode()).hexdigest()
