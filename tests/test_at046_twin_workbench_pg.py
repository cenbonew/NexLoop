"""AT-046 synthetic twin through the production workbench (stage finding 2026-10-22: publish_consumer_twin answered 422). On PostgreSQL:
the actual workbench app (create_app, real login realm), the owner's governed Actions publish_consumer_twin and retire_consumer_twin,
driven by at046_stage.py's publish-twin and cleanup phases - the request validation that refused the stage body runs here."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import at046_stage as A  # noqa: E402
from real_dispatch_switch import Workbench  # noqa: E402

from test_real_dispatch_switch_pg import switching  # noqa: E402,F401
from commercial_fixture import publish_registry_actions  # noqa: E402
from commitment_fixture import TENANT, commitments  # noqa: E402,F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_browser_identity_reads import identity  # noqa: E402,F401
from test_browser_session_creation import uow  # noqa: E402,F401
from test_browser_http import ORIGIN  # noqa: E402
from test_twin_calibrate_tool import AppTransport  # noqa: E402


def phase(tmp_path, client, member, name, **inputs):
    owner = Workbench(AppTransport(client), ORIGIN)
    owner.login(member['username'], member['password'])
    conf = tmp_path / f'{name}.json'
    conf.write_text(json.dumps({'schema': 'nexloop-s6-stage/1', 'executor': 'L2 local', 'release': 'local', 'tenant_id': TENANT,
                                'results_dir': str(tmp_path / 'results'), 'inputs': inputs}))
    conf.chmod(0o600)
    out = io.StringIO()
    try:
        code = A.main([name, '--config', str(conf)], ports=A.Ports(owner=owner), out=out)
    finally:
        owner.logout()
    return code, json.loads(out.getvalue())


def test_the_synthetic_twin_is_published_and_retired_through_the_workbench(switching, admin, tmp_path):
    from fastapi.testclient import TestClient
    from nexloop_eios.http_api import create_app
    c = switching
    publish_registry_actions(admin, ('nexloop.consumer_twin.publish', 'nexloop.consumer_twin.retire'))
    owner, operator = c['owner_member'], c['operator_member']
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        # The stage failure, reproduced: the body of 0584f0a4 and before (definition only) is refused by the entry's validation.
        old = Workbench(AppTransport(client), ORIGIN)
        old.login(owner['username'], owner['password'])
        definition = A.twin_definition({'publish_twin': {'twin_id': 'at046-synthetic-stage'}})
        status, body = old._call('POST', '/api/v1/workbench/actions/publish_consumer_twin', {'definition': definition},
                                 {'X-CSRF-Token': old.csrf, 'Idempotency-Key': 'at046-twin-' + 'b' * 32})
        old.logout()
        assert status == 422 and body['code'] == 'invalid_request'
        code, doc = phase(tmp_path, client, owner, 'publish-twin', publish_twin={'twin_id': 'at046-synthetic-stage'})
        step = next(s for s in doc['steps'] if s['name'] == 'twin_published')
        assert code == 0 and step['observed']['status'] == 200 and step['observed']['twin_status'] == 'published', doc
        twin = {'twin_id': step['observed']['twin_id'], 'version': step['observed']['version']}
        assert twin == {'twin_id': 'at046-synthetic-stage', 'version': 1}
        # A rerun replays the same request: no second version.
        code, again = phase(tmp_path, client, owner, 'publish-twin', publish_twin={'twin_id': 'at046-synthetic-stage'})
        replay = next(s for s in again['steps'] if s['name'] == 'twin_published')['observed']
        assert code == 0 and replay['replayed'] is True and replay['twin_id'] == 'at046-synthetic-stage' and replay['version'] is None, again['steps']
        # No world was created by publish-twin.
        admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        assert admin.execute('select count(*) from control.nexloop_sim_worlds where tenant_id=%s', (TENANT,)).fetchone()[0] == 0
        assert admin.execute("select count(*) from control.nexloop_consumer_twins where tenant_id=%s and twin_id='at046-synthetic-stage'", (TENANT,)).fetchone()[0] == 1
        # An operator may not publish it (owner only).
        code, refused = phase(tmp_path, client, operator, 'publish-twin', publish_twin={'twin_id': 'at046-synthetic-stage'})
        assert code == 1 and next(s for s in refused['steps'] if s['name'] == 'twin_published')['observed']['status'] == 403
        # cleanup retires it through the same entry.
        code, done = phase(tmp_path, client, owner, 'cleanup', twin=twin)
        assert code == 0 and next(s for s in done['steps'] if s['name'] == 'synthetic_twin_retired')['passed'] is True, done
    assert admin.execute('select runtime.nexloop_consumer_twin_status(%s,%s,%s)', (TENANT, 'at046-synthetic-stage', 1)).fetchone()[0] == 'retired'
