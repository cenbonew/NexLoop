/** Host-owned model selection. Never serialize provider credentials into a Run. */
import {createModels,fauxProvider,fauxAssistantMessage,type Models} from '@earendil-works/pi-ai';
import {deepseekProvider} from '@earendil-works/pi-ai/providers/deepseek';
import {RuntimeError} from './runtime-adapter.js';

type Material=(path:string,maximum:number,allowEmpty?:boolean)=>Buffer;
export type TrustedModelSelection={
  models:Models;
  model:{provider:string;modelId:string};
  runtimeProfile:'deterministic-test'|'deepseek-flash';
  validation:'test_only_real_validation_blocked'|'real_validation_pending';
  credentialSource:'none'|'secret_file'|'environment';
  estimatedReservationFloor?:string;
};

export function validateEstimatedReservation(value:unknown,floor:string):void{
  const units=(text:unknown)=>{if(typeof text!=='string'||!/^\d{1,8}(\.\d{1,8})?$/.test(text))throw new RuntimeError('runtime_model_configuration_refused');const [whole,fraction='']=text.split('.');return BigInt(whole!)*100000000n+BigInt(fraction.padEnd(8,'0'));};
  if(units(value)<units(floor))throw new RuntimeError('runtime_model_configuration_refused');
}

/** Frozen tariff estimate, not a guarantee of live supplier billing. */
export function capTrustedDeepSeekModels(models:Models,model:unknown):{models:Models;estimatedReservationFloor:string}{
  const fail=()=>{throw new RuntimeError('runtime_model_configuration_refused');};
  if(!model||typeof model!=='object')return fail();
  let snapshot:unknown;try{snapshot=structuredClone(model);}catch{return fail();}
  const m=snapshot as Record<string,unknown>,cost=m.cost as Record<string,unknown>|undefined;
  if(m.provider!=='deepseek'||m.id!=='deepseek-flash'||m.api!=='openai-completions'||m.baseUrl!=='https://api.deepseek.com'||!Number.isSafeInteger(m.contextWindow)||Number(m.contextWindow)<1||!Number.isSafeInteger(m.maxTokens)||Number(m.maxTokens)<4096||!cost)return fail();
  const rates=['input','cacheRead','cacheWrite','output'].map(name=>cost[name]);
  if(rates.some(rate=>typeof rate!=='number'||!Number.isFinite(rate)||rate<0))return fail();
  const input=Math.max(...rates.slice(0,3) as number[]),output=rates[3] as number;
  const estimate=(Number(m.contextWindow)*input+4096*output)/1000000;
  if(!Number.isFinite(estimate)||estimate<=0||estimate*100000000>Number.MAX_SAFE_INTEGER)return fail();
  const floor=(Math.ceil(estimate*100000000)/100000000).toFixed(8);
  const freeze=(value:unknown):void=>{if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}};freeze(m);
  const guarded=new Proxy(models,{get(target,property){
    const value=Reflect.get(target,property,target);
    if(property==='streamSimple'&&typeof value==='function')return (...args:unknown[])=>{
      const actual=args[0];
      if(!actual||typeof actual!=='object'||(actual as Record<string,unknown>).provider!==m.provider||(actual as Record<string,unknown>).id!==m.id)return fail();
      const options=args[2];if(options!==undefined&&(!options||typeof options!=='object'||Array.isArray(options)))return fail();
      return Reflect.apply(value,target,[m,args[1],{...(options as object|undefined),maxTokens:4096}]);
    };
    return typeof value==='function'?value.bind(target):value;
  }});
  return {models:guarded,estimatedReservationFloor:floor};
}

/** Explicit input is trusted Host configuration, never HTTP/Run parameters.
 * Stage accepts only a nonempty private secret file; local file wins over env.
 * The supplied material reader must enforce owned private regular files.
 */
export function selectTrustedModel(environment:Readonly<Record<string,string|undefined>>,privateMaterial:Material,stage=false):TrustedModelSelection{
  const fail=()=>{throw new RuntimeError('runtime_model_configuration_refused');};
  const readKey=()=>{
    const path=environment.MODEL_CREDENTIALS_FILE?.trim();
    let key='',source:'none'|'secret_file'|'environment'='none';
    if(path){try{key=privateMaterial(path,16384,true).toString('utf8').trim();}catch{return fail();}}
    if(key)source='secret_file';
    else if(environment.MODEL_API_KEY?.trim()){
      if(stage)return fail();
      key=environment.MODEL_API_KEY.trim();source='environment';
    }
    if(key&&(key.length>8192||!/^[\x21-\x7e]+$/.test(key)))return fail();
    return {key,source};
  };
  if((environment.MODEL_ID?.trim()||'deepseek-flash')!=='deepseek-flash'||(environment.MODEL_BASE_URL?.trim()||'https://api.deepseek.com').replace(/\/$/,'')!=='https://api.deepseek.com')return fail();
  const initial=readKey();
  const provider=environment.MODEL_PROVIDER?.trim()||(initial.key?'deepseek':'test');
  if(!['deepseek','test'].includes(provider))return fail();
  if(provider==='test'||!initial.key){
    const models=createModels({authContext:{env:async()=>undefined,fileExists:async()=>false}});
    const faux=fauxProvider();models.setProvider(faux.provider);
    faux.setResponses(Array.from({length:64},()=>fauxAssistantMessage('Deterministic test runtime completion')));
    return {models,model:{provider:'faux',modelId:'faux-1'},runtimeProfile:'deterministic-test',validation:'test_only_real_validation_blocked',credentialSource:'none'};
  }
  // Frozen Pi provider has the selected Flash model and OpenAI-compatible API.
  // Its sole env lookup resolves fresh private material; ambient credentials,
  // arbitrary endpoints and OAuth/files are unavailable to this model registry.
  const models=createModels({authContext:{env:async name=>{
    if(name!=='DEEPSEEK_API_KEY')return undefined;
    const current=readKey();if(!current.key)return fail();return current.key;
  },fileExists:async()=>false}});
  models.setProvider(deepseekProvider());
  const model=models.getModel('deepseek','deepseek-flash');
  if(!model||model.api!=='openai-completions'||model.baseUrl!=='https://api.deepseek.com')return fail();
  const capped=capTrustedDeepSeekModels(models,model);
  return {...capped,model:{provider:'deepseek',modelId:'deepseek-flash'},runtimeProfile:'deepseek-flash',validation:'real_validation_pending',credentialSource:initial.source};
}
