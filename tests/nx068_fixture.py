"""NX-068 test helper: register's follow marker (service_grants.FOLLOW_MARKER) on fixture grant facts that register did not write."""
import copy
import json

from psycopg.types.json import Jsonb

from goal_fixture import MODELS
from nexloop_eios.service_grants import FOLLOW_MARKER


def mark(admin, tenant, name):
    """The marker on every service grant fact of the Action `name`, any version (what register writes for a FOLLOWABLE declaration)."""
    for key, payload in admin.execute("""select entity_key,payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'
            and payload->>'subject_kind'='service' and regexp_replace(entity_key[2],':[0-9]+$','')=%s""", (tenant, 'eios:action:' + name)).fetchall():
        body = copy.deepcopy(payload)
        body.pop('snapshot_digest', None)
        for g in body['grants']:
            g['grant_id'] = g['grant_id'] + FOLLOW_MARKER
        fact = MODELS['grants'].model_validate_json(json.dumps(body))
        admin.execute('update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind=%s and entity_key=%s',
                      (Jsonb(fact.model_dump(mode='json')), tenant, 'grants', key))
