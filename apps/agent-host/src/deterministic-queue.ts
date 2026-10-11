import type {AssistantMessage,Context,FauxResponseStep} from '@earendil-works/pi-ai';

/** Explicit deterministic test profiles only: a per-context response that puts itself back on the faux queue when consumed, so a
 * long-lived Host (the NX-031 load lane) keeps serving after the first `depth` model turns. The response still reads only its own
 * conversation; there is no global response index. */
export function refillingResponses(faux:{appendResponses:(steps:FauxResponseStep[])=>void},response:(context:Context)=>AssistantMessage|Promise<AssistantMessage>,depth:number):FauxResponseStep[]{
  const step:FauxResponseStep=context=>{faux.appendResponses([step]);return response(context as Context);};
  return Array.from({length:depth},()=>step);
}
