"""NX-026 end to end over the actual NX-047 chain (synthetic data, real Host/Pi with the deterministic protocol).

Human HTTPS Message → relay → Pi Run → governed reply intent → real local JSON delivery → governed Message.agent_create
→ same conversation stream → extraction of the enterprise commitment (deterministic provider) → the commitment keeper
(credential, grants, lifecycle property rule and the owner's Commitment decision from the versioned deploy manifests,
applied through trusted configuration) registers it as a governed Commitment. Delivery never fulfils it (AT-040). The
runtime guard runs as four processes (deployment shape, ADR-022 §4 / ADR-024).
"""
import copy,hashlib,json,secrets,uuid
from datetime import UTC,datetime,timedelta
from pathlib import Path

from psycopg.conninfo import make_conninfo
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.commitments import CommitmentKeeper,CommitmentReadPort,load_settings
from nexloop_eios.trusted_configuration import apply_manifest
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_outbound_messages_pg import REPLY,chain,delivery_material,delivery_service,effect_worker,outbound

ROOT=Path(__file__).resolve().parents[1]


def publish_keeper(f,admin):
    """Commitment type, Commitment Actions, keeper principal, its lifecycle rule and the owner's Commitment decision:
    all from the versioned manifests, applied through trusted configuration (ADR-020 §3)."""
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    from nexloop_eios import business_actions,business_object_types,service_grants
    o=f['original'];tenant=o['tenant'];manifest=copy.deepcopy(f['manifest'])
    types=business_object_types.trusted_object_types(ROOT/'deploy/ontology/business-object-types.v1.json')
    manifest['object_types']+=types
    base=CapabilityContractSnapshot.model_validate_json(json.dumps(next(a for a in manifest['actions'] if a['definition']['stable_name']=='Message.agent_create')['capability']))
    rows=business_actions.compile_actions(business_actions.load(ROOT/'deploy/configuration/business-actions.v1.json'),tenant=tenant,
        created_by='explicit-assembly-owner',created_at=datetime.now(UTC),object_types=types,
        capabilities={'ontology.object.create':base,'ontology.object.edit':base.model_copy(update={'capability_name':'ontology.object.edit'})},
        select=('Commitment.create','Commitment.edit'))
    manifest['actions']+=rows
    grants=service_grants.load(ROOT/'deploy/authorization/service-grants.v1.json')
    principal=next(p for p in grants['principals'] if p['role']=='commitment_keeper')
    end=datetime.now(UTC)+timedelta(minutes=30)
    binding,compiled=service_grants.compile_principal(grants,tenant,principal,credential_expires_at=end)
    compiled+=[r for r in service_grants.compile_property_rules(grants,tenant) if r[1][0]==principal['principal_id']]
    owner=service_grants.load_owner_restrictions(ROOT/'deploy/authorization/owner-property-restrictions.json')
    compiled+=[r for r in service_grants.compile_owner_restrictions(owner,tenant) if r[1]==['Commitment']]
    facts={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']}
    for kind,key,fact in compiled:
        payload=fact.model_dump(mode='json');payload.pop('snapshot_digest',None)
        facts[(kind,tuple(key))]={'kind':kind,'key':key,'payload':payload}
    token=secrets.token_urlsafe(48);secret_map=json.loads(o['paths']['secrets'].read_text());secret_map[principal['credential_reference']]=token
    o['paths']['secrets'].write_text(json.dumps(secret_map))
    manifest.update(manifest_id=str(uuid.uuid4()),authority_facts=list(facts.values()),
        service_credentials=manifest['service_credentials']+[{'reference':principal['credential_reference'],'binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':end.isoformat(),'status':'active'}],
        expected_revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(tenant,)).fetchone()[0])
    apply_manifest(manifest,database_url_file=o['paths']['dsn'],signing_key_file=o['paths']['signing'],signing_key_id='explicit-configuration',service_secrets_file=o['paths']['secrets'])
    f['manifest']=manifest
    return token


def test_delivered_agent_promise_becomes_a_governed_commitment_and_delivery_never_fulfils_it(assembled_message,admin,tmp_path):
    f=assembled_message
    with chain(f,admin,tmp_path,guard_workers=4) as c:
        tenant=c['tenant'];recorder=c['recorder'];conversation_id=c['conversation_id']
        assert 'succeeded' in c['runtime_output']
        ((intent,state,*_),)=outbound(admin,tenant);assert state=='persisted'
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        (created,)=recorder.run_once();agent_message=created['message']
        assert agent_message['body']==REPLY and agent_message['direction']=='outbound'
        # Extraction of the enterprise commitment from the delivered words (deterministic provider keyed by the window).
        from multi_authority_fixture import seed_multi_authority
        from nexloop_eios.claim_store import ConversationClaimExtractor
        from nexloop_eios.conversation_extraction import DeterministicExtractionProvider,build_user_payload
        from test_claim_store_pg import source_targets
        ids=[r[0] for r in admin.execute('select message_id from runtime.nexloop_conversation_messages where tenant_id=%s order by sequence',(tenant,)).fetchall()]
        source=c['backend'].authenticate(c['tokens']['assembly-source'],world='real');sp,sg=source._backend._pool,source._backend._signer
        session,_=seed_multi_authority(admin,sp,source_targets(conversation_id,ids),identity_suffix='-nx026-extractor',tenant=tenant)
        extractor=ConversationClaimExtractor(sp,session,sg,None,timezone='Asia/Shanghai')
        ctx,window=extractor.load_window(conversation_id,ids)
        agent_ref=next(m.sequence for m in window if m.speaker=='agent')
        response={'topics':[{'topic':'处理进展','conversation_summary':'企业承诺明天下午前反馈','user_valid_reply':True,'message_refs':[m.sequence for m in window]}],
            'claims':[{'topic_index':0,'message_ref':agent_ref,'quote':'我会在明天下午前给你处理进展','kind':'commitment','subject':{'kind':'enterprise','text':''},
                       'predicate':'处理进展反馈','value':{'type':'string','value':'给出处理进展'}}]}
        extractor.provider=DeterministicExtractionProvider({hashlib.sha256(build_user_payload(window,ctx).encode()).hexdigest():response})
        extractor.extract(conversation_id=conversation_id,message_ids=ids)
        (claim_id,window_end),=admin.execute("select claim_id,valid_time->'latest_bound_window'->>1 from ontology.nexloop_claims where tenant_id=%s and epistemic_kind='commitment'",(tenant,)).fetchall()
        # The keeper from the deployed manifests.
        token=publish_keeper(f,admin)
        settings=load_settings(ROOT/'deploy/configuration/commitments.v1.json')
        with open_core(make_conninfo(c['o']['pg'],user='nexloop_domain_worker')) as worker:
            keeper=lambda:CommitmentKeeper(worker,authenticate_service(worker,token,world='real'),sg,settings=settings)
            first=keeper().run_once();second=keeper().run_once()
            assert first['registered']==1 and first['settled']>=0,first
            (commitment,)=admin.execute('select commitment_id from runtime.nexloop_commitments where tenant_id=%s and claim_id=%s',(tenant,claim_id)).fetchone()
            view=CommitmentReadPort(worker,authenticate_service(worker,token,world='real'),sg).commitment(commitment)
        p=view['properties']
        assert p['made_to']==c['consumer']['id'] or p['made_to']==admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s',(conversation_id,)).fetchone()[0]
        assert p['status']=='open' and p['due_precision']=='latest_bound' and datetime.fromisoformat(p['due_at'])==datetime.fromisoformat(window_end)
        assert p['made_by']['intent_id']==intent and p['content_ref']['message_id']==agent_message['id'] and view['quote']=='我会在明天下午前给你处理进展'
        assert admin.execute('select resolution_state from ontology.nexloop_claims where claim_id=%s',(claim_id,)).fetchone()==('resolved',)
        # AT-040: the reply was delivered; nothing is fulfilled or resolved by that.
        assert p['fulfillment_evidence']==[] and view['evidence']['delivered']==[] and view['evidence']['problem_resolved']=='unavailable'
        # The next stage is scheduled at due_at - lead_seconds (NX-024 T7 mark before the due date).
        (available,)=admin.execute("select available_at from runtime.nexloop_work_feed where feed='commitment-monitor' and item_key=%s",('commitment:'+commitment,)).fetchone()
        assert abs((available-(datetime.fromisoformat(window_end)-timedelta(seconds=settings['lead_seconds']))).total_seconds())<1
