/** Stable boundary. Inputs must additionally pass current backend lease/identity authorization. */
export type RunCommand = {
  schema_version:'1.0';run_id:string;tenant_id:string;world_id:string;
  mode:'real'|'simulation'|'shadow'|'test';request_id:string;trigger_event_id:string;
  role_ref:string;consumer_ref:string;goal_version_ref:string;context_manifest_ref:string;
  runtime_profile:string;credential_ref:string;
  budget:{maximum_model_turns:number;maximum_tool_calls:number;active_timeout_seconds:number;maximum_cost:string;currency:string};
  not_after:string;runtime_owner_epoch:number;
};
export class RuntimeError extends Error {
  readonly code:string;
  constructor(code:string){super(code);this.code=code;this.name='RuntimeError';}
}
export type RuntimeReceipt={run_id:string;request_id:string;conversation_id:number;submission_id:number};
export type RuntimeInspection=RuntimeReceipt & {submission_status:string;scheduling:string;active_tasks:number;runtime_outcome:'running'|'succeeded'|'failed'|'cancelled'|'unknown';persistence:{journal_mode:'wal';synchronous:2}};
export interface RuntimeAdapter {
  start(command:unknown,input:string):Promise<RuntimeReceipt>;
  resume(command:unknown,input:string):Promise<RuntimeReceipt>;
  inspect(command:unknown):Promise<RuntimeInspection>;
  cancel(command:unknown):Promise<{run_id:string;status:'cancel_requested'}>;
  close():Promise<void>;
}
function object(value:unknown,keys:readonly string[]):Record<string,unknown>{
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)throw new RuntimeError('invalid_run_command');
  const row=value as Record<string,unknown>;
  if(Object.keys(row).length!==keys.length||keys.some(k=>!Object.hasOwn(row,k)))throw new RuntimeError('invalid_run_command');
  return row;
}
export function validateRunCommand(value:unknown):RunCommand {
  const row=object(value,['schema_version','run_id','tenant_id','world_id','mode','request_id','trigger_event_id','role_ref','consumer_ref','goal_version_ref','context_manifest_ref','runtime_profile','credential_ref','budget','not_after','runtime_owner_epoch']);
  const string=(v:unknown,min=1,max=Number.MAX_SAFE_INTEGER)=>typeof v==='string'&&[...v].length>=min&&[...v].length<=max;
  const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const ref=/^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$/;
  const integer=(v:unknown,min:number,max:number)=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=min&&v<=max;
  if(row.schema_version!=='1.0'||typeof row.mode!=='string'||!['real','simulation','shadow','test'].includes(row.mode))throw new RuntimeError('invalid_run_command');
  for(const key of ['run_id','tenant_id','trigger_event_id'])if(!string(row[key])||!uuid.test(row[key] as string))throw new RuntimeError('invalid_run_command');
  for(const key of ['role_ref','consumer_ref','goal_version_ref','context_manifest_ref','credential_ref'])if(!string(row[key],1,512)||!ref.test(row[key] as string))throw new RuntimeError('invalid_run_command');
  if(!string(row.world_id)||!string(row.runtime_profile)||!string(row.request_id,16,200)||!integer(row.runtime_owner_epoch,1,Number.MAX_SAFE_INTEGER))throw new RuntimeError('invalid_run_command');
  if((row.mode==='real')!==(row.world_id==='real'))throw new RuntimeError('invalid_run_command');
  const date=row.not_after;
  if(typeof date!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/.test(date)||!Number.isFinite(Date.parse(date)))throw new RuntimeError('invalid_run_command');
  // Date.parse normalizes invalid month-day combinations; reject them explicitly.
  const year=Number(date.slice(0,4)),month=Number(date.slice(5,7)),day=Number(date.slice(8,10));
  const days=[31,(year%4===0&&(year%100!==0||year%400===0))?29:28,31,30,31,30,31,31,30,31,30,31];
  if(month<1||month>12||day<1||day>(days[month-1]??0)||Number(date.slice(11,13))>23||Number(date.slice(14,16))>59||Number(date.slice(17,19))>59)throw new RuntimeError('invalid_run_command');
  const budget=object(row.budget,['maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency']);
  if(!integer(budget.maximum_model_turns,1,64)||!integer(budget.maximum_tool_calls,1,128)||!integer(budget.active_timeout_seconds,1,3600)||typeof budget.maximum_cost!=='string'||!/^\d+(\.\d{1,8})?$/.test(budget.maximum_cost)||typeof budget.currency!=='string'||! /^[A-Z]{3}$/.test(budget.currency))throw new RuntimeError('invalid_run_command');
  // Snapshot caller-owned objects before asynchronous authorization.
  return structuredClone(row) as RunCommand;
}
