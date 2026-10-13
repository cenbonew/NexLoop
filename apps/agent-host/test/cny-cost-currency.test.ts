/** D-g option 2 (owner 2026-10-13): the Host's cost policy and the Run budget may be CNY. Prices come from a versioned tariff
 * at the supplier's official RMB prices, never converted from USD; a currency mismatch and any other currency stay refused;
 * a non-USD model profile without its tariff does not start. */
import {afterEach,describe,expect,it} from 'vitest';
import {mkdtemp,rm} from 'node:fs/promises';
import {mkdtempSync,writeFileSync,chmodSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {calculateCost,type Models} from '@earendil-works/pi-ai';
import {fauxAssistantMessage} from '../../../vendor/pi/packages/ai/dist/index.js';
import {capTrustedDeepSeekModels,validateModelTariff} from '../src/trusted-model-profile.js';
import {PiRuntimeAdapter} from '../src/pi-runtime-adapter.ts';
import {RuntimeHost} from '../src/runtime-host.js';
import {readPrivateMaterial} from '../src/private-material.js';
import {command,deterministic,until} from './pi-runtime-support.ts';

const TARIFF=JSON.parse(readFileSync(new URL('../../../deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json',import.meta.url),'utf8'));
const MODEL={provider:'deepseek',id:'deepseek-flash',api:'openai-completions',baseUrl:'https://api.deepseek.com',contextWindow:1000000,maxTokens:384000,cost:{input:0.3,cacheRead:0.006,cacheWrite:0,output:1.2}};

describe('CNY tariff',()=>{
  it('the repository tariff is valid and replaces the Pi USD rates (no conversion)',()=>{
    const tariff=validateModelTariff(TARIFF,'deepseek','deepseek-flash','CNY');
    expect(tariff.rates).toEqual({input:2,cacheRead:0.04,cacheWrite:2,output:8});
    let dispatched:Record<string,unknown>|undefined;
    const models={streamSimple:(m:Record<string,unknown>)=>{dispatched=m;return 'stream';}} as unknown as Models;
    const capped=capTrustedDeepSeekModels(models,MODEL,tariff);
    // (1,000,000 x 2 + 4096 x 8) / 1,000,000 = 2.032768 CNY
    expect(capped.estimatedReservationFloor).toBe('2.03276800');
    capped.models.streamSimple(MODEL as never,{messages:[]});
    expect(dispatched?.cost).toEqual({input:2,output:8,cacheRead:0.04,cacheWrite:2});
    // Pi's own usage cost uses the dispatched model's rates: 1M miss input + 1M hit + 1M output = 2 + 0.04 + 8 CNY.
    const usage={input:1000000,output:1000000,cacheRead:1000000,cacheWrite:0,totalTokens:3000000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
    expect(calculateCost(dispatched as never,usage as never).total).toBeCloseTo(10.04,8);
  });
  it.each([
    ['another currency',{...TARIFF,currency:'EUR'},'EUR'],['USD tariff',{...TARIFF,currency:'USD'},'USD'],['wrong model',{...TARIFF,model:'deepseek-v4-pro'},'CNY'],
    ['missing rate',{...TARIFF,rates:{input:2,cache_read:0.04,output:8}},'CNY'],['negative rate',{...TARIFF,rates:{...TARIFF.rates,output:-1}},'CNY'],
    ['no source',{...TARIFF,source:'http://insecure.invalid'},'CNY'],['extra key',{...TARIFF,rate_usd:1},'CNY'],
  ])('refuses a tariff with %s',(_name,value,currency)=>{
    expect(()=>validateModelTariff(value,'deepseek','deepseek-flash',currency as string)).toThrow('runtime_model_tariff_refused');
  });
});

const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const host of hosts.splice(0))await host.close();for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});});
function hostWith(extra:Record<string,unknown>){
  const root=mkdtempSync(join(tmpdir(),'nexloop-cny-host-'));chmodSync(root,0o700);roots.push(root);
  const file=(name:string,value:string)=>{const path=join(root,name);writeFileSync(path,value,{mode:0o600});return path;};
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca','synthetic-not-used-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',...extra}));
  const tariff=file('tariff',JSON.stringify(TARIFF));
  return {open:()=>{const host=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(host);return host;},tariff,file};
}

describe('Host cost currency configuration',()=>{
  it('a deterministic Host follows the configured currency; EUR and a tariff on it are refused',()=>{
    expect(()=>hostWith({runtime_profile:'deterministic-test',cost_currency:'CNY'}).open()).not.toThrow();
    expect(()=>hostWith({runtime_profile:'deterministic-test',cost_currency:'EUR'}).open()).toThrow('runtime configuration refused');
    const h=hostWith({runtime_profile:'deterministic-test',cost_currency:'CNY'});
    expect(()=>hostWith({runtime_profile:'deterministic-test',cost_currency:'CNY',model_tariff_file:h.tariff}).open()).toThrow('runtime configuration refused');
  });
  it('a CNY model profile without its tariff does not start; a tariff with USD is refused',()=>{
    const model=(h:ReturnType<typeof hostWith>)=>h.file('model-config',JSON.stringify({stage:false}));
    const a=hostWith({runtime_profile:'deepseek-flash',cost_currency:'CNY',maximum_request_cost:'3.0'});
    expect(()=>hostWith({runtime_profile:'deepseek-flash',cost_currency:'CNY',maximum_request_cost:'3.0',model_configuration_file:model(a)}).open()).toThrow('runtime model tariff required');
    const b=hostWith({});
    expect(()=>hostWith({runtime_profile:'deepseek-flash',maximum_request_cost:'3.0',model_configuration_file:model(b),model_tariff_file:b.tariff}).open()).toThrow('runtime configuration refused');
  });
});

describe('adapter: CNY policy and Run budget',()=>{
  const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
  afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const d of directories.splice(0))await rm(d,{recursive:true,force:true});});
  async function run(policyCurrency:string,budgetCurrency:string){
    const path=await mkdtemp(join(tmpdir(),'nexloop-cny-adapter-'));directories.push(path);
    const provider=deterministic();provider.faux.setResponses([fauxAssistantMessage('done')]);
    const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:policyCurrency},
      assertOwner:()=>{},authorize:async(_c,operation)=>({ever_execution_authorized:operation==='model'})});adapters.push(adapter);
    const cmd=command();cmd.budget.currency=budgetCurrency;
    await adapter.start(cmd,'input');await until(async()=>(await adapter.inspect(cmd)).runtime_outcome!=='running');
    return (await adapter.inspect(cmd)).runtime_outcome;
  }
  it('CNY with a CNY budget runs; mixed USD/CNY and EUR are refused',async()=>{
    expect(await run('CNY','CNY')).toBe('succeeded');
    expect(await run('USD','CNY')).toBe('failed');
    expect(await run('CNY','USD')).toBe('failed');
    expect(await run('EUR','EUR')).toBe('failed');
    expect(await run('USD','USD')).toBe('succeeded');
  });
});
