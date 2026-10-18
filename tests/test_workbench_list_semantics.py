"""Walkthrough 2026-10-18 batch 7 (F03/F04/F05/F09), the read layer without a database: WorkbenchQueries over a fake governed
reader. The list, its counts and its exceptions describe one scope; filters, sort and the offset page are applied to that one
governed result; the promised words (one Message READ each) and customer names (one per distinct customer) are read for the page
only; the name state is decided in one order everywhere. The SQL side (governed reads, AT-003 derivation) is covered by the PG
tests; here only what this layer adds."""
import pytest

from nexloop_eios.workbench_reads import WorkbenchForbidden, WorkbenchQueries

A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


def view(cid, consumer, status, *, due=None, late=False, quote='我明天给您寄出', message=None):
    return {'commitment_id': cid, 'consumer_id': consumer, 'source_message_id': message or 'm' + cid[:3], 'revision': 1, 'quote': quote, 'source_available': True,
            'origin': 'claim', 'registered_at': '2026-10-18T01:00:00+00:00', 'events': [], 'exceptions': [], 'evidence': {},
            'properties': {'status': status, 'due_at': due, 'due_precision': 'exact', 'late': late, 'made_to': consumer}}


class Reader:
    def __init__(self, commitments, exceptions, names=None, readable=True, forbidden=()):
        self.data = {'commitments': commitments, 'exceptions': exceptions}
        self.names, self.readable, self.forbidden = names or {}, readable, set(forbidden)
        self.calls, self.message_reads, self.name_reads = [], [], []

    def call(self, verb, **payload):
        self.calls.append((verb, payload))
        assert verb == 'commitments' and payload.get('all') is True  # one governed read of every status
        rows = [v for v in self.data['commitments'] if 'consumer_id' not in payload or v['consumer_id'] == payload['consumer_id']]
        return {'commitments': [dict(v) for v in rows], 'exceptions': [dict(e) for e in self.data['exceptions']]}

    def _readable(self, message_id, purpose):
        self.message_reads.append(message_id)
        return self.readable

    def consumer_fields(self, consumer_id):
        if consumer_id in self.forbidden:
            raise WorkbenchForbidden('consumer fields')
        name = self.names.get(consumer_id)
        if name == 'withheld':
            return [{'name': 'display_name', 'present': None, 'readable': False}]
        return [{'name': 'display_name', 'present': name is not None, 'readable': True}]

    def read_object(self, type_name, object_id, fields, purpose):
        self.name_reads.append(object_id)
        return {'display_name': self.names[object_id]}


ROWS = [view('1' * 64, A, 'open', due='2026-10-20T00:00:00+00:00'), view('2' * 64, A, 'fulfilled'), view('3' * 64, B, 'breached', due='2026-10-19T00:00:00+00:00', late=True),
        view('4' * 64, C, 'cancelled'), view('5' * 64, B, 'in_progress', due='2026-10-25T00:00:00+00:00')]
EXCEPTIONS = [{'subject_ref': 'commitment:' + '3' * 64, 'consumer_id': B, 'reason': 'deadline_breached', 'detail': {}, 'raised_at': '2026-10-19T01:00:00+00:00'},
              {'subject_ref': 'commitment:' + '2' * 64, 'consumer_id': A, 'reason': 'late_fulfilment', 'detail': {}, 'raised_at': '2026-10-18T02:00:00+00:00'},
              {'subject_ref': 'claim:' + 'd' * 64, 'consumer_id': C, 'reason': 'unregistered', 'detail': {}, 'raised_at': '2026-10-18T03:00:00+00:00'}]


def queries(**kw):
    reader = Reader(ROWS, EXCEPTIONS, names={A: '合成客户甲', B: 'withheld'}, **kw)
    return reader, WorkbenchQueries(reader)


def test_list_counts_and_exceptions_share_one_scope():
    """F05: the ended tab shows only ended commitments and only their exceptions; the open tab adds pre-registration ones; counts
    for both tabs and for all exceptions come with every answer."""
    reader, q = queries()
    ended = q.commitments(status_group='ended')
    assert [v['commitment_id'][0] for v in ended['commitments']] == ['2', '4']
    assert [e['reason'] for e in ended['exceptions']] == ['late_fulfilment']
    assert ended['counts'] == {'open': 3, 'ended': 2, 'exceptions_in_scope': 1, 'exceptions_all': 3} and ended['total'] == 2
    opened = q.commitments()
    assert opened['status_group'] == 'open' and [v['commitment_id'][0] for v in opened['commitments']] == ['1', '3', '5']
    assert sorted(e['reason'] for e in opened['exceptions']) == ['deadline_breached', 'unregistered']
    legacy = q.commitments(include_closed=True)  # all=true
    assert legacy['status_group'] == 'all' and legacy['total'] == 5 and len(legacy['exceptions']) == 3
    assert all(c == ('commitments', {'all': True}) for c in reader.calls)


def test_filters_sort_and_page_with_reads_for_the_page_only():
    """F09 / F03: due window, late flag, sort by due time, an offset page; quotes and names read only for the page's rows, each
    customer's name once."""
    reader, q = queries()
    late = q.commitments(late=True)
    assert [v['commitment_id'][0] for v in late['commitments']] == ['3'] and [e['reason'] for e in late['exceptions']] == ['deadline_breached']
    reader.message_reads.clear(); reader.name_reads.clear()
    first = q.commitments(status_group='all', sort='due_at', limit=2)
    assert [v['commitment_id'][0] for v in first['commitments']] == ['3', '1'] and first['next_cursor'] == '2' and first['total'] == 5
    assert reader.message_reads == ['m333', 'm111']  # two rows, two Message READs
    assert reader.name_reads == [A]  # B is owner-restricted: never read
    assert [v['consumer_display'] for v in first['commitments']] == [{'status': 'restricted', 'reason': 'withheld'}, {'status': 'ok', 'name': '合成客户甲'}]
    rest = q.commitments(status_group='all', sort='due_at', limit=2, cursor=first['next_cursor'])
    assert [v['commitment_id'][0] for v in rest['commitments']] == ['5', '2'] and rest['next_cursor'] == '4'
    from datetime import UTC, datetime
    window = q.commitments(due_after=datetime(2026, 10, 19, 12, tzinfo=UTC), due_before=datetime(2026, 10, 21, tzinfo=UTC))
    assert [v['commitment_id'][0] for v in window['commitments']] == ['1']


def test_restricted_quote_and_customer_filter():
    """F03: a promised text the member may not read is withheld and marked; the customer filter goes to the governed read."""
    reader, q = queries(readable=False)
    one = q.commitments(consumer_id=A, status_group='all')
    assert [v['commitment_id'][0] for v in one['commitments']] == ['1', '2'] and reader.calls[-1] == ('commitments', {'all': True, 'consumer_id': A})
    assert all(v['quote'] is None and v['quote_status'] == 'restricted' and 'source_message_id' not in v for v in one['commitments'])


@pytest.mark.parametrize('fields,values,expected', [
    (None, None, {'status': 'restricted', 'reason': 'forbidden'}),
    ([{'name': 'display_name', 'present': None, 'readable': False}], None, {'status': 'restricted', 'reason': 'withheld'}),
    ([{'name': 'display_name', 'present': False, 'readable': True}], None, {'status': 'missing'}),
    ([], None, {'status': 'missing'}),
    ([{'name': 'display_name', 'present': True, 'readable': True}], {'display_name': '  '}, {'status': 'missing'}),
    ([{'name': 'display_name', 'present': True, 'readable': True}], {'display_name': '合成客户乙'}, {'status': 'ok', 'name': '合成客户乙'}),
])
def test_name_state_order(fields, values, expected):
    """F04: failed > restricted > missing > ok; a restriction never says whether a name exists."""
    reader = Reader([], [], forbidden={A} if fields is None else ())
    assert WorkbenchQueries(reader).display_state(A, None if fields is None else fields, values) == expected


def test_name_read_failure_is_failed_not_missing():
    class Broken(Reader):
        def consumer_fields(self, consumer_id):
            raise ConnectionError('database unavailable')
    assert WorkbenchQueries(Broken([], [])).display_state(A) == {'status': 'failed'}
    assert WorkbenchQueries(Reader([], [])).display_state(None) == {'status': 'missing'}



def test_registered_order_is_by_registered_at_with_the_id_as_tie_breaker():
    """PR #9 review: preparation and registration out of step (the read returns prepared_at order); sorting uses registered_at, ties
    by id, unregistered last, so pages do not overlap or skip."""
    def at(cid, registered):
        return {**view(cid, A, 'open'), 'registered_at': registered}
    # Read order (prepared_at): 1, 2, 3, 4, 5; registration order: 3, then 1 and 4 at the same instant, then 5; 2 not yet registered.
    rows = [at('1' * 64, '2026-10-18T02:00:00+00:00'), at('2' * 64, None), at('3' * 64, '2026-10-18T01:00:00+00:00'),
            at('4' * 64, '2026-10-18 02:00:00+00'), at('5' * 64, '2026-10-18T03:00:00.5+00:00')]
    q = WorkbenchQueries(Reader(rows, [], names={A: '合成客户甲'}))
    order = lambda **kw: [v['commitment_id'][0] for v in q.commitments(sort='registered_at', **kw)['commitments']]  # noqa: E731
    assert order() == ['3', '1', '4', '5', '2']
    assert order(order='desc') == ['5', '4', '1', '3', '2']
    pages = [q.commitments(sort='registered_at', limit=2, cursor=c) for c in (None, '2', '4')]
    assert [v['commitment_id'][0] for p in pages for v in p['commitments']] == ['3', '1', '4', '5', '2']


def test_due_before_is_exclusive_on_the_server():
    """PR #9 review: the page sends the next day's midnight; a commitment due at 23:59:59.5 that day is inside, midnight is not."""
    from datetime import UTC, datetime
    rows = [view('1' * 64, A, 'open', due='2026-10-20T15:59:59.5+00:00'), view('2' * 64, A, 'open', due='2026-10-20T16:00:00+00:00')]
    q = WorkbenchQueries(Reader(rows, [], names={A: '合成客户甲'}))
    bound = datetime.fromisoformat('2026-10-21T00:00:00+08:00')  # what the page sends for "到 2026-10-20"
    assert [v['commitment_id'][0] for v in q.commitments(due_before=bound)['commitments']] == ['1']
    assert bound.astimezone(UTC).isoformat() == '2026-10-20T16:00:00+00:00'
