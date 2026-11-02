-- Contact channel re-extraction windows (plan docs/implementation/claim-to-consumer-property-plan.md §8.4, §10 item 4). The migration number
-- lives only in the file name and the catalog; nothing below repeats it.
-- authz.nexloop_contact_channel_reextract (eios:action:nexloop.claim.reextract_contact:1, protocol nexloop-contact-channel-reextract-v1): the
-- claim extraction scheduler service asks which Conversations hold consumer channel statements that the 0228 vocabulary cannot read (a
-- preference or constraint Claim whose predicate gives no contact dimension, and whose predicate or quote names a contact channel), and gets
-- one extraction window per Conversation (the source Message ids, oldest first, at most 50) to enqueue under a re-extraction event id. The
-- extractor then records new Claims under its new version; the old Claims stay as evidence. Tenant from the service credential, world from its
-- binding, caller role nexloop_api only (authz.nexloop_plan_port_tenant). Skipped: Consumers with any erasure request, persona simulated_agent
-- or synthetic, Claims already recorded by the named extractor version or later re-extracted, superseded or erased Claims. It reads only
-- predicate, quote, kind, speaker and source Message of those Claims to choose windows; it returns Message ids and counts, never a Claim value
-- or quote, and writes nothing.

create function authz.nexloop_contact_channel_reextract(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;c jsonb:=p_payload::jsonb;v_version text;windows jsonb;n jsonb;
begin
 t:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-contact-channel-reextract-v1',
  'eios:action:nexloop.claim.reextract_contact:1',array['nexloop_api']);
 if jsonb_typeof(c) is distinct from 'object' or (select count(*) from jsonb_object_keys(c))<>1
  or coalesce(c->>'extractor_version','')!~'^nx019-extractor/[0-9]{1,4}$' then
  raise exception 'contact channel reextract invalid' using errcode='22023';end if;
 v_version:=c->>'extractor_version';
 with candidates as (
  select cl.consumer_id,cl.conversation_id,cl.source_message_id,cl.source_sequence,
         exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.consumer_id=cl.consumer_id) erased,
         runtime.nexloop_consumer_persona(t,p_world,cl.consumer_id)<>'real' simulated
    from ontology.nexloop_claims cl
   where cl.tenant_id=t and cl.world=p_world and cl.speaker='consumer' and cl.epistemic_kind in ('preference','constraint')
     and cl.source_message_id is not null and cl.superseded_at is null and cl.resolution_state<>'superseded'
     and coalesce(cl.value->>'type','')<>'erased'
     and runtime.nexloop_contact_preference_dimension(cl.predicate) is null
     and (cl.predicate~*'(联系|沟通|通知|接收|消息|通讯|触达)\s*(渠道|方式|途径)|(contact|communication|notification|preferred)\s*(channel|method)|channel'
       or cl.quote~*'(站内|短信|电话|邮件|微信|in[- ]app|message me here|text me|call me|email me)')
     and not exists(select 1 from ontology.nexloop_claims n where n.tenant_id=cl.tenant_id and n.world=cl.world and n.source_message_id=cl.source_message_id
       and n.extractor_version=v_version)),
 eligible as (select distinct conversation_id,source_message_id,source_sequence from candidates where not erased and not simulated),
 grouped as (select conversation_id,max(source_sequence) through_sequence,
   (array_agg(source_message_id order by source_sequence))[1:50] message_ids from eligible group by conversation_id)
 select coalesce(jsonb_agg(jsonb_build_object('conversation_id',g.conversation_id,'through_sequence',g.through_sequence,'message_ids',to_jsonb(g.message_ids))
          order by g.conversation_id),'[]'::jsonb),
        jsonb_build_object('candidate_claims',(select count(*) from candidates),
          'skipped_erasure',(select count(distinct consumer_id) from candidates where erased),
          'skipped_persona',(select count(distinct consumer_id) from candidates where simulated and not erased),
          'consumers',(select count(distinct consumer_id) from candidates where not erased and not simulated),
          'messages',(select count(*) from eligible))
   into windows,n from grouped g;
 return jsonb_build_object('extractor_version',v_version,'windows',windows,'counts',n);
end $$;
alter function authz.nexloop_contact_channel_reextract(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_contact_channel_reextract(text,text,text,text,text) from public;
grant execute on function authz.nexloop_contact_channel_reextract(text,text,text,text,text) to nexloop_api;
