import {describe,it,expect} from 'vitest';
import {selectTrustedModel} from '../src/trusted-model-profile.js';
const noFile=()=>{throw new Error('private source must not be exposed');};
describe('Host-owned frozen Pi model profile, no provider network',()=>{
  it('uses explicit deterministic fallback with no ambient credentials',async()=>{
    const selected=selectTrustedModel({},noFile);
    expect(selected.runtimeProfile).toBe('deterministic-test');
    expect(selected.validation).toBe('test_only_real_validation_blocked');
    expect(selected.credentialSource).toBe('none');
    expect(await selected.models.getAuth('deepseek')).toBeUndefined();
  });
  it('uses fresh private secret before development environment',async()=>{
    let material='synthetic-private-first';let reads=0;
    const selected=selectTrustedModel({MODEL_CREDENTIALS_FILE:'private-test-file',MODEL_API_KEY:'synthetic-environment'},()=>{reads++;return Buffer.from(material);});
    expect(selected.credentialSource).toBe('secret_file');
    const first=await selected.models.getAuth('deepseek');
    material='synthetic-private-rotated';const second=await selected.models.getAuth('deepseek');
    expect(first?.auth.apiKey).toBe('synthetic-private-first');
    expect(second?.auth.apiKey).toBe('synthetic-private-rotated');expect(reads).toBe(3);
    expect(JSON.stringify(selected)).not.toContain(material);
    expect(selected.models.getModel('deepseek','deepseek-flash')?.api).toBe('openai-completions');
    expect(selected.validation).toBe('real_validation_pending');
  });
  it('fails closed if a configured private file becomes unreadable',async()=>{
    let readable=true;
    const selected=selectTrustedModel({MODEL_CREDENTIALS_FILE:'private-test-file'},()=>{if(!readable)throw new Error('synthetic-private-source');return Buffer.from('synthetic-test-secret');});
    readable=false;
    await expect(selected.models.getAuth('deepseek')).rejects.toThrow();
  });
  it('empty file falls back to development environment',async()=>{
    const selected=selectTrustedModel({MODEL_CREDENTIALS_FILE:'private-test-file',MODEL_API_KEY:'synthetic-environment'},()=>Buffer.from(''));
    expect(selected.credentialSource).toBe('environment');
    expect((await selected.models.getAuth('deepseek'))?.auth.apiKey).toBe('synthetic-environment');
  });
  it('stage forbids environment-only key and permits private mounted key',()=>{
    expect(()=>selectTrustedModel({MODEL_API_KEY:'synthetic-environment'},noFile,true)).toThrow('runtime_model_configuration_refused');
    expect(selectTrustedModel({MODEL_CREDENTIALS_FILE:'private-test-file'},()=>Buffer.from('synthetic-mounted'),true).credentialSource).toBe('secret_file');
  });
  it.each([
    {MODEL_PROVIDER:'other',MODEL_API_KEY:'synthetic-key'},
    {MODEL_ID:'other-model',MODEL_API_KEY:'synthetic-key'},
    {MODEL_BASE_URL:'https://untrusted.invalid',MODEL_API_KEY:'synthetic-key'},
    {MODEL_API_KEY:'synthetic\nkey'},
  ])('rejects untrusted model selection or invalid credential without source output',environment=>{
    expect(()=>selectTrustedModel(environment,noFile)).toThrow('runtime_model_configuration_refused');
  });
});

import {capTrustedDeepSeekModels,validateEstimatedReservation} from '../src/trusted-model-profile.js';
import {type Models} from '@earendil-works/pi-ai';
describe('frozen estimate reservation and hard output cap, no network',()=>{
 const model={provider:'deepseek',id:'deepseek-flash',api:'openai-completions',baseUrl:'https://api.deepseek.com',contextWindow:1000000,maxTokens:384000,cost:{input:0.3,cacheRead:0.006,cacheWrite:0,output:1.2}};
 it('uses full context worst input tariff plus capped output and overrides caller maxTokens',()=>{
  let options:unknown;const models={streamSimple:(_m:unknown,_c:unknown,o:unknown)=>{options=o;return 'stream';}} as unknown as Models;
  const capped=capTrustedDeepSeekModels(models,model);expect(capped.estimatedReservationFloor).toBe('0.30491520');
  expect(capped.models.streamSimple(model as never,{messages:[]},{maxTokens:999999})).toBe('stream');expect(options).toEqual({maxTokens:4096});
  capped.models.streamSimple(model as never,{messages:[]});expect(options).toEqual({maxTokens:4096});
  expect(()=>capped.models.streamSimple(undefined as never,{messages:[]})).toThrow();
  expect(()=>capped.models.streamSimple({...model,id:'other'} as never,{messages:[]})).toThrow();
 });
 it.each([undefined,{...model,cost:{...model.cost,input:NaN}},{...model,cost:{...model.cost,output:-1}},{...model,contextWindow:0},{...model,maxTokens:1}])('refuses malformed frozen model metadata',value=>{
  expect(()=>capTrustedDeepSeekModels({} as Models,value)).toThrow('runtime_model_configuration_refused');
 });
});

 it('enforces exact eight decimal frozen floor boundary',()=>{
  expect(()=>validateEstimatedReservation('0.30491520','0.30491520')).not.toThrow();
  expect(()=>validateEstimatedReservation('0.30491519','0.30491520')).toThrow();
  expect(()=>validateEstimatedReservation('0.30491521','0.30491520')).not.toThrow();
 });

it('always dispatches the validated frozen model snapshot despite same-id caller or source mutation',()=>{
 const trusted={provider:'deepseek',id:'deepseek-flash',api:'openai-completions',baseUrl:'https://api.deepseek.com',contextWindow:1000000,maxTokens:384000,cost:{input:0.3,cacheRead:0.006,cacheWrite:0,output:1.2}};
 let dispatched:typeof trusted|undefined;
 const models={streamSimple:(actual:typeof trusted)=>{dispatched=actual;return 'stream';}} as unknown as Models;
 const capped=capTrustedDeepSeekModels(models,trusted);
 trusted.baseUrl='https://attacker.invalid';trusted.cost.output=0;trusted.contextWindow=1;
 const malicious={...trusted,api:'other-api',cost:{...trusted.cost,input:0}};
 capped.models.streamSimple(malicious as never,{messages:[]});
 expect(dispatched?.baseUrl).toBe('https://api.deepseek.com');expect(dispatched?.api).toBe('openai-completions');
 expect(dispatched?.contextWindow).toBe(1000000);expect(dispatched?.cost.output).toBe(1.2);expect(dispatched?.cost.input).toBe(0.3);
 expect(Object.isFrozen(dispatched)).toBe(true);expect(Object.isFrozen(dispatched?.cost)).toBe(true);
 expect(capped.estimatedReservationFloor).toBe('0.30491520');
});
