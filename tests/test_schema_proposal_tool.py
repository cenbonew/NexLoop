"""scripts/deploy/schema_proposal.py: the owner's NX-040 Schema proposal review and publication from a private configuration.
Unit part: configuration, the one request per subcommand, the dry run (nothing read from stdin, nothing sent), the password never an
argument or in the output. PG part: through the production workbench app (create_app, real login, the Schema publication routes):
check, --verify-reject (only the check runs), publish (the F4 permit used), and the operator refused."""
import io
import json
import logging
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import schema_proposal as T  # noqa: E402

PID = str(uuid.UUID(int=7, version=4))
SECRET = 'zz-owner-pass-marker'


def private(tmp_path, name, value, mode=0o600):
    path = tmp_path / name
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)
    return path


def cfg(**over):
    return json.dumps({'schema': T.SCHEMA, 'proposal_id': PID, 'rationale': '合成审核', **over})


def test_the_example_configuration_is_valid():
    c = T.load_config('publish', (ROOT / 'deploy/examples/schema-proposal.example.json').read_text())
    assert c['proposal_id'].endswith('000000000000')


@pytest.mark.parametrize('command,over,detail', [('publish', {'rationale': ' '}, 'rationale'), ('check', {'proposal_id': 'x'}, 'uuid'),
                                                 ('migrate-batch', {}, 'publication_id'), ('check', {'tenant_id': 'x'}, 'unknown'),
                                                 ('publish', {'expect_codes': ['Bad Code']}, 'expect_codes'), ('publish', {'batch': 'yes'}, 'batch')])
def test_a_bad_configuration_is_refused(command, over, detail):
    with pytest.raises(T.Refused, match=detail):
        T.load_config(command, cfg(**over))


def test_each_subcommand_sends_one_request_and_verify_reject_only_checks():
    c = T.load_config('publish', cfg(batch=True))
    assert T.request_of('basis', c, False) == ('GET', f'/api/v1/workbench/schema-proposals/{PID}/basis', None)
    assert T.request_of('check', c, False)[1].endswith('/check')
    assert T.request_of('publish', c, False) == ('POST', f'/api/v1/workbench/schema-proposals/{PID}/publish', {'rationale': '合成审核', 'batch': True})
    assert T.request_of('publish', c, True) == ('GET', f'/api/v1/workbench/schema-proposals/{PID}/check', None)  # never a publication
    assert T.request_of('hide', c, False)[2] == {'rationale': '合成审核'}
    assert T.idempotency_key('publish', c) == T.idempotency_key('publish', dict(c)) != T.idempotency_key('hide', c)


def test_check_summary_shows_the_impact_list():
    """NX-061 A1 (owner 2026-10-26: shown, not a gate): the CLI's check summary carries what /check lists, names and versions only."""
    answer = {'proposal_id': PID, 'operation': 'add_vocabulary_value', 'type_name': 'Consumer', 'parent_version': 3, 'next_version': 4,
              'successor_actions': 2, 'compatibility': {'allowed': True, 'change_class': 'compatible', 'codes': []},
              'impact': {'type_name': 'Consumer', 'changed_properties': ['level'], 'affected_by_changed_properties': 1, 'truncated': False,
                         'unreadable': [{'kind': 'functions', 'stable_name': 'consumer.broken'}],
                         'dependents': [{'definition_type': 'object_set', 'stable_name': 'consumer.by_level', 'version': 2, 'pinned_type_version': 3,
                                         'hops': 1, 'changed_properties': ['level']},
                                        {'definition_type': 'action', 'stable_name': 'Consumer.edit', 'version': 1, 'pinned_type_version': 3,
                                         'hops': 2, 'changed_properties': []}]}}
    out = T.summarize('check', answer)
    assert out['allowed'] is True and out['next_version'] == 4
    assert out['impact'] == {'verdict': 'affected', 'verdict_text': '有影响' + T.PARTIAL, 'complete': False,  # one unreadable
                             'type_name': 'Consumer', 'object_types': ['Consumer'],
                             'object_sets': ['consumer.by_level@2'], 'views': [], 'functions': [], 'actions': ['Consumer.edit@1'], 'other': [],
                             'changed_properties': ['level'], 'affected': 2, 'affected_by_changed_properties': 1,
                             'dependents': [{'definition_type': 'object_set', 'stable_name': 'consumer.by_level', 'version': 2,
                                             'pinned_type_version': 3, 'changed_properties': ['level']},
                                            {'definition_type': 'action', 'stable_name': 'Consumer.edit', 'version': 1,
                                             'pinned_type_version': 3, 'changed_properties': []}],
                             'truncated': False, 'unreadable': [{'kind': 'functions', 'stable_name': 'consumer.broken'}]}
    assert 'impact' not in T.summarize('basis', {'operation': 'x', 'schema': {'version': 3}})


EMPTY = {'type_name': 'Store', 'changed_properties': ['opening_hours'], 'affected_by_changed_properties': 0, 'truncated': False,
         'unreadable': [], 'dependents': []}
LISTED = {**EMPTY, 'affected_by_changed_properties': 1, 'dependents': [
    {'definition_type': 'object_view', 'stable_name': 'store.card', 'version': 3, 'pinned_type_version': 1, 'hops': 1, 'changed_properties': ['opening_hours']},
    {'definition_type': 'function', 'stable_name': 'store.score', 'version': 1, 'pinned_type_version': 1, 'hops': 1, 'changed_properties': []},
    {'definition_type': 'interface', 'stable_name': 'Placed', 'version': 2, 'pinned_type_version': 1, 'hops': 2, 'changed_properties': []}]}


ODD = {**LISTED, 'dependents': [*LISTED['dependents'], 'not an entry']}


@pytest.mark.parametrize('impact, verdict, complete, text', [
    (LISTED, 'affected', True, '有影响'),                                                  # the positive control
    (EMPTY, 'no_impact', True, '无影响'),                                                   # empty, and said so
    ({**EMPTY, 'unreadable': [{'kind': 'actions', 'stable_name': 'Store.edit'}]}, 'incomplete', False, '影响不完整'),  # empty, not all read
    ({**EMPTY, 'truncated': True}, 'incomplete', False, '影响不完整'),                       # empty but a kind was cut at 500
    # L6 S2: an answer whose dependents are not a list of entries is malformed, not "no impact"
    ({**EMPTY, 'dependents': ['not an entry']}, 'incomplete', False, '影响不完整'),
    ({**EMPTY, 'dependents': {'definition_type': 'action'}}, 'incomplete', False, '影响不完整'),
    ({k: v for k, v in EMPTY.items() if k != 'dependents'}, 'incomplete', False, '影响不完整'),
    # L6 S1: listed, but not the whole list - affected, and the text says it is incomplete
    ({**LISTED, 'truncated': True}, 'affected', False, '有影响（不完整'),
    ({**LISTED, 'unreadable': [{'kind': 'functions', 'stable_name': 'store.broken'}]}, 'affected', False, '有影响（不完整'),
    (ODD, 'affected', False, '有影响（不完整'),
    (None, 'not_reported', None, '不是无影响'),                                             # an answer without impact
])
def test_the_impact_verdict_is_never_silent(impact, verdict, complete, text):
    shown = T.summarize('check', {'compatibility': {'allowed': True}, 'impact': impact})['impact']
    assert shown['verdict'] == verdict and text in shown['verdict_text'] and shown.get('complete') is complete
    assert ('不完整' in shown['verdict_text']) is (complete is False)
    if verdict == 'not_reported':
        assert set(shown) == {'verdict', 'verdict_text'}  # no empty lists that would read as nothing affected
        return
    kinds = ('object_sets', 'views', 'functions', 'actions', 'other')
    assert shown['affected'] == len(shown['dependents']) == sum(len(shown[k]) for k in kinds)  # every listed entry in one kind
    if verdict == 'affected':
        assert shown['object_types'] == ['Store'] and shown['changed_properties'] == ['opening_hours']
        assert (shown['views'], shown['functions'], shown['other']) == (['store.card@3'], ['store.score@1'], ['interface:Placed@2'])
        assert shown['object_sets'] == shown['actions'] == []
    else:
        assert shown['affected'] == 0 and shown['object_types'] == [] and shown['changed_properties'] == ['opening_hours']


class AnsweringWorkbench:
    """The workbench as the CLI uses it, answering one /check."""
    answer = None

    def __init__(self, transport, origin):
        self.csrf = 'csrf'

    def login(self, username, password):
        pass

    def logout(self):
        pass

    def _call(self, method, path, body, extra):
        return 200, self.answer


CHECKED = {'compatibility': {'allowed': True, 'codes': []}}


@pytest.mark.parametrize('answer, code, verdict', [
    ({**CHECKED, 'impact': EMPTY}, 0, 'no_impact'),            # the positive controls: a complete answer, empty or not
    ({**CHECKED, 'impact': LISTED}, 0, 'affected'),
    ({**CHECKED, 'impact': {**LISTED, 'truncated': True}}, 0, 'affected'),  # shown as incomplete, still an answer
    ({**CHECKED, 'impact': None}, 1, 'not_reported'),
    # L6 F1: a 200 answer without compatibility never reached impact_of - check passed with no impact at all
    ({'impact': None}, 1, 'not_reported'),
    ({}, 1, 'not_reported'),
    ({'allowed': True}, 1, 'not_reported'),
    ({'impact': EMPTY}, 0, 'no_impact'),                       # no compatibility: the impact is still read, not dropped
    ([], 1, 'not_reported'),                                   # not even an object
    ({**CHECKED, 'impact': 'none'}, 1, 'not_reported'),
])
def test_check_exits_1_when_the_answer_carries_no_impact(tmp_path, capsys, monkeypatch, answer, code, verdict):
    monkeypatch.setattr(T, 'Workbench', AnsweringWorkbench)
    monkeypatch.setattr(AnsweringWorkbench, 'answer', answer)
    conf, user = private(tmp_path, 'c.json', cfg()), private(tmp_path, 'u', 'owner\n')
    argv = ['check', '--config', str(conf), '--origin', 'https://app.example.invalid', '--username-file', str(user), '--password-stdin']
    assert T.main(argv, stdin=io.StringIO(SECRET + '\n'), transport=NoNetwork()) == code
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is (code == 0) and out['result']['impact']['verdict'] == verdict
    # --verify-reject keeps its own exit rule (refused or not: none of these is refused); the impact rides along
    assert T.main([*argv, '--verify-reject'], stdin=io.StringIO(SECRET + '\n'), transport=NoNetwork()) == 1
    assert json.loads(capsys.readouterr().out)['result']['impact']['verdict'] == verdict


def test_a_check_result_with_no_verdict_is_not_ok(tmp_path, capsys, monkeypatch):
    """The exit rule itself (L6 F1): a check result whose impact carries no known verdict fails, whatever summarize made of it."""
    monkeypatch.setattr(T, 'Workbench', AnsweringWorkbench)
    monkeypatch.setattr(AnsweringWorkbench, 'answer', {**CHECKED, 'impact': EMPTY})
    conf, user = private(tmp_path, 'c.json', cfg()), private(tmp_path, 'u', 'owner\n')
    argv = ['check', '--config', str(conf), '--origin', 'https://app.example.invalid', '--username-file', str(user), '--password-stdin']
    for shown in ({}, {'impact': None}, {'impact': {}}, {'impact': {'verdict': 'unknown'}}):
        monkeypatch.setattr(T, 'summarize', lambda command, value, shown=shown: dict(shown))
        assert T.main(argv, stdin=io.StringIO(SECRET + '\n'), transport=NoNetwork()) == 1
        assert json.loads(capsys.readouterr().out)['ok'] is False
    monkeypatch.setattr(T, 'summarize', lambda command, value: {'impact': {'verdict': 'no_impact'}})  # the positive control
    assert T.main(argv, stdin=io.StringIO(SECRET + '\n'), transport=NoNetwork()) == 0


class NoNetwork:
    def request(self, *unused):
        raise AssertionError('no request')


class NoStdin:
    def readline(self):
        raise AssertionError('no password read')


def test_dry_run_and_password_rules(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    conf, user = private(tmp_path, 'c.json', cfg()), private(tmp_path, 'u', 'owner\n')
    assert T.main(['publish', '--config', str(conf), '--dry-run'], stdin=NoStdin(), transport=NoNetwork()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] and out['method'] == 'POST'
    base = ['publish', '--config', str(conf), '--origin', 'https://app.example.invalid', '--username-file', str(user)]
    for argv in ([*base, '--password', SECRET], [*base, SECRET], base, ['basis', '--config', str(conf), '--verify-reject', '--dry-run']):
        assert T.main(argv, stdin=NoStdin(), transport=NoNetwork()) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + caplog.text


class AppTransport:
    def __init__(self, client):
        self.client, self.requests = client, []

    def request(self, method, path, headers, body=None):
        self.requests.append((method, path))
        response = self.client.request(method, path, headers=headers, content=body)
        return response.status_code, list(response.headers.multi_items()), response.content


# PG part --------------------------------------------------------------------------------------------------------------------------
from psycopg.types.json import Jsonb  # noqa: E402
from nexloop_eios.workbench_roles import compile_members  # noqa: E402
from test_browser_http import ORIGIN  # noqa: E402
from test_nx040_schema_publication_pg import proposal  # noqa: E402
from test_workbench_read_pg import ROLES, members_file, workbench, workbench_login, write_facts, configure  # noqa: E402,F401
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, published_action, uow  # noqa: E402,F401


def test_check_verify_reject_and_publish_end_to_end(workbench, tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    w = workbench
    admin = w['admin']
    members = ((w['owner'], 'owner'), (w['operator'], 'operator'))
    configure(w, *members)
    write_facts(admin, compile_members(ROLES, members_file(*members)))
    f = {'admin': admin, 'tenant': TENANT}
    good = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    bad = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'raw_blob', 'value_type': 'json'})
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Store',1,%s)",
                  (TENANT, Jsonb({'type_name': 'Store', 'version': 1, 'properties': [{'property_name': 'city', 'value_type': 'string'}],
                                  'property_groups': [{'group_name': 'place', 'property_names': ['city']}]})))
    lonely = proposal(f, 'add_optional_property', 'Store', {'property_name': 'opening_hours', 'value_type': 'string'})

    def tool(member, command, conf, *extra):
        path = private(tmp_path, f'c-{uuid.uuid4().hex[:6]}.json', conf)
        user = private(tmp_path, 'user-' + member['username'], member['username'] + '\n')
        with w['client']() as client:
            transport = AppTransport(client)
            code = T.main([command, '--config', str(path), '--origin', ORIGIN, '--username-file', str(user), '--password-stdin', *extra],
                          stdin=io.StringIO(member['password'] + '\n'), transport=transport)
        captured = capsys.readouterr()
        for secret in (member['password'], member['username']):
            assert secret not in captured.out + captured.err + caplog.text
        return code, json.loads(captured.out), transport.requests

    code, out, _ = tool(w['owner'], 'check', cfg(proposal_id=good))
    assert code == 0 and out['result']['allowed'] is True and out['result']['next_version'] == out['result']['parent_version'] + 1
    # NX-061 A1: the impact list of the actual /check, as the CLI shows it (the fixture's Consumer Actions depend on the type).
    shown = out['result']['impact']
    assert shown['type_name'] == 'Consumer' and shown['changed_properties'] == ['preferred_store'] and shown['affected'] == len(shown['dependents']) >= 1
    assert all(set(e) == set(T.IMPACT_ENTRY) for e in shown['dependents'])
    # The positive control: affected, the fixture's Consumer Actions listed by kind - the same entries, in the same order, as the
    # API's own /check answer.
    assert shown['verdict'] == 'affected' and shown['complete'] is True and shown['object_types'] == ['Consumer'] and shown['actions']
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        api = client.get(f'/api/v1/workbench/schema-proposals/{good}/check').json()['impact']
    assert [{k: e[k] for k in T.IMPACT_ENTRY} for e in api['dependents']] == shown['dependents']
    for kind, name in T.IMPACT_KINDS:
        assert shown[name] == [f"{e['stable_name']}@{e['version']}" for e in api['dependents'] if e['definition_type'] == kind]
    assert (shown['changed_properties'], shown['affected_by_changed_properties'], shown['truncated'], shown['unreadable']) == (
        api['changed_properties'], api['affected_by_changed_properties'], api['truncated'], api['unreadable'])
    # A type no published Definition depends on: an empty list, said to be no impact (not an empty list passed over in silence).
    code, out, _ = tool(w['owner'], 'check', cfg(proposal_id=lonely))
    shown = out['result']['impact']
    assert code == 0 and out['ok'] is True and shown['verdict'] == 'no_impact' and shown['verdict_text'] == '无影响' and shown['complete'] is True
    assert shown['type_name'] == 'Store' and shown['affected'] == 0 and shown['dependents'] == shown['actions'] == shown['views'] == []
    # --verify-reject: the refused change, with its expected code; only the check runs.
    code, out, requests = tool(w['owner'], 'publish', cfg(proposal_id=bad, expect_codes=['value_type_not_allowed']), '--verify-reject')
    assert code == 0 and out['rejected'] is True and 'value_type_not_allowed' in out['result']['codes']
    assert not any(m == 'POST' and 'schema-proposals' in p for m, p in requests)
    code, out, _ = tool(w['owner'], 'publish', cfg(proposal_id=good), '--verify-reject')  # an allowed change is not a refusal
    assert code == 1 and out['rejected'] is False
    # The operator: 403.
    code, out, _ = tool(w['operator'], 'publish', cfg(proposal_id=good))
    assert code == 1 and out['status'] == 403 and out['code'] == 'forbidden'
    # The owner publishes; the F4 permit is used.
    code, out, _ = tool(w['owner'], 'publish', cfg(proposal_id=good))
    assert code == 0 and out['result']['published'] is True and out['result']['permit'].startswith('permit:')
    assert admin.execute('select status from ontology.nexloop_schema_proposals where proposal_id=%s', (good,)).fetchone()[0] == 'published'
    code, out, _ = tool(w['owner'], 'publish', cfg(proposal_id=bad))
    assert code == 1 and out['result']['published'] is False and 'value_type_not_allowed' in out['result']['codes']
