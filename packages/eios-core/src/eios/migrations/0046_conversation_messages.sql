-- CANDIDATE DRAFT: not registered/scanned until 0045 owner freezes its lineage.
-- Governed formal objects and technical Inbox/Outbox share the same transaction.
create table control.nexloop_consumer_owners (
 tenant_id text not null,world text not null check(world='real'),principal_id text not null,
 consumer_id text not null,ownership_id text not null,idempotency_key text not null,
 primary key(tenant_id,world,principal_id),unique(tenant_id,world,idempotency_key)
);
create table runtime.nexloop_conversations (
 tenant_id text not null,world text not null check(world='real'),conversation_id text not null,
 consumer_id text not null,principal_id text not null,idempotency_key text not null,last_sequence bigint not null default 0,
 primary key(tenant_id,world,conversation_id),unique(tenant_id,world,principal_id,idempotency_key),check(last_sequence>=0)
);
create table runtime.nexloop_conversation_messages (
 tenant_id text not null,world text not null,conversation_id text not null,sequence bigint not null check(sequence>0),
 message_id text not null,idempotency_key text not null,payload_digest text not null,record jsonb not null,
 primary key(tenant_id,world,conversation_id,sequence),unique(tenant_id,world,conversation_id,idempotency_key),
 foreign key(tenant_id,world,conversation_id) references runtime.nexloop_conversations
);
create table runtime.nexloop_message_inbox (
 tenant_id text not null,world text not null,message_id text not null,event_id text not null,
 primary key(tenant_id,world,message_id),unique(tenant_id,world,event_id)
);
create table runtime.nexloop_message_outbox (
 tenant_id text not null,world text not null,event_id text not null,message_id text not null,
 conversation_id text not null,sequence bigint not null,record jsonb not null,status text not null default 'pending',
 primary key(tenant_id,world,event_id),check(status in ('pending','delivered')),
 unique(tenant_id,world,conversation_id,sequence)
);
alter table control.nexloop_consumer_owners owner to nexloop_owner;
alter table runtime.nexloop_conversations owner to nexloop_owner;
alter table runtime.nexloop_conversation_messages owner to nexloop_owner;
alter table runtime.nexloop_message_inbox owner to nexloop_owner;
alter table runtime.nexloop_message_outbox owner to nexloop_owner;
alter table control.nexloop_consumer_owners enable row level security;
alter table control.nexloop_consumer_owners force row level security;
alter table runtime.nexloop_conversations enable row level security;
alter table runtime.nexloop_conversations force row level security;
alter table runtime.nexloop_conversation_messages enable row level security;
alter table runtime.nexloop_conversation_messages force row level security;
alter table runtime.nexloop_message_inbox enable row level security;
alter table runtime.nexloop_message_inbox force row level security;
alter table runtime.nexloop_message_outbox enable row level security;
alter table runtime.nexloop_message_outbox force row level security;
create policy consumer_owner_tenant on control.nexloop_consumer_owners to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy conversation_tenant on runtime.nexloop_conversations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy conversation_message_tenant on runtime.nexloop_conversation_messages to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy message_inbox_tenant on runtime.nexloop_message_inbox to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy message_outbox_tenant on runtime.nexloop_message_outbox to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_consumer_owners,runtime.nexloop_conversations,runtime.nexloop_conversation_messages,
 runtime.nexloop_message_inbox,runtime.nexloop_message_outbox from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_conversation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 v_principal text:=a->>'principal_id';v_owner control.nexloop_consumer_owners%rowtype;v_conversation runtime.nexloop_conversations%rowtype;
 v_message runtime.nexloop_conversation_messages%rowtype;v_body jsonb;v_properties jsonb;v_create jsonb:=c->'create';v_result jsonb;
 v_id text;v_key text:=c->>'idempotency_key';v_digest text;v_sequence bigint;v_items jsonb;v_after bigint;v_limit integer;v_definition jsonb;v_capability jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-conversation-v1' or a->>'operation' is distinct from 'execute'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-conversation-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition,capability into v_definition,v_capability from control.nexloop_action_definitions
  where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_definition is distinct from a->'definition' or v_capability is distinct from a->'capability'
  or v_definition->'governance'->>'approval_mode' is distinct from 'none'
  or v_definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(v_definition->'governance'->'policy_refs')<>0 then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 if (v_verb in ('prepare_owner','commit_owner') and a->>'action_resource' is distinct from 'eios:action:ConsumerOwnership.create:1')
  or (v_verb in ('prepare_conversation','commit_conversation') and a->>'action_resource' is distinct from 'eios:action:Conversation.create:1')
  or (v_verb in ('prepare_message','commit_message') and a->>'action_resource' is distinct from 'eios:action:Message.create:1')
  or (v_verb in ('conversations','messages','events') and a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.read:1') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_verb in ('prepare_owner','commit_owner') then
  -- Registrar is a genuine governed service, never a consumer session.
  if exists(select 1 from control.nexloop_browser_sessions b where encode(b.session_token_digest,'hex')=p_digest)
   or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
      and t.binding->>'subject_kind'='service' and t.status='active')
   or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=c->>'consumer_id')
   or not exists(select 1 from control.nexloop_browser_memberships m join control.nexloop_browser_subjects s on s.subject_id=m.subject_id
     where m.tenant_id=v_tenant and m.principal_id=c->>'principal_id' and m.payload->>'status'='active' and s.payload->>'kind'='human' and s.payload->>'status'='active'
      and (m.payload->>'valid_from')::timestamptz<=clock_timestamp() and (m.payload->>'valid_until' is null or (m.payload->>'valid_until')::timestamptz>clock_timestamp())) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':owner:'||(c->>'principal_id'),0));
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  perform m.principal_id from control.nexloop_browser_memberships m join control.nexloop_browser_subjects s on s.subject_id=m.subject_id
   where m.tenant_id=v_tenant and m.principal_id=c->>'principal_id' and m.payload->>'status'='active' and s.payload->>'kind'='human' and s.payload->>'status'='active'
    and (m.payload->>'valid_from')::timestamptz<=clock_timestamp() and (m.payload->>'valid_until' is null or (m.payload->>'valid_until')::timestamptz>clock_timestamp()) for share of m,s;
  if not found then raise exception 'conversation unavailable' using errcode='42501';end if;
  select * into v_owner from control.nexloop_consumer_owners where tenant_id=v_tenant and world=p_world and principal_id=c->>'principal_id' for update;
  if found then
   if v_owner.consumer_id is distinct from c->>'consumer_id' or v_owner.idempotency_key is distinct from v_key then
    raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   return jsonb_build_object('replay',true,'result',jsonb_build_object('consumer_id',v_owner.consumer_id,'principal_id',v_owner.principal_id,'ownership_id',v_owner.ownership_id));
  end if;
  v_body=jsonb_build_object('request_id','consumer-owner-'||encode(sha256(convert_to(v_tenant||':'||(c->>'principal_id')||':'||v_key,'UTF8')),'hex'),
   'type_name','ConsumerOwnership','properties',jsonb_build_object('consumer_id',c->>'consumer_id','principal_id',c->>'principal_id'));
 elsif v_verb in ('prepare_conversation','commit_conversation','conversations','prepare_message','commit_message','messages','events') then
  -- Session digest must really be browser-backed; no SERVICE pretending Human.
  if not exists(select 1 from control.nexloop_browser_sessions b where encode(b.session_token_digest,'hex')=p_digest) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  select * into v_owner from control.nexloop_consumer_owners where tenant_id=v_tenant and world=p_world and principal_id=v_principal for share;
  if not found or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='ConsumerOwnership'
    and o.object_id=v_owner.ownership_id and o.properties=jsonb_build_object('consumer_id',v_owner.consumer_id,'principal_id',v_principal))
   or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=v_owner.consumer_id) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  if v_verb='conversations' then
   if jsonb_typeof(c->'limit') is distinct from 'number' or c->>'limit'!~'^[1-9][0-9]{0,2}$'
    or jsonb_typeof(c->'after') is distinct from 'string' or (c->>'after'<>'' and c->>'after'!~'^[0-9a-f]{64}$') then
    raise exception 'conversation unavailable' using errcode='42501';end if;
   v_limit=(c->>'limit')::integer;
   if v_limit not between 1 and 100 or c->>'after' is null then raise exception 'conversation unavailable' using errcode='42501';end if;
   select coalesce(jsonb_agg(x.record order by x.conversation_id),'[]'::jsonb) into v_items from
    (select t.conversation_id,jsonb_build_object('id',t.conversation_id,'consumer_id',t.consumer_id,'world_id',p_world,'revision',1) record
     from runtime.nexloop_conversations t where t.tenant_id=v_tenant and t.world=p_world and t.principal_id=v_principal
      and exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Conversation' and o.object_id=t.conversation_id
       and o.properties=jsonb_build_object('consumer_id',t.consumer_id,'owner_principal',v_principal))
      and t.consumer_id=v_owner.consumer_id and t.conversation_id>c->>'after' order by t.conversation_id limit v_limit) x;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   return jsonb_build_object('items',v_items,'next_cursor',case when jsonb_array_length(v_items)=v_limit then v_items->(v_limit-1)->>'id' else null end);
  end if;
  if v_verb in ('prepare_conversation','commit_conversation') then
   perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':conversation:'||v_principal||':'||v_key,0));
   select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and principal_id=v_principal and idempotency_key=v_key for update;
   if found then
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('replay',true,'result',jsonb_build_object('id',v_conversation.conversation_id,'consumer_id',v_conversation.consumer_id,'world_id',p_world,'revision',1));
   end if;
   v_body=jsonb_build_object('request_id','conversation-'||encode(sha256(convert_to(v_principal||':'||v_key,'UTF8')),'hex'),
    'type_name','Conversation','properties',jsonb_build_object('consumer_id',v_owner.consumer_id,'owner_principal',v_principal));
  else
   select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=c->>'conversation_id' for update;
   if not found or v_conversation.principal_id is distinct from v_principal or v_conversation.consumer_id is distinct from v_owner.consumer_id
    or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Conversation' and o.object_id=v_conversation.conversation_id
      and o.properties=jsonb_build_object('consumer_id',v_conversation.consumer_id,'owner_principal',v_principal)) then
    raise exception 'conversation unavailable' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   if v_verb in ('messages','events') then
    if jsonb_typeof(c->'limit') is distinct from 'number' or c->>'limit'!~'^[1-9][0-9]{0,2}$'
     or jsonb_typeof(c->'after_sequence') is distinct from 'number' or c->>'after_sequence'!~'^(0|[1-9][0-9]{0,18})$'
     or (length(c->>'after_sequence')=19 and c->>'after_sequence'>'9223372036854775807') then
     raise exception 'conversation unavailable' using errcode='42501';end if;
    v_limit=(c->>'limit')::integer;v_after=(c->>'after_sequence')::bigint;
    if v_limit not between 1 and 100 or v_after<0 then raise exception 'conversation unavailable' using errcode='42501';end if;
    select coalesce(jsonb_agg(case when v_verb='events' then jsonb_build_object('id',x.sequence::text,'type','message.accepted','data',x.record) else x.record end order by x.sequence),'[]'::jsonb) into v_items
     from (select m.sequence,m.record from runtime.nexloop_conversation_messages m where m.tenant_id=v_tenant and m.world=p_world
       and m.conversation_id=v_conversation.conversation_id and m.sequence>v_after order by m.sequence limit v_limit) x;
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('items',v_items,'next_cursor',case when jsonb_array_length(v_items)=v_limit then
      case when v_verb='events' then v_items->(v_limit-1)->>'id' else v_items->(v_limit-1)->>'sequence' end else null end);
   end if;
   if jsonb_typeof(c->'body') is distinct from 'string' or length(c->>'body') not between 1 and 8192 then raise exception 'conversation unavailable' using errcode='42501';end if;
   v_digest=encode(sha256(convert_to(jsonb_build_object('body',c->>'body')::text,'UTF8')),'hex');
   select * into v_message from runtime.nexloop_conversation_messages where tenant_id=v_tenant and world=p_world and conversation_id=v_conversation.conversation_id and idempotency_key=v_key;
   if found then
    if v_message.payload_digest is distinct from v_digest then raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('replay',true,'result',jsonb_build_object('message',v_message.record,'created',false));
   end if;
   v_sequence=v_conversation.last_sequence+1;
   v_body=jsonb_build_object('request_id','message-'||encode(sha256(convert_to(v_conversation.conversation_id||':'||v_key,'UTF8')),'hex'),'type_name','Message',
    'properties',jsonb_build_object('conversation_id',v_conversation.conversation_id,'sequence',v_sequence,'actor',v_principal,'body',c->>'body','accepted_at',clock_timestamp()::text));
  end if;
 else raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_key is null or length(v_key) not between 16 and 200 or v_key!~'^[A-Za-z0-9._~-]+$' then raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_verb like 'prepare_%' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('replay',false,'payload',v_body);
 end if;
 if jsonb_typeof(v_create) is distinct from 'object' or v_create->>'text' is null or v_create->>'signature' is null or v_create->>'payload' is null then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 -- Message accepted_at was generated in prepare inside this transaction; no
 -- runtime/client date authorizes writing. All immutable binding fields match.
 if v_verb='commit_message' then
  v_properties=(v_create->>'payload')::jsonb->'properties';
  if v_properties-'accepted_at' is distinct from (v_body->'properties')-'accepted_at'
   or jsonb_typeof(v_properties->'accepted_at') is distinct from 'string'
   or (v_properties->>'accepted_at')::timestamptz>clock_timestamp()
   or (v_properties->>'accepted_at')::timestamptz<transaction_timestamp()-interval '30 seconds' then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  v_body=jsonb_set(v_body,'{properties,accepted_at}',v_properties->'accepted_at');
 end if;
 if (v_create->>'payload')::jsonb is distinct from v_body then raise exception 'conversation unavailable' using errcode='42501';end if;
 v_result=authz.nexloop_create_object_action(p_digest,p_world,v_create->>'text',v_create->>'signature',v_create->>'payload');
 v_id=v_result->>'object_id';
 if v_verb='commit_owner' then
  insert into control.nexloop_consumer_owners values(v_tenant,p_world,c->>'principal_id',c->>'consumer_id',v_id,v_key);
  v_result=jsonb_build_object('consumer_id',c->>'consumer_id','principal_id',c->>'principal_id','ownership_id',v_id);
 elsif v_verb='commit_conversation' then
  insert into runtime.nexloop_conversations values(v_tenant,p_world,v_id,v_owner.consumer_id,v_principal,v_key,0);
  v_result=jsonb_build_object('id',v_id,'consumer_id',v_owner.consumer_id,'world_id',p_world,'revision',1);
 else
  v_result=jsonb_build_object('id',v_id,'conversation_id',v_conversation.conversation_id,'sequence',v_sequence,'actor',v_principal,
   'body',c->>'body','accepted_at',v_properties->>'accepted_at','status','accepted');
  insert into runtime.nexloop_conversation_messages values(v_tenant,p_world,v_conversation.conversation_id,v_sequence,v_id,v_key,v_digest,v_result);
  insert into runtime.nexloop_message_inbox values(v_tenant,p_world,v_id,v_conversation.conversation_id||':'||v_sequence);
  insert into runtime.nexloop_message_outbox values(v_tenant,p_world,v_conversation.conversation_id||':'||v_sequence,v_id,v_conversation.conversation_id,v_sequence,v_result,'pending');
  update runtime.nexloop_conversations set last_sequence=v_sequence where tenant_id=v_tenant and world=p_world and conversation_id=v_conversation.conversation_id;
  v_result=jsonb_build_object('message',v_result,'created',true);
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;
alter function authz.nexloop_conversation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_conversation_command(text,text,text,text,text) to nexloop_api;
