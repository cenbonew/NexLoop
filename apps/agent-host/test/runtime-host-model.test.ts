/** Real constructor/files; negatives stop before any EIOS guard request. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../src/runtime-host.js';import {readPrivateMaterial} from '../src/private-material.js';import {command} from './pi-runtime-support.ts';
const roots:string[]=[];const hosts:RuntimeHost[]=[];const original=process.env.MODEL_API_KEY;
afterEach(async()=>{for(const host of hosts.splice(0))await host.close();for(const root of roots.splice(0))rmSync(root,{recursive:true});if(original===undefined)delete process.env.MODEL_API_KEY;else process.env.MODEL_API_KEY=original;});
function setup(options:{stage?:boolean;key?:string;cost?:string;environment?:string}={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-host-profile-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,value:string)=>{const path=join(root,name);writeFileSync(path,value,{mode:0o600});return path;};
 const key=file('model-key',options.key??'');const model=file('model-config',JSON.stringify({MODEL_CREDENTIALS_FILE:key,stage:options.stage??false}));
 const ca=file('guard-ca','synthetic-not-used-ca'),guardKey=file('guard-key','a'.repeat(64));
 const config=file('runtime-config',JSON.stringify({guard_ca_file:ca,guard_key_file:guardKey,guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deepseek-flash',model_configuration_file:model,maximum_request_cost:options.cost??'0.30491520'}));
 if(options.environment===undefined)delete process.env.MODEL_API_KEY;else process.env.MODEL_API_KEY=options.environment;
 let reads=0;const material=(path:string,max:number,empty?:boolean)=>{reads++;return readPrivateMaterial(path,max,empty);};
 return {root,key,model,config,ca,guardKey,file,material,reads:()=>reads,open(){const host=new RuntimeHost(root,config,material,()=>{});hosts.push(host);return host;}};
}
async function mismatch(f:ReturnType<typeof setup>,wrongProfile:string){const host=f.open();const before=f.reads();const cmd=command();cmd.runtime_profile=wrongProfile;await expect(host.dispatch('start',{activation_ref:'activation_'+'a'.repeat(24),command:cmd,input:'test'})).rejects.toThrow('invalid_runtime_request');expect(f.reads()).toBe(before);}
describe('actual private RuntimeHost model constructor',()=>{
 it('empty local secret and empty stage secret both select deterministic fallback',async()=>{
  await mismatch(setup(), 'deepseek-flash');await mismatch(setup({stage:true}), 'deepseek-flash');
 });
 it('stage never reads ambient model key while local uses explicit local key',async()=>{
  await mismatch(setup({stage:true,environment:'synthetic-env-secret'}),'deepseek-flash');
  await mismatch(setup({environment:'synthetic-env-secret'}),'deterministic-test');
 });
 it('real private key profile accepts exact floor and rejects below before guard',async()=>{
  await mismatch(setup({key:'synthetic-private-key',cost:'0.30491520'}),'deterministic-test');
  const f=setup({key:'synthetic-private-key',cost:'0.30491519'});expect(()=>f.open()).toThrow('runtime_model_configuration_refused');
 });
 it('nonempty private file wins over invalid ambient key and credentials stay private',async()=>{
  const f=setup({key:'synthetic-file-secret',environment:'invalid\nambient'});const host=f.open();
  expect(JSON.stringify(host)).not.toContain('synthetic-file-secret');expect(JSON.stringify(host)).not.toContain('invalid');
  const cmd=command();cmd.runtime_profile='deterministic-test';const before=f.reads();await expect(host.dispatch('inspect',{activation_ref:'activation_'+'b'.repeat(24),command:cmd})).rejects.toThrow('invalid_runtime_request');expect(f.reads()).toBe(before);
 });
 it('actual public-mode and symlink model credential files are refused',()=>{
  const f=setup({key:'synthetic-private'});chmodSync(f.key,0o644);expect(()=>f.open()).toThrow('runtime_model_configuration_refused');
  chmodSync(f.key,0o600);const alias=join(f.root,'alias');symlinkSync(f.key,alias);writeFileSync(f.model,JSON.stringify({MODEL_CREDENTIALS_FILE:alias,stage:false}));expect(()=>f.open()).toThrow('runtime_model_configuration_refused');
 });
 it('default nonempty material policy still refuses empty guard/TLS/config',()=>{
  const f=setup();for(const name of ['guard-empty','tls-empty','config-empty'])expect(()=>readPrivateMaterial(f.file(name,''),32768)).toThrow('private material refused');
 });
});
