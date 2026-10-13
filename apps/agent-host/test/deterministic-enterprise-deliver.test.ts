/** NX-037 synthetic five kinds: the deterministic enterprise delivery protocol is a deterministic-test configuration only. A model
 * profile (deepseek-flash, the profile of every deployed stage Host) refuses to start with the key, and so does a deterministic
 * Host without the message-from-input protocol on context pack v2 it rides on. The tool call itself (only when the Run was
 * offered nexloop.enterprise.deliver) is exercised end to end on PostgreSQL (tests/test_nx037_synthetic_five_pg.py). */
import {afterEach,describe,expect,it} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../src/runtime-host.js';
import {readPrivateMaterial} from '../src/private-material.js';

const TARIFF=readFileSync(new URL('../../../deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json',import.meta.url),'utf8');
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const host of hosts.splice(0))await host.close();for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});});
function open(extra:Record<string,unknown>){
  const root=mkdtempSync(join(tmpdir(),'nexloop-enterprise-host-'));chmodSync(root,0o700);roots.push(root);
  const file=(name:string,value:string)=>{const path=join(root,name);writeFileSync(path,value,{mode:0o600});return path;};
  const resolved=Object.fromEntries(Object.entries(extra).map(([k,v])=>[k,v==='<tariff>'?file('tariff',TARIFF):v==='<model>'?file('model','{"stage":false}'):v]));
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca','synthetic-not-used-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',...resolved}));
  const host=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(host);return host;
}
const DETERMINISTIC={runtime_profile:'deterministic-test',effect_tools:true,deterministic_message_from_input:true,context_input_protocol:'nexloop.context-pack.v2',cost_currency:'CNY'};

describe('deterministic_enterprise_deliver',()=>{
  it('starts on the deterministic-test message protocol (context pack v2)',()=>{
    expect(()=>open({...DETERMINISTIC,deterministic_enterprise_deliver:true})).not.toThrow();
  });
  it('a model profile Host refuses to start with it',()=>{
    const model={runtime_profile:'deepseek-flash',effect_tools:true,context_input_protocol:'nexloop.context-pack.v2',model_configuration_file:'<model>',
      maximum_request_cost:'2',cost_currency:'CNY',model_tariff_file:'<tariff>'};
    expect(()=>open({...model,deterministic_enterprise_deliver:true})).toThrow('runtime configuration refused');
    // Control: the same model configuration without the key passes the configuration checks (it stops later, at the synthetic
    // empty model configuration of this test), so the refusal above is the key's.
    let without:unknown;try{open(model);}catch(error){without=error;}
    expect(String(without)).not.toContain('runtime configuration refused');
  });
  it.each([
    ['a value other than true',{deterministic_enterprise_deliver:'yes'}],['false',{deterministic_enterprise_deliver:false}],
    ['no effect tools',{deterministic_enterprise_deliver:true,effect_tools:false}],
    ['no message-from-input protocol',{deterministic_enterprise_deliver:true,deterministic_message_from_input:undefined}],
    ['another context pack',{deterministic_enterprise_deliver:true,context_input_protocol:'nexloop.context-pack.v6'}],
  ])('a deterministic Host refuses it with %s',(_name,change)=>{
    const config:Record<string,unknown>={...DETERMINISTIC,...change};
    for(const key of Object.keys(config))if(config[key]===undefined)delete config[key];
    expect(()=>open(config)).toThrow('runtime configuration refused');
  });
});
