"""NX-042 T1: Python mirrors of the consumer twin rules in SQL (0278, temporary number): the minimum interval width (D4), the
real-content guard (D5) and the definition digest: sha256 of the frozen form's jsonb text - every free text replaced by
'sha256:<hex>' (the erasable side table holds the text, D5) and the numbers normalised (SQL trim_scale), so the digest neither
changes on erasure nor depends on how a number was written (0.10 and 0.1 are the same)."""
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
    if type(v) in (int, float, Decimal):
        return format(Decimal(repr(v) if type(v) is float else v).normalize(), 'f')
    return json.dumps(v, ensure_ascii=False)


def _hash(text):
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()


def frozen_form(d):
    """runtime.nexloop_consumer_twin_redact: the texts as hashes (the numbers are normalised when written out)."""
    f = dict(d)
    if 'description' in f:
        f['description'] = _hash(f['description'])
    f['hypotheses'] = [{**h, 'statement': _hash(h['statement'])} for h in d['hypotheses']]
    return f


def definition_digest(d):
    return hashlib.sha256(_jsonb_text(frozen_form(d)).encode()).hexdigest()
