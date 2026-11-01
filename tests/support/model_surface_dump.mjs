// K1 (design dsh-superagent-idea-intake §3.2): print, per Run kind, the exact model request the Host builds, as recorded
// for NX-023 (the request snapshot's request_text). It goes through the real assembly: RuntimeHost's toolsForRun selection
// (plan outcome / outreach / incentive / reply-ends-run per Run kind) and PiRuntimeAdapter with the real-model instructions,
// against a deterministic provider that answers "done". No network, no guard, no credentials.
//   node tests/support/model_surface_dump.mjs <runs.json>   runs.json: {"<kind>": {"host": {<Host tool flags>}, "plan": bool, "pack": {...}}}
//   prints {"<kind>": <request_text object>, ...}
import {mkdtempSync,writeFileSync,chmodSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';import {randomUUID} from 'node:crypto';
import {createModels,fauxProvider,fauxAssistantMessage} from '../../vendor/pi/packages/ai/dist/index.js';
import {RuntimeHost} from '../../apps/agent-host/dist/runtime-host.js';
import {readPrivateMaterial} from '../../apps/agent-host/dist/private-material.js';
import {PiRuntimeAdapter} from '../../apps/agent-host/dist/pi-runtime-adapter.js';
import {RUN_INSTRUCTIONS} from '../../apps/agent-host/dist/runtime-effect-tools.js';
import {SIM_RUN_INSTRUCTIONS,CONTEXT_PROTOCOL_SIM} from '../../apps/agent-host/dist/sim-context.js';

const runs=JSON.parse(readFileSync(process.argv[2],'utf8'));
const roots=[];
const temp=prefix=>{const root=mkdtempSync(join(tmpdir(),prefix));chmodSync(root,0o700);roots.push(root);return root;};
function host(flags){
  const root=temp('nexloop-model-surface-host-');
  const file=(name,text)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
  const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,
    ...flags,
    // NX-041: a simulation Host (world_group simulation) needs its own id, purge key and the simulation context protocol.
    ...(flags.world_group==='simulation'?{host_id:'sim-model-surface',world_purge_key_file:file('purge-key','b'.repeat(64)),context_input_protocol:CONTEXT_PROTOCOL_SIM}:{})}));
  return new RuntimeHost(root,config,readPrivateMaterial,()=>{});
}
function command(plan,simulation){
  const run_id=randomUUID();
  return {schema_version:'1.0',run_id,tenant_id:randomUUID(),world_id:simulation?'sim:'+randomUUID():'real',mode:simulation?'simulation':'real',
    request_id:plan?'plan-reevaluate-'+run_id:'synthetic-model-surface-'+run_id,
    trigger_event_id:randomUUID(),role_ref:'role:synthetic',consumer_ref:'consumer:synthetic',goal_version_ref:'goal:synthetic-v1',
    context_manifest_ref:'artifact:synthetic',runtime_profile:'synthetic-test',credential_ref:'credential:synthetic-reference',
    budget:{maximum_model_turns:8,maximum_tool_calls:8,active_timeout_seconds:60,maximum_cost:'1.0',currency:'USD'},
    not_after:new Date(Date.now()+300_000).toISOString(),runtime_owner_epoch:1};
}
const out={};const hosts=[];
try{
  for(const [kind,run] of Object.entries(runs)){
    const h=host(run.host);hosts.push(h);
    const input=JSON.stringify(run.pack);const simulation=run.host.world_group==='simulation';const cmd=command(run.plan===true,simulation);
    h.activations.set(cmd.run_id,{ref:'activation_synthetic',input});
    const select=h.adapter.options.toolsForRun;
    // The deepseek-flash Host gives message Runs replyEndsRun; the deterministic Host omits it, and it changes no request text.
    const faux=fauxProvider();const models=createModels();models.setProvider(faux.provider);faux.setResponses([fauxAssistantMessage('done')]);
    let seen;
    const adapter=new PiRuntimeAdapter({root:temp('nexloop-model-surface-run-'),models,model:{provider:'faux',modelId:'faux-1'},
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},instructions:simulation?SIM_RUN_INSTRUCTIONS:RUN_INSTRUCTIONS,recordModelRequests:true,
      toolsForRun:select,authorize:async(_c,operation,request)=>{if(operation==='model'&&request&&seen===undefined)seen=request.request_text;return {ever_execution_authorized:false};}});
    try{
      await adapter.start(cmd,input);
      const end=Date.now()+10000;
      while((await adapter.inspect(cmd)).submission_status!=='done'){if(Date.now()>end)throw new Error('run not done: '+kind);await new Promise(r=>setTimeout(r,10));}
    }finally{await adapter.close();}
    if(seen===undefined)throw new Error('no model request recorded: '+kind);
    out[kind]=JSON.parse(seen);
  }
}finally{for(const h of hosts)await h.close();for(const r of roots)rmSync(r,{recursive:true,force:true});}
process.stdout.write(JSON.stringify(out));
