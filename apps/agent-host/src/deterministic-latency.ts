import type {AssistantMessage,Context} from '@earendil-works/pi-ai';

/** NX-031 D5: injected model latency of the explicit deterministic-test profile only (the load lane's stand-in for a real model).
 * Lognormal with the configured median and p95, drawn from a seeded generator: the same seed gives the same sequence. It only
 * sleeps before the scripted response (inside the model call, so it counts as the Run's model time) and never changes the output. */
export type DeterministicLatency={median_ms:number;p95_ms:number;seed:number};
export const LATENCY_MEDIAN_MAX_MS=10000;
export const LATENCY_P95_MAX_MS=30000;
const Z95=1.6448536269514722;

export function parseDeterministicLatency(value:unknown,runtimeProfile:unknown):DeterministicLatency|undefined{
  if(value===undefined)return undefined;
  const refuse=():never=>{throw new Error('runtime configuration refused');};
  if(runtimeProfile!=='deterministic-test'||typeof value!=='object'||value===null||Array.isArray(value))return refuse();
  const entries=value as Record<string,unknown>;
  if(Object.keys(entries).sort().join(',')!=='median_ms,p95_ms,seed')return refuse();
  const {median_ms:median,p95_ms:p95,seed}=entries;
  if(!Number.isSafeInteger(median)||!Number.isSafeInteger(p95)||!Number.isSafeInteger(seed))return refuse();
  if((median as number)<1||(median as number)>LATENCY_MEDIAN_MAX_MS||(p95 as number)<(median as number)||(p95 as number)>LATENCY_P95_MAX_MS
    ||(seed as number)<0||(seed as number)>0xffffffff)return refuse();
  return {median_ms:median as number,p95_ms:p95 as number,seed:seed as number};
}

/** Reproducible sample sequence (mulberry32 + Box–Muller); each value is capped at LATENCY_P95_MAX_MS. */
export function latencySampler({median_ms,p95_ms,seed}:DeterministicLatency):()=>number{
  let state=seed>>>0;
  const uniform=()=>{state=(state+0x6d2b79f5)>>>0;let t=state;t=Math.imul(t^(t>>>15),t|1);t^=t+Math.imul(t^(t>>>7),t|61);return (((t^(t>>>14))>>>0)+0.5)/4294967296;};
  const sigma=Math.log(p95_ms/median_ms)/Z95;
  return ()=>{
    const z=Math.sqrt(-2*Math.log(uniform()))*Math.cos(2*Math.PI*uniform());
    return Math.min(LATENCY_P95_MAX_MS,Math.round(median_ms*Math.exp(sigma*z)));
  };
}

/** The scripted response, unchanged, after one sampled sleep. */
export function delayed(response:(context:Context)=>AssistantMessage,sample:()=>number):(context:Context)=>Promise<AssistantMessage>{
  return async context=>{await new Promise(resolve=>setTimeout(resolve,sample()));return response(context);};
}
