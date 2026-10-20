/** NX-041 S6 (design §6, D3, D6): a Host process serves one world group. A real Host refuses sim:/shadow: Run commands, a simulation Host
 * refuses real and test ones - before admission, any guard request or any file read. A simulation Host keeps each world's Runs under
 * its own directory and removes a deleted world's directory as a whole. */
import {mkdtempSync,writeFileSync,chmodSync,rmSync,existsSync,mkdirSync,symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID,createHmac} from 'node:crypto';
import {afterEach,describe,it,expect} from 'vitest';
import {RuntimeHost} from '../src/runtime-host.js';
import {readPrivateMaterial} from '../src/private-material.js';
import {PiRuntimeAdapter} from '../src/pi-runtime-adapter.ts';
import {worldGroupAccepts,worldDirectory} from '../dist/worlds.js';
import {purgeWorldDirectory} from '../dist/run-purge.js';
import {command,deterministic} from './pi-runtime-support.ts';

const roots:string[]=[];const hosts:RuntimeHost[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true,force:true});});
const SIM='sim:'+randomUUID(),SHADOW='shadow:'+randomUUID();
function root(){const r=mkdtempSync(join(tmpdir(),'nexloop-sim-host-'));chmodSync(r,0o700);roots.push(r);return r;}
const PURGE_KEY='5'.repeat(64),HOST_ID='sim-host-one';
function host(group?:string,extra:Record<string,unknown>={}){
  const r=root(),file=(name:string,value:string)=>{const p=join(r,name);writeFileSync(p,value,{mode:0o600});return p;};
  const sim=group==='simulation'?{host_id:HOST_ID,world_purge_key_file:file('purge-key',PURGE_KEY)}:{};
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca','synthetic-not-used-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',...(group===undefined?{}:{world_group:group}),...sim,...extra}));
  const runtime=join(r,'runtime');mkdirSync(runtime,{mode:0o700});
  let reads=0;const material=(path:string,max:number,empty?:boolean)=>{reads++;return readPrivateMaterial(path,max,empty);};
  const h=new RuntimeHost(runtime,config,material,()=>{});hosts.push(h);return {host:h,runtime,reads:()=>reads};
}
const mac=(text:string,key=PURGE_KEY)=>createHmac('sha256',Buffer.from(key,'hex')).update(text).digest('hex');
function permit(world:string,options:{host?:string;minutes?:number;key?:string}={}){
  const expires=new Date(Date.now()+(options.minutes??10)*60_000).toISOString().replace(/\.\d{3}Z$/,'Z'),h=options.host??HOST_ID;
  return {host_id:h,world,expires_at:expires,mac:mac(`nexloop-sim-world-purge-v1:${h}:${world}:${expires}`,options.key)};
}
function verified(proof:Record<string,unknown>){
  return mac(`nexloop-sim-world-purge-proof-v1:${proof.host_id}:${proof.world}:${proof.outcome}:${proof.seen}:${proof.entries}:${proof.listing_digest}:${proof.issued_at}`)===proof.mac;
}
function run(world:string,mode:string){const c=command();c.world_id=world;c.mode=mode;c.runtime_profile='deterministic-test';return c;}
const ACTIVATION='activation_'+'a'.repeat(24);

describe('NX-041 S6 world groups',()=>{
  it('the group rule: real serves real and test, simulation serves sim and shadow, nothing else',()=>{
    for(const [group,world,ok] of [['real','real',true],['real','test',true],['real',SIM,false],['real',SHADOW,false],['simulation',SIM,true],
      ['simulation',SHADOW,true],['simulation','real',false],['simulation','test',false],['simulation','simulation',false],['real','simulation',false]] as const)
      expect(worldGroupAccepts(group,world),`${group} ${world}`).toBe(ok);
    expect(worldDirectory(SIM)).toBe(SIM.replace(':','-'));expect(worldDirectory('real')).toBeNull();expect(worldDirectory('sim:x')).toBeNull();
  });
  it('a real Host (the default) refuses sim and shadow Runs; a simulation Host refuses real and test Runs; before any read',async()=>{
    for(const [group,world,mode] of [[undefined,SIM,'simulation'],['real',SHADOW,'shadow'],['simulation','real','real'],['simulation','test','test']] as const){
      const f=host(group);const before=f.reads();
      for(const operation of ['start','inspect'] as const)
        await expect(f.host.dispatch(operation,{activation_ref:ACTIVATION,command:run(world,mode),...(operation==='start'?{input:'x'}:{})}))
          .rejects.toMatchObject({code:'invalid_run_world'});
      expect(f.reads()).toBe(before);
    }
  });
  it('an unknown world group, a simulation Host without its id and purge key, or a real Host with them is refused at construction',()=>{
    expect(()=>host('shadow')).toThrow('runtime configuration refused');
    expect(()=>host('simulation',{host_id:'Bad Id'})).toThrow('runtime configuration refused');
    expect(()=>host(undefined,{host_id:HOST_ID})).toThrow('runtime configuration refused');
  });
  it('review 6: a same-group Run passes the world check and reaches admission (the guard), unlike a cross-group one',async()=>{
    const f=host('simulation');
    const same=await f.host.dispatch('inspect',{activation_ref:ACTIVATION,command:run(SIM,'simulation')}).then(()=>'ok',(error:{code?:string})=>error.code);
    expect(same).not.toBe('invalid_run_world');expect(same).not.toBe('invalid_runtime_request');
    await expect(f.host.dispatch('inspect',{activation_ref:ACTIVATION,command:run('real','real')})).rejects.toMatchObject({code:'invalid_run_world'});
  },20000);
  it('a simulation Host purges a deleted world only with the database permit and answers with a signed proof',async()=>{
    const f=host('simulation'),dir=join(f.runtime,worldDirectory(SIM)!),run1=randomUUID();
    mkdirSync(join(dir,run1),{recursive:true,mode:0o700});writeFileSync(join(dir,run1,'runtime.sqlite'),'synthetic');
    const other=join(f.runtime,worldDirectory(SHADOW)!);mkdirSync(other,{mode:0o700});
    // Review 2.3: no permit, a forged, expired, too long, other-Host or other-world permit removes nothing.
    for(const bad of [undefined,{...permit(SIM),mac:'0'.repeat(64)},permit(SIM,{minutes:-1}),permit(SIM,{minutes:60}),permit(SIM,{host:'sim-host-two'}),
      permit(SHADOW),permit(SIM,{key:'6'.repeat(64)})]){
      await expect(f.host.purgeWorld(bad===undefined?{world:SIM}:{world:SIM,permit:bad})).rejects.toMatchObject({code:expect.stringMatching(/^invalid_/)});
    }
    expect(existsSync(dir)).toBe(true);
    const done=await f.host.purgeWorld({world:SIM,permit:permit(SIM)}) as {world:string;outcome:string;proof:Record<string,unknown>};
    expect(done.outcome).toBe('purged');expect(existsSync(dir)).toBe(false);expect(existsSync(other)).toBe(true);
    expect(done.proof).toMatchObject({world:SIM,host_id:HOST_ID,outcome:'purged',entries:2,after:'absent'});
    expect(verified(done.proof)).toBe(true);expect(String(done.proof.listing_digest)).toMatch(/^[0-9a-f]{64}$/);
    // This Host never created the directory through a Run, so it never saw the world (review 3.3).
    expect(done.proof.seen).toBe(false);
    const again=await f.host.purgeWorld({world:SIM,permit:permit(SIM)}) as {proof:Record<string,unknown>};
    expect(again.proof).toMatchObject({outcome:'absent',entries:0});expect(verified(again.proof)).toBe(true);
    for(const body of [{world:'real',permit:permit('real')},{world:'sim:x',permit:permit('sim:x')},{world:SIM,permit:permit(SIM),extra:1},{}])
      await expect(f.host.purgeWorld(body as Record<string,unknown>)).rejects.toMatchObject({code:'invalid_runtime_request'});
    await expect(f.host.purge({run_id:run1})).rejects.toMatchObject({code:'invalid_runtime_request'});  // a simulation Host needs the world
    expect(await f.host.purge({run_id:run1,world:SHADOW})).toEqual({run_id:run1,outcome:'absent'});
    const real=host();
    await expect(real.host.purgeWorld({world:SIM,permit:permit(SIM)})).rejects.toMatchObject({code:'invalid_runtime_request'});
    await expect(real.host.purge({run_id:run1,world:SIM})).rejects.toMatchObject({code:'invalid_runtime_request'});
  });
  it('the world directory purge refuses a symlink and anything that is not a simulation world',()=>{
    const r=root(),outside=root();writeFileSync(join(outside,'keep'),'x');symlinkSync(outside,join(r,worldDirectory(SIM)!));
    expect(()=>purgeWorldDirectory(r,SIM)).toThrow('runtime_storage_refused');expect(existsSync(join(outside,'keep'))).toBe(true);
    for(const bad of ['real','test','..','sim:../x',SIM.toUpperCase()])expect(()=>purgeWorldDirectory(r,bad)).toThrow('invalid_runtime_request');
  });
  it('a simulation adapter keeps a Run under its world directory; the world purge closes and removes it',async()=>{
    const r=root(),provider=deterministic();
    const adapter=new PiRuntimeAdapter({root:r,worldDirectories:true,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});adapters.push(adapter);
    const cmd=command();cmd.world_id=SIM;cmd.mode='simulation';
    await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    expect(existsSync(join(r,worldDirectory(SIM)!,cmd.run_id.toLowerCase(),'runtime.sqlite'))).toBe(true);
    expect(existsSync(join(r,cmd.run_id.toLowerCase()))).toBe(false);
    const purged=await adapter.purgeWorld(SIM);
    expect(purged.outcome).toBe('purged');expect(purged.seen).toBe(true);expect(purged.entries).toBeGreaterThanOrEqual(2);  // the Run held it
    expect((await adapter.purgeWorld(SIM))).toMatchObject({outcome:'absent',seen:true,entries:0});  // gone, and this Host had it
    expect(existsSync(join(r,worldDirectory(SIM)!))).toBe(false);
    await expect(adapter.inspect(cmd)).rejects.toMatchObject({code:'runtime_state_missing'});
  });
});
