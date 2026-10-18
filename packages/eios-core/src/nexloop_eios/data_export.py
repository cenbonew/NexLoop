"""NX-029 slice 3: governed export of one Consumer for the owner who requested it (design §6; migration 0141; AT-003).

``Consumer.export`` (governed entry, human only) records the request. ``DataExporter`` then assembles the export with
the requester's own workbench session and only through the requester's own governed reads (ADR-025 member reads:
owner-restricted property groups and Messages the member's role does not derive are withheld and only counted, never
copied in by the server; each read leaves its read-audit row with purpose 'export'). Commercial records are included
only when the requester holds the observe Action. The export is not stored; each download is an audit event (counts
and payload digest, no content).
"""
import hashlib
import hmac
from datetime import UTC,datetime

from nexloop_eios.postgres_artifacts import canonical_payload

EXPORT_ACTION='nexloop.consumer.export'
PROTOCOL='nexloop-data-export-v1'
MAX_MESSAGES=2000


class ExportUnavailable(PermissionError):
    pass


class DataExporter:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def _call(self,payload):
        from nexloop_eios.action_definitions import PostgresActionDefinitionReader
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        body=canonical_payload(payload)
        claims=dict(action_claims(self.pool,self.session,EXPORT_ACTION))
        claims.update(protocol=PROTOCOL,key_id=self.signer.key_id,parameters_digest=hashlib.sha256(body.encode()).hexdigest())
        definition,_=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(EXPORT_ACTION,1)
        claims['definition']=definition.model_dump(mode='json')
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_data_export(%s,%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]

    def export(self,export_id):
        """The export as a dict; raises ExportUnavailable when the request is not this requester's or not open."""
        from nexloop_eios.workbench_reads import WorkbenchForbidden,WorkbenchQueries,WorkbenchReader
        try:opened=self._call({'verb':'open','export_id':str(export_id)})
        except Exception as error:raise ExportUnavailable('data export unavailable') from error
        consumer=opened['consumer_id'];reader=WorkbenchReader(self.pool,self.session,self.signer)
        withheld={'properties':0,'messages':0,'commercial':0}
        try:
            fields=reader.consumer_fields(consumer) or []
        except WorkbenchForbidden:
            fields=[]
        readable=[f['name'] for f in fields if f['readable'] and f['present']]
        # Owner decision 2026-10-18 (D2 export exception): the number of recorded properties withheld comes from the export's own
        # path (0218 `withheld_present`); the workbench field list no longer says whether a restricted property is recorded.
        withheld['properties']=int(opened['withheld_present'])
        properties=(reader.read_object('Consumer',consumer,readable,'export') or {}) if readable else {}
        if readable and not properties:withheld['properties']+=len(readable)
        messages=[]
        for message_id in opened['message_ids'][:MAX_MESSAGES]:
            value=reader.message_fields(message_id,('actor','body'),'export')
            if value is None:
                withheld['messages']+=1;continue
            messages.append({'message_id':message_id,'actor':value.get('actor'),'body':value.get('body')})
        commercial=[]
        try:
            from nexloop_eios.commercial import CommercialObserver
            commercial=CommercialObserver(self.pool,self.session,self.signer).records(consumer)['records']
        except Exception:
            withheld['commercial']=1
        export={'schema':'nexloop-consumer-export/1','export_id':str(export_id),'world':opened['world'],'consumer':{'id':consumer,'properties':properties},
            'messages':messages,'commercial_records':commercial,'withheld':withheld,'generated_at':datetime.now(UTC).isoformat()}
        digest=hashlib.sha256(canonical_payload(export).encode()).hexdigest()
        self._call({'verb':'record','export_id':str(export_id),'payload_digest':digest,
            'counts':{'properties':len(properties),'messages':len(messages),'commercial_records':len(commercial),**{'withheld_'+k:v for k,v in withheld.items()}}})
        return export


__all__=['DataExporter','ExportUnavailable']
