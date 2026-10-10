/** Global active-Run limit of one Agent Host (ADR-022 §4, ADR-024).
 * A Run holds one slot from admission until its submission has settled and its harness is idle.
 * Waiting happens before any guard request, so it never builds or extends a proof; the wait is
 * bounded by both the configured budget and the command's not_after. FIFO; a timed-out waiter
 * leaves the queue with the explicit retryable `runtime_capacity_exhausted`. */
import {RuntimeError} from './runtime-adapter.js';

export const DEFAULT_MAXIMUM_ACTIVE_RUNS=4;
export const DEFAULT_RUN_ADMISSION_WAIT_MS=500;

export type RunSlot={release():void};
type Waiter={runId:string;resolve:(slot:RunSlot|undefined)=>void;timer:ReturnType<typeof setTimeout>};

export function validateAdmissionConfiguration(maximum:unknown,waitMs:unknown){
  const limit=maximum===undefined?DEFAULT_MAXIMUM_ACTIVE_RUNS:maximum;
  const wait=waitMs===undefined?DEFAULT_RUN_ADMISSION_WAIT_MS:waitMs;
  if(!Number.isInteger(limit)||(limit as number)<1||(limit as number)>64)throw new Error('runtime configuration refused');
  if(!Number.isInteger(wait)||(wait as number)<0||(wait as number)>30000)throw new Error('runtime configuration refused');
  return {maximum:limit as number,waitMs:wait as number};
}

export class RunAdmissionGate{
  private readonly active=new Set<string>();
  private readonly queue:Waiter[]=[];
  constructor(readonly maximum:number,readonly waitMs:number,private readonly now:()=>number=Date.now){
    validateAdmissionConfiguration(maximum,waitMs);
  }
  get activeRuns(){return this.active.size;}
  get waiting(){return this.queue.length;}
  /** undefined: this Run already holds a slot (idempotent retry of the same Run). */
  acquire(runId:string,notAfter:number):Promise<RunSlot|undefined>{
    if(this.active.has(runId))return Promise.resolve(undefined);
    if(this.active.size<this.maximum&&this.queue.length===0)return Promise.resolve(this.take(runId));
    const remaining=Math.min(this.now()+this.waitMs,notAfter)-this.now();
    if(remaining<=0)return Promise.reject(new RuntimeError('runtime_capacity_exhausted'));
    return new Promise((resolve,reject)=>{
      const waiter:Waiter={runId,resolve,timer:setTimeout(()=>{
        const index=this.queue.indexOf(waiter);if(index>=0)this.queue.splice(index,1);
        reject(new RuntimeError('runtime_capacity_exhausted'));
      },remaining)};
      this.queue.push(waiter);
    });
  }
  private take(runId:string):RunSlot{
    this.active.add(runId);let released=false;
    return {release:()=>{if(released)return;released=true;this.active.delete(runId);this.drain();}};
  }
  private drain(){
    while(this.queue.length&&this.active.size<this.maximum){
      const waiter=this.queue.shift()!;clearTimeout(waiter.timer);
      waiter.resolve(this.active.has(waiter.runId)?undefined:this.take(waiter.runId));
    }
  }
}
