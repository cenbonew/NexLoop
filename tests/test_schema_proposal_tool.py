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
    assert out['impact'] == {'type_name': 'Consumer', 'changed_properties': ['level'], 'affected': 2, 'affected_by_changed_properties': 1,
                             'dependents': [{'definition_type': 'object_set', 'stable_name': 'consumer.by_level', 'version': 2,
                                             'pinned_type_version': 3, 'changed_properties': ['level']},
                                            {'definition_type': 'action', 'stable_name': 'Consumer.edit', 'version': 1,
                                             'pinned_type_version': 3, 'changed_properties': []}],
                             'truncated': False, 'unreadable': [{'kind': 'functions', 'stable_name': 'consumer.broken'}]}
    assert T.summarize('check', {**answer, 'impact': None})['impact'] is None      # an older API without impact: shown as absent
    assert 'impact' not in T.summarize('basis', {'operation': 'x', 'schema': {'version': 3}})


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
