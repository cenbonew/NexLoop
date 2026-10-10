"""NX-028 slice 1 (D8): the commitment-view contract holds for a real registered NX-026 commitment.

The commitment comes from the actual keeper registration (commitment_fixture). The projection is built exactly as the
0141 `commitment` verb builds it (runtime.nexloop_commitment_view plus consumer_id/source_message_id), then filtered by
WorkbenchQueries for the promised words: withheld without the source Message's READ, shown with it (AT-003).
"""
import json
from pathlib import Path

import jsonschema
from commitment_fixture import TENANT, commitments, deadline  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_commitments_pg import QUOTE, fresh
from nexloop_eios.workbench_reads import WorkbenchQueries

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/commitment-view.schema.json').read_text())


class _Reader:
    def __init__(self, readable):
        self.readable = readable

    def _readable(self, message_id):
        return self.readable and message_id is not None


def projection(admin, commitment_id):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        return admin.execute("""select runtime.nexloop_commitment_view(r.tenant_id,r.world,r.commitment_id)
            ||jsonb_build_object('consumer_id',r.consumer_id,'source_message_id',r.message_id)
            from runtime.nexloop_commitments r where r.tenant_id=%s and r.world='real' and r.commitment_id=%s""", (TENANT, commitment_id)).fetchone()[0]


def test_registered_commitment_matches_contract_and_quote_needs_message_read(commitments, admin):
    commitment, message = fresh(commitments)
    restricted = WorkbenchQueries(_Reader(False))._commitment(projection(admin, commitment))
    jsonschema.validate(restricted, SCHEMA)
    assert restricted['quote'] is None and restricted['quote_status'] == 'restricted' and 'source_message_id' not in restricted
    assert restricted['properties']['status'] in ('open', 'in_progress') and restricted['evidence']['problem_resolved'] == 'unavailable'
    # Delivery of the promising message is not delivery evidence of the promise (AT-040): nothing in "delivered" yet.
    assert restricted['evidence']['delivered'] == []
    shown = WorkbenchQueries(_Reader(True))._commitment(projection(admin, commitment))
    jsonschema.validate(shown, SCHEMA)
    assert shown['quote'] == QUOTE and shown['quote_status'] == 'ok'
