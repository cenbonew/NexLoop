/** NX-031: the deterministic per-context responses keep serving a long-lived Host past the initial queue depth. */
import {describe,it,expect} from 'vitest';
import {createModels,fauxProvider,fauxAssistantMessage,type Context} from '@earendil-works/pi-ai';
import {refillingResponses} from '../dist/deterministic-queue.js';
function provider(){
 const models=createModels({authContext:{env:async()=>undefined,fileExists:async()=>false}}),faux=fauxProvider();models.setProvider(faux.provider);
 const ask=(text:string)=>models.streamSimple(faux.getModel(),{messages:[{role:'user',content:text,timestamp:0}]} as Context).result();
 return {faux,ask};
}
const echo=(context:Context)=>{const last=context.messages.at(-1);return fauxAssistantMessage('echo:'+(last?.role==='user'&&typeof last.content==='string'?last.content:''));};
describe('refilling deterministic responses',()=>{
 it('serves far more turns than the queue depth, each from its own context',async()=>{
  const {faux,ask}=provider();faux.setResponses(refillingResponses(faux,echo,2));
  for(let turn=0;turn<70;turn++){
   const message=await ask('turn-'+turn);
   expect(message.stopReason).toBe('stop');expect(message.content).toEqual([{type:'text',text:'echo:turn-'+turn}]);
  }
  expect(faux.getPendingResponseCount()).toBe(2);
 });
 it('a plain queue of the same depth is exhausted (the failure the load lane hit)',async()=>{
  const {faux,ask}=provider();faux.setResponses([echo,echo]);
  await ask('a');await ask('b');
  expect((await ask('c')).stopReason).toBe('error');
 });
});
