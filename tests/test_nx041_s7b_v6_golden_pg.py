"""NX-041 S7b review 4.2: a golden regression of the real v6 Context, so that 'the real assembly is unchanged' is held by a test and
not only by the entries' bodies (their helpers were redefined in S1 and S5; S1/S5 reviews checked real/test equivalence).

Fixed input (the v6 fixture of test_context_v6_pg: one customer message, the built-in strategy, no Claims), real path only. The
bound pack is normalized - identifiers, digests and times replaced by stable placeholders in order of first appearance - and compared
byte for byte with tests/support/golden/nx041-context-v6-real.json. NEXLOOP_WRITE_GOLDEN=1 rewrites the file (a reviewed change).
"""
import json
import os
import re
from pathlib import Path

from nexloop_eios.contracts import ContextPackV6
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_artifacts import context_message  # noqa: F401
from test_context_v6_pg import bound, v6  # noqa: F401

GOLDEN = Path(__file__).resolve().parent / 'support/golden/nx041-context-v6-real.json'
PATTERNS = [('uuid', re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')), ('hex64', re.compile(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])')),
            ('hex32', re.compile(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])')),
            ('time', re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?'))]


def normalize(pack):
    seen = {}
    text = json.dumps(pack, ensure_ascii=False, sort_keys=True)
    for name, pattern in PATTERNS:
        def place(m, name=name):
            return seen.setdefault((name, m.group(0)), f'<{name}-{sum(1 for k in seen if k[0] == name) + 1}>')
        text = pattern.sub(place, text)
    return json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True, indent=1) + '\n'


def test_the_real_v6_context_of_a_fixed_input_is_unchanged(v6, admin):  # noqa: F811
    f = v6
    assert f['v6_relay']().run_once() == 'queued'
    pack, _ = bound(admin)
    ContextPackV6.model_validate(pack)
    assert pack['bindings']['world_id'] == 'real'
    actual = normalize(pack)
    if os.environ.get('NEXLOOP_WRITE_GOLDEN') == '1':
        GOLDEN.write_text(actual)
    assert actual == GOLDEN.read_text()
