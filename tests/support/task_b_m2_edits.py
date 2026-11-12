"""Option B M2 (task_b_m2_effect_action_version): the effect context registrar, the effect intent commands and the admission
functions re-created with the substitutions below. Each entry: function -> (source migration stem, [(old, new), ...]); the
migration body is the source body with every `old` replaced by `new` (each exactly once). The copy guard reverts them."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / 'packages/eios-core/src/eios/migrations'

SLOT_OF = 'authz.nexloop_effect_resource_slot'
ALLOW_LIST_RESOURCE = ("not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or not exists(select 1 from "
                       "control.nexloop_effect_actions ev where ev.resource_id=c->>'resource_id' and ev.version::text=p->>'action_version')")
ANY_VERSION = SLOT_OF + "(c->>'resource_id') is null or split_part(c->>'resource_id',':',4) is distinct from p->>'action_version'"
SLOT_BY_RESOURCE = "(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id')"
SLOT_BY_NAME = SLOT_OF + "(c->>'resource_id')"

CONTEXT = [
    # Any version of an allow-listed effect Action may be proven; which one is checked against the selection below (M2-a).
    ("if not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'executor_proof'->>'resource_id')",
     "if " + SLOT_OF + "(c->'executor_proof'->>'resource_id') is null"),
    # configure: the executor proof names the version selected for the control's Consumer.
    ("  if not found then raise exception 'Consumer unavailable' using errcode='42501';end if;\n",
     "  if not found then raise exception 'Consumer unavailable' using errcode='42501';end if;\n"
     "  if c->'executor_proof'->>'resource_id' is distinct from authz.nexloop_effect_bind_action_resource(c->>'tenant_id',p_world,"
     + SLOT_OF + "(c->'executor_proof'->>'resource_id'),consumer.schema_version) then\n"
     "   raise exception 'effect Action version unavailable' using errcode='42501';end if;\n"),
    ("   or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'run_proof'->>'resource_id') then raise exception 'Run required'",
     "   or " + SLOT_OF + "(c->'run_proof'->>'resource_id') is null then raise exception 'Run required'"),
    # bind (M2-a): both proofs name the version selected in SQL for the Consumer's current version.
    ("    and ea.resource_id=c->'run_proof'->>'resource_id' and ea.resource_id=c->'executor_proof'->>'resource_id')",
     "    and authz.nexloop_effect_bind_action_resource(c->>'tenant_id',p_world,ea.slot_identity,consumer.schema_version)=c->'run_proof'->>'resource_id'\n"
     "    and c->'run_proof'->>'resource_id'=c->'executor_proof'->>'resource_id')"),
    # A rebinding of the step keeps its context's version.
    ("   context_uuid:=existing.context_id;\n",
     "   context_uuid:=existing.context_id;\n"
     "   if (select coalesce(x.action_resource,c->'executor_proof'->>'resource_id') from control.nexloop_effect_contexts x where x.context_id=existing.context_id)\n"
     "    is distinct from c->'executor_proof'->>'resource_id' then raise exception 'effect context conflict' using errcode='P0001';end if;\n"),
    # The selected resource is written explicitly (0451's trigger only fills an omitted one).
    ("'revision',consumer.nexloop_revision)),l.executor_principal);",
     "'revision',consumer.nexloop_revision)),l.executor_principal,c->'executor_proof'->>'resource_id');"),
]

INTENT = [
    (ALLOW_LIST_RESOURCE, ANY_VERSION),
    ("slot_identity=" + SLOT_BY_RESOURCE, "slot_identity=" + SLOT_BY_NAME),
    # The intent names exactly its context's effect Action resource.
    ("ea.slot_identity=ctx.slot_identity and ea.resource_id=c->>'resource_id';",
     "ea.slot_identity=ctx.slot_identity and c->>'resource_id'=coalesce(ctx.action_resource,ea.resource_id);"),
]

WRAPPERS = [(ALLOW_LIST_RESOURCE, ANY_VERSION), ("slot_identity=" + SLOT_BY_RESOURCE, "slot_identity=" + SLOT_BY_NAME)]

RETIRE = [
    # M2-b: a version an unexpired context still names is not retired (contexts are immutable; they expire within minutes).
    (" perform set_config('eios.tenant_id',p_tenant,true);\n update control.nexloop_effect_action_admissions",
     " perform set_config('eios.tenant_id',p_tenant,true);\n"
     # L6 F2: the admission row is locked first; a bind holding it (for share) finishes before the context check below.
     " perform 1 from control.nexloop_effect_action_admissions where tenant_id=p_tenant and world='real' and stable_name=p_stable_name\n"
     "  and version=p_version and retired_at is null for update;\n"
     " if exists(select 1 from control.nexloop_effect_contexts x where x.tenant_id=p_tenant and x.world='real'\n"
     "   and x.action_resource='eios:action:'||p_stable_name||':'||p_version and x.valid_until>clock_timestamp()) then\n"
     "  return jsonb_build_object('retired',false,'admission_id',null,'code','referenced_by_unexpired_context');end if;\n"
     " update control.nexloop_effect_action_admissions"),
]

ISSUE_CHECK = ("  if not found or v_consumer.nexloop_revision<>(v_props->>'consumer_revision')::bigint then raise exception '{msg}' using errcode='42501';end if;\n")
BIND = "authz.nexloop_effect_bind_action_resource(v_tenant,p_world,'{slot}',v_consumer.schema_version)"

ISSUE = [
    # The Run's effect resources are the versions selected for the Consumer's current version (option B M2.1), checked below.
    ("or v_props->'allowed_actions' not in ('[\"eios:action:nexloop.service.request:1\"]'::jsonb,'[\"eios:action:nexloop.service.request:1\",\"eios:action:nexloop.enterprise.deliver:1\"]'::jsonb)",
     "or jsonb_typeof(v_props->'allowed_actions') is distinct from 'array' or jsonb_array_length(v_props->'allowed_actions') not between 1 and 2"),
    (ISSUE_CHECK.format(msg='message relay unavailable').lstrip(' '),
     ISSUE_CHECK.format(msg='message relay unavailable').lstrip(' ')
     + " if v_props->'allowed_actions' not in (jsonb_build_array(" + BIND.format(slot='service.request:primary') + "),\n"
     "   jsonb_build_array(" + BIND.format(slot='service.request:primary') + "," + BIND.format(slot='enterprise.deliver:primary') + ")) then\n"
     "  raise exception 'message relay unavailable' using errcode='42501';end if;\n"),
    ("resource_id='eios:action:nexloop.service.request:1' and active for share;", "resource_id=v_props->'allowed_actions'->>0 and active for share;"),
    ("if v_proof->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1' then", "if v_proof->>'resource_id' is distinct from v_props->'allowed_actions'->>0 then"),
    ("if v_proof2->>'resource_id' is distinct from 'eios:action:nexloop.enterprise.deliver:1' then", "if v_proof2->>'resource_id' is distinct from v_props->'allowed_actions'->>1 then"),
]
HANDOFF = [
    ("(c->>'assignment_text')::jsonb->'allowed_actions' @> '[\"eios:action:nexloop.enterprise.deliver:1\"]'::jsonb",
     "exists(select 1 from jsonb_array_elements_text((c->>'assignment_text')::jsonb->'allowed_actions') x where " + SLOT_OF + "(x)='enterprise.deliver:primary')"),
]
FALLBACK = [
    ("or v_props->'allowed_actions' is distinct from '[\"eios:action:nexloop.service.request:1\"]'::jsonb",
     "or jsonb_typeof(v_props->'allowed_actions') is distinct from 'array' or jsonb_array_length(v_props->'allowed_actions')<>1"),
    (ISSUE_CHECK.format(msg='reply fallback unavailable').lstrip(' '),
     ISSUE_CHECK.format(msg='reply fallback unavailable').lstrip(' ')
     + " if v_props->'allowed_actions' is distinct from jsonb_build_array(" + BIND.format(slot='service.request:primary') + ") then\n"
     "  raise exception 'reply fallback unavailable' using errcode='42501';end if;\n"),
    ("resource_id='eios:action:nexloop.service.request:1' and active for share;", "resource_id=v_props->'allowed_actions'->>0 and active for share;"),
    ("if v_proof->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1' then", "if v_proof->>'resource_id' is distinct from v_props->'allowed_actions'->>0 then"),
    ("array['eios:action:nexloop.service.request:1']", "array[v_props->'allowed_actions'->>0]"),
]
HINT = [
    ("not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'action_resource')", SLOT_OF + "(c->>'action_resource') is null"),
    # The offering names its delivery Action by stable name (its stored ':1' is not migrated, design §2.5).
    ("if 'eios:action:'||(identity->'supply'->'properties'->>'delivery_action') is distinct from c->>'action_resource' then",
     "if split_part('eios:action:'||(identity->'supply'->'properties'->>'delivery_action'),':',3) is distinct from split_part(c->>'action_resource',':',3) then"),
]
METADATA = [
    ("not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=p_resource)", SLOT_OF + "(p_resource) is null"),
    ("er.slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=p_resource)", "er.slot_identity=" + SLOT_OF + "(p_resource)"),
    ("if p_resource='eios:action:nexloop.service.request:1' then", "if " + SLOT_OF + "(p_resource)='service.request:primary' then"),
    ("where 'eios:action:'||(e.value->>'action')=p_resource;", "where split_part('eios:action:'||(e.value->>'action'),':',3)=split_part(p_resource,':',3);"),
    ("and 'eios:action:'||(so.properties->>'delivery_action')=p_resource)", "and split_part('eios:action:'||(so.properties->>'delivery_action'),':',3)=split_part(p_resource,':',3))"),
]

DELIVERY = [
    # The local delivery of a reply intent proves the intent's own effect Action version (option B M2.1), not :1.
    ("target:=case when verb in ('deliver','recover_reserve','recover_deliver') then 'eios:action:nexloop.service.request:1' else 'eios:action:nexloop.service.query:1' end;",
     "target:=case when verb in ('deliver','recover_reserve','recover_deliver') then c->>'resource_id' else 'eios:action:nexloop.service.query:1' end;\n"
     " if verb in ('deliver','recover_reserve','recover_deliver') and " + SLOT_OF + "(target) is distinct from 'service.request:primary' then\n"
     "  raise exception 'local delivery denied' using errcode='42501';end if;"),
    ("if not found or i.executor_principal is distinct from principal or i.action_name is distinct from 'nexloop.service.request'\n",
     "if not found or i.executor_principal is distinct from principal or i.action_name is distinct from 'nexloop.service.request'\n"
     "  or verb in ('deliver','recover_reserve','recover_deliver') and target is distinct from 'eios:action:'||i.action_name||':'||(i.action_definition->>'version')\n"),
]

# function -> (source stem, edits[, name in the source migration when it was renamed since])
EDITS = {
    'authz.nexloop_effect_context_command': ('effect_scope_successor_proof', CONTEXT),
    'authz.nexloop_effect_intent_command_v0040': ('effect_scope_successor_proof', INTENT),
    'authz.nexloop_effect_intent_command_v0041': ('nx037_effect_action_version_exact', WRAPPERS),
    'authz.nexloop_effect_intent_command_v0052': ('nx037_effect_action_version_exact', WRAPPERS),
    'authz.nexloop_message_run_issuance_command_before_takeover_v0049': ('nx037_enterprise_slot_issuance', ISSUE),
    'authz.nexloop_message_run_issuance_command_before_relay_handoff_v0190': ('nx037_enterprise_slot_issuance', HANDOFF),
    'authz.nexloop_reply_fallback_command_before_takeover_v0110': ('nx025_reply_fallback_run', FALLBACK, 'authz.nexloop_reply_fallback_command'),
    'authz.nexloop_effect_catalog_hint': ('nx037_run_effect_slots', HINT),
    'authz.nexloop_effect_catalog_metadata_v0401': ('nx037_run_effect_slots', METADATA),
    'authz.nexloop_local_delivery_authority': ('nx031_delivery_definition_share_lock', DELIVERY),
    'control.nexloop_retire_effect_action_admission': ('effect_action_admissions', RETIRE),
}


def source(name):
    """(header up to and including the opening dollar quote, body, closing quote) of `name`'s latest definition in its source
    migration; the header names `name` (a renamed function's source header names its old name)."""
    entry = EDITS[name]
    stem, found = entry[0], (entry[2] if len(entry) > 2 else name)
    text = next(MIGRATIONS.glob(f'*_{stem}.sql')).read_text()
    heads = list(re.finditer(r'create (?:or replace )?function ' + re.escape(found) + r'\(.*?(\$[a-z_]*\$)', text, flags=re.S | re.I))
    head = heads[-1]
    quote = head.group(1)
    header = text[head.start():head.end()]
    header = re.sub(r'^create (?:or replace )?function ' + re.escape(found), 'CREATE OR REPLACE FUNCTION ' + name, header, flags=re.I)
    return header, text[head.end():text.index(quote, head.end())], quote


def body(stem, name):
    text = next(MIGRATIONS.glob(f'*_{stem}.sql')).read_text()
    head = re.search(r'create (?:or replace )?function ' + re.escape(name) + r'\(.*?(\$[a-z_]*\$)', text, flags=re.S | re.I)
    return text[head.end():text.index(head.group(1), head.end())]


def edited(name):
    _, out, _ = source(name)
    for old, new in EDITS[name][1]:
        assert out.count(old) == 1, (name, out.count(old), old[:90])
        out = out.replace(old, new)
    return out


def statement(name):
    header, _, quote = source(name)
    return header + edited(name) + quote + ';\n'
