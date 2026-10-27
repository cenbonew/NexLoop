"""NX-054 §8 (the relation erased values migration): the commitments phase of the erasure writes Commitment.made_to =
'erasure:<request id>' (0138, 0358), the stage precheck of 2026-10-27 found 44 of them ("erasure:workbench-erase-synthetic-<64 hex>")
and blocked publication. One judgement, runtime.nexloop_relation_value_erased: such a marker, like 'pseudonym:...', counts among the
precheck's pseudonyms (nothing blocks), derives no relation and is never counted dangling - on insert, and on the update that
replaces a Consumer id by it (the derived relation goes). A value that is neither stays a blocking finding."""
import hashlib

import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_publish_runbook_pg import precheck, publish
from test_relation_types_p1_pg import catalog  # noqa: F401
from test_relation_types_p2_pg import dangling, edges, hexid, put

MARKER = 'erasure:workbench-erase-synthetic-' + hashlib.sha256(b'erased-consumer').hexdigest()


def commitment_row(doc):
    return next(f for f in doc['fields'] if f['relation'] == 'commitment_to' and f['world'] == 'real')


def test_the_erasure_marker_is_a_pseudonym_in_the_precheck(catalog):  # noqa: F811
    admin = catalog['admin']
    consumer = hexid('ev-consumer')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Commitment', hexid('ev-c1'), {'made_to': consumer}, replica=True)
    put(admin, 'Commitment', hexid('ev-c2'), {'made_to': MARKER}, replica=True)
    code, doc = precheck(catalog)
    row = commitment_row(doc)
    assert code == 0 and row['with_value'] == 2 and row['hex64'] == 1 and row['pseudonyms'] == 1 and doc['blocking'] == []
    put(admin, 'Commitment', hexid('ev-c3'), {'made_to': 'erasure:'}, replica=True)  # no request id: not the marker
    code, doc = precheck(catalog)
    assert code == 1 and any('commitment_to' in b for b in doc['blocking'])


def test_the_erasure_marker_derives_nothing_and_is_not_dangling(catalog):  # noqa: F811
    """Through the derivation itself (Goal.consumer_id: a Commitment's creation is guarded, the judgement is per value)."""
    admin = catalog['admin']
    assert publish(catalog)[0] == 0
    consumer, kept, erased = hexid('ev2-consumer'), hexid('ev2-kept'), hexid('ev2-erased')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', erased, {'consumer_id': MARKER})  # an ordinary insert: the trigger derives at commit
    assert edges(admin, 'goal_of', erased) == [] and dangling(admin) == {}
    put(admin, 'Goal', kept, {'consumer_id': consumer})
    assert edges(admin, 'goal_of', kept) == [(kept, consumer, 'real')]
    # The erasure replaces the Consumer id by the marker: the derived relation goes, nothing is counted dangling.
    admin.execute("update ontology.objects set properties=jsonb_set(properties,'{consumer_id}',to_jsonb(%s::text)) where object_id=%s", (MARKER, kept))
    assert edges(admin, 'goal_of', kept) == [] and dangling(admin) == {}
    # A value that is neither a 64-hex id nor an erased marker is still counted.
    put(admin, 'Goal', hexid('ev2-odd'), {'consumer_id': 'erasure:'})
    assert dangling(admin) == {'real:goal_of': 1}


@pytest.mark.parametrize('value,erased', [(MARKER, True), ('pseudonym:' + 'a' * 32, True), ('erasure:at050-erase:20261018-1', True),
                                          ('erasure:', False), ('erasure:bad value', False), ('a' * 64, False), (None, False), ('', False)])
def test_the_one_judgement(catalog, value, erased):  # noqa: F811
    assert catalog['admin'].execute('select runtime.nexloop_relation_value_erased(%s)', (value,)).fetchone()[0] is erased
