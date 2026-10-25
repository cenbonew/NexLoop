"""NX-057 G1 (migration remediation_rule_publications; run sheet docs/release/run-nx057-remediation-stage.md §3): the remediation rules file
is published from its exact text through scripts/deploy/remediation_rules_apply.py and the real technical configurator; SQL records the
file's sha256 and the expected revision, refuses another current revision and another text of a published version, and the configurator
can no longer configure rules without that record. The window is replaced by the configurator DSN of the fixture (the window itself is
tests/test_configurator_window_pg.py)."""
import contextlib
import hashlib
import importlib.util
import io
import json
import secrets

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.trusted_configuration import configurator_connection
from test_observability_pg import TENANT, observed  # noqa: F401
from test_remediation_pg import ROOT, manifest, remediating, rule  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

V1 = ROOT / 'deploy/configuration/remediation-rules.v1.json'
spec = importlib.util.spec_from_file_location('remediation_rules_apply', ROOT / 'scripts/deploy/remediation_rules_apply.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(configurator, *args):
    @contextlib.contextmanager
    def window(path, refused):
        yield configurator
    out = io.StringIO()
    code = cli.main(['--manifest', str(V1), '--tenant', TENANT, '--bootstrap-dsn-file', 'unused', *args], out=out, window=window)
    return code, json.loads(out.getvalue())


def publications(admin):
    return admin.execute("""select manifest_version, manifest_sha256, expected_revision, published_by from control.nexloop_remediation_rule_publications
        where tenant_id=%s order by manifest_version""", (TENANT,)).fetchall()


def test_the_file_is_published_once_with_its_sha256_and_expected_revision(remediating):
    r, admin, configurator = remediating, remediating['admin'], remediating['w']['configurator']
    # deliver_final_failure's source: the tenant declared nexloop.enterprise.deliver:1 external (effect categories v3), seeded as admin.
    admin.execute("""insert into control.nexloop_action_effect_externality(tenant_id,world,resource_id,definition_digest,external_effect,manifest_version)
        values(%s,'real','eios:action:nexloop.enterprise.deliver:1',%s,true,3)""", (TENANT, secrets.token_hex(32)))
    digest = hashlib.sha256(V1.read_bytes()).hexdigest()
    code, out = run(configurator, '--expected-revision', '1')
    assert code == 2 and out == {'applied': False, 'reason': 'SerializationFailure: remediation rules: expected revision 1, current 0'}
    assert publications(admin) == [] and admin.execute('select count(*) from control.nexloop_remediation_rules').fetchone()[0] == 0
    code, out = run(configurator, '--expected-revision', '0')
    assert code == 0 and out['applied'] is True and out['manifest_sha256'] == digest and out['expected_revision'] == 0
    assert out['result'] == {'configured': True, 'manifest_version': 1, 'replay': False, 'rules': 1, 'manifest_sha256': digest, 'expected_revision': 0}
    assert publications(admin) == [(1, digest, 0, 'nexloop_configurator')]
    stored = admin.execute("select manifest from control.nexloop_remediation_rules where tenant_id=%s", (TENANT,)).fetchone()[0]
    assert stored == json.loads(V1.read_text())
    # The same file again is a replay, whatever revision it names (the run sheet's second run); nothing new is written.
    for expected in ('0', '1'):
        code, out = run(configurator, '--expected-revision', expected)
        assert code == 0 and out['result']['replay'] is True and out['result']['expected_revision'] == 0
    assert len(publications(admin)) == 1
    # Version 1 with another text: refused, the first publication stays.
    with pytest.raises(psycopg.errors.SerializationFailure, match='version 1 published with another text'):
        r['rules'](manifest(rule()))
    assert publications(admin) == [(1, digest, 0, 'nexloop_configurator')]


def test_only_the_recording_entry_publishes_and_the_record_is_append_only(remediating):
    r, admin, configurator = remediating, remediating['admin'], remediating['w']['configurator']
    text = json.dumps(manifest(rule()))
    with configurator_connection(configurator) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):        # the configurator no longer configures without the record
            db.execute("select control.nexloop_configure_remediation_rules(%s,'real',%s)", (TENANT, Jsonb(json.loads(text))))
    with configurator_connection(configurator) as db:
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='remediation rules: invalid'):   # the sha256 must be the text's
            db.execute("select control.nexloop_publish_remediation_rules(%s,'real',%s,%s,0)", (TENANT, text, 'a' * 64))
    with configurator_connection(configurator) as db:
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='remediation rules: invalid'):   # no expected revision
            db.execute("select control.nexloop_publish_remediation_rules(%s,'real',%s,%s,null)", (TENANT, text, hashlib.sha256(text.encode()).hexdigest()))
    with pytest.raises(psycopg.Error, match='trusted configuration only'):   # any other session
        admin.execute("select control.nexloop_publish_remediation_rules(%s,'real',%s,%s,0)", (TENANT, text, hashlib.sha256(text.encode()).hexdigest()))
    assert publications(admin) == []
    assert r['rules'](manifest(rule()))['replay'] is False
    assert [row[0] for row in publications(admin)] == [1]
    for statement in ('delete from control.nexloop_remediation_rule_publications', "update control.nexloop_remediation_rule_publications set expected_revision=0"):
        with pytest.raises(psycopg.Error), admin.transaction():
            admin.execute(statement)
