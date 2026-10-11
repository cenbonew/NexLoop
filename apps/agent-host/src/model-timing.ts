/** NX-031 AT-060 (dispatcher 2026-10-11, A2): a Run's model time from its durable Pi task records, numbers only.
 * Each generation task's span (startedAt → endedAt) minus the spans of the tool tasks it owns; a generation without both
 * stamps (still live, or written by an earlier version) is left out of the sum and the count. Read from the same durable
 * records inspect scans, so a restarted Host reports the same figure. Never alters scheduling, authorization or outcomes. */
export type ModelTiming={calls:number;total_ms:number;max_ms:number};
type TaskLike={readonly id:unknown;readonly kind:string;readonly owner?:unknown;readonly startedAt?:number;readonly endedAt?:number};

const span=(task:TaskLike)=>typeof task.startedAt==='number'&&typeof task.endedAt==='number'&&task.endedAt>=task.startedAt?task.endedAt-task.startedAt:undefined;

export function modelTiming(tasks:readonly TaskLike[]):ModelTiming{
  const tools=new Map<unknown,number>();
  for(const task of tasks){
    if(!task.kind.includes('tool')||task.owner===undefined)continue;
    const ms=span(task);if(ms!==undefined)tools.set(task.owner,(tools.get(task.owner)??0)+ms);
  }
  let calls=0,total=0,max=0;
  for(const task of tasks){
    if(!task.kind.includes('generation'))continue;
    const ms=span(task);if(ms===undefined)continue;
    const model=Math.max(0,ms-(tools.get(task.id)??0));
    calls++;total+=model;max=Math.max(max,model);
  }
  return {calls,total_ms:total,max_ms:max};
}
