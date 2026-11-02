/** NX-064: the Agent Host's effect request counters, without any runtime dependency (main.ts serves them on the metrics endpoint). */
/** NX-064 (ADR-029c): process-level counters of this Host's effect requests, per operation (numbers only, never a Run id or
 * any request content), read by the authenticated loopback metrics endpoint and recorded by the runtime worker as process samples.
 * calls: tool calls; ambiguous: calls whose first request was ambiguous (timeout, connection error, 503); unavailable: calls that
 * ended unavailable; h_<i>: every HTTP request's elapsed time in the guard histogram buckets (observability.histogram_bucket). */
export const EFFECT_METRIC_OPERATIONS=['effect_submit','enterprise_submit','effect_find','incentive'] as const;
export type EffectMetricOperation=typeof EFFECT_METRIC_OPERATIONS[number];
const effectCounters=new Map<EffectMetricOperation,{calls:number;ambiguous:number;unavailable:number;buckets:Map<number,number>}>();
function effectCounter(operation:EffectMetricOperation){
  let value=effectCounters.get(operation);
  if(value===undefined){value={calls:0,ambiguous:0,unavailable:0,buckets:new Map()};effectCounters.set(operation,value);}
  return value;
}
/** The guard's log-spaced bucket (1.1^i ms, bucket 0 <= 1 ms, last 116), as observability.histogram_bucket. */
export function histogramBucket(ms:number):number{
  if(!(ms>1))return 0;
  return Math.min(116,Math.max(1,Math.ceil(Math.log(ms)/Math.log(1.1)-1e-9)));
}
export function countEffect(operation:EffectMetricOperation,key:'calls'|'ambiguous'|'unavailable'){effectCounter(operation)[key]+=1;}
export async function timedRequest<T>(operation:EffectMetricOperation,request:()=>Promise<T>):Promise<T>{
  const began=Date.now();
  try{return await request();}
  finally{const buckets=effectCounter(operation).buckets;const bucket=histogramBucket(Date.now()-began);buckets.set(bucket,(buckets.get(bucket)??0)+1);}
}
/** Cumulative since the Host started: effect_calls/effect_ambiguous/effect_unavailable over every operation, then per operation
 * <operation>_calls, _ambiguous, _unavailable and its non-empty <operation>_h_<i>. */
export function effectMetrics():Record<string,number>{
  const out:Record<string,number>={};let calls=0,ambiguous=0,unavailable=0;
  for(const operation of EFFECT_METRIC_OPERATIONS){
    const value=effectCounter(operation);
    calls+=value.calls;ambiguous+=value.ambiguous;unavailable+=value.unavailable;
    out[operation+'_calls']=value.calls;out[operation+'_ambiguous']=value.ambiguous;out[operation+'_unavailable']=value.unavailable;
    for(const [bucket,count] of [...value.buckets].sort((a,b)=>a[0]-b[0]))out[`${operation}_h_${bucket}`]=count;
  }
  return {effect_calls:calls,effect_ambiguous:ambiguous,effect_unavailable:unavailable,...out};
}
