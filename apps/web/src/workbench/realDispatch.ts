/**
 * The owner's real dispatch switches from the workbench (owner 2026-10-18: "把开启调整为控制台上的一个按钮，点击即可开启").
 * Same rules as scripts/deploy/real_dispatch_switch.py, through the same governed owner Action (set_real_dispatch,
 * nexloop.real_dispatch.set:1; the SQL handler refuses anyone but the owner):
 *  - state: GET /api/v1/workbench/metrics → real_dispatch (tenant and Action switches, states and revisions only, 0153);
 *  - "一键开启": the tenant switch open with data_mode real, then the enterprise delivery Action switch; "一键关闭": the
 *    script's --close-all (the tenant switch and every open Action switch);
 *  - one request per change, in order; its Idempotency-Key is the script's: 'switch-' + sha256 of the change (with the
 *    revision it starts from) and the reason, serialised exactly like Python's json.dumps(sort_keys=True), so a retry of an
 *    unfinished run replays (by the button or by the script) and a later change of the same switch is a new request;
 *  - the first refused or unknown result stops the run (no blind retry); the state is then read again.
 * The deployment layer (REAL_DISPATCH_ENABLED, APP_HOST compose env) is not readable here and cannot be changed here.
 */
import {WorkbenchError,workbenchAct,writeKindOf,type WriteFailure} from './api';

export const DELIVER='eios:action:nexloop.enterprise.deliver:1';
export type SwitchState={enabled:boolean;revision:number};
export type RealDispatch={world:string;tenant:(SwitchState&{data_mode:string|null})|null;actions:(SwitchState&{action_type:string})[]};

function bad():never{throw new WorkbenchError('invalid');}
const obj=(v:unknown)=>v&&typeof v==='object'&&!Array.isArray(v)?v as Record<string,unknown>:bad();
const rev=(v:unknown)=>Number.isSafeInteger(v)&&Number(v)>=0?Number(v):bad();
const bool=(v:unknown)=>typeof v==='boolean'?v:bad();
const ACTION=/^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$/;
export function realDispatch(value:unknown):RealDispatch{
  const v=obj(value);
  const t=v.tenant===null||v.tenant===undefined?null:(()=>{const o=obj(v.tenant);
    if(o.data_mode!==null&&o.data_mode!=='real'&&o.data_mode!=='test')bad();
    return {enabled:bool(o.enabled),data_mode:(o.data_mode as string|null),revision:rev(o.revision)};})();
  const actions=(Array.isArray(v.actions)?v.actions:bad()).map(x=>{const o=obj(x);
    if(typeof o.action_type!=='string'||!ACTION.test(o.action_type))bad();return {action_type:o.action_type as string,enabled:bool(o.enabled),revision:rev(o.revision)};});
  return {world:typeof v.world==='string'?v.world:bad(),tenant:t,actions};
}

export type Change={scope_kind:'tenant'|'action_type';enabled:boolean;action_type:string|null;data_mode:'real'|'test'|null;from_revision:number};
/** The script's plan(): what differs from the target ('open') or every open switch ('close'). */
export function plan(current:RealDispatch,mode:'open'|'close'):Change[]{
  const tenant=current.tenant??{enabled:false,data_mode:null,revision:0};
  const actions=new Map(current.actions.map(a=>[a.action_type,a]));
  if(mode==='close'){
    const out:Change[]=[];
    if(tenant.enabled)out.push({scope_kind:'tenant',enabled:false,action_type:null,data_mode:(tenant.data_mode as 'real'|'test'|null)??'test',from_revision:tenant.revision});
    for(const name of [...actions.keys()].sort()){const a=actions.get(name)!;
      if(a.enabled)out.push({scope_kind:'action_type',enabled:false,action_type:name,data_mode:null,from_revision:a.revision});}
    return out;
  }
  const out:Change[]=[];
  if(!tenant.enabled||tenant.data_mode!=='real')out.push({scope_kind:'tenant',enabled:true,action_type:null,data_mode:'real',from_revision:tenant.revision});
  const deliver=actions.get(DELIVER)??{enabled:false,revision:0};
  if(!deliver.enabled)out.push({scope_kind:'action_type',enabled:true,action_type:DELIVER,data_mode:null,from_revision:deliver.revision});
  return out;
}

/** Python's json.dumps(value, sort_keys=True): ', ' and ': ' separators, ASCII only (\uXXXX, surrogate pairs), no NaN. */
export function pythonJson(value:unknown):string{
  if(value===null)return 'null';
  if(value===true)return 'true';if(value===false)return 'false';
  if(typeof value==='number'){if(!Number.isInteger(value))throw new Error('integers only');return String(value);}
  if(typeof value==='string'){
    let out='"';
    for(let i=0;i<value.length;i++){const c=value.charCodeAt(i);const ch=value[i];
      if(ch==='"')out+='\\"';else if(ch==='\\')out+='\\\\';else if(ch==='\n')out+='\\n';else if(ch==='\r')out+='\\r';else if(ch==='\t')out+='\\t';
      else if(ch==='\b')out+='\\b';else if(ch==='\f')out+='\\f';
      else if(c<0x20||c>0x7f)out+='\\u'+c.toString(16).padStart(4,'0');   // UTF-16 code units: astral characters become surrogate pairs, as in Python
      else out+=ch;}
    return out+'"';
  }
  if(Array.isArray(value))return '['+value.map(pythonJson).join(', ')+']';
  const o=value as Record<string,unknown>;
  return '{'+Object.keys(o).sort().map(k=>pythonJson(k)+': '+pythonJson(o[k])).join(', ')+'}';
}
export async function idempotencyKey(change:Change,reason:string):Promise<string>{
  const bytes=new TextEncoder().encode(pythonJson({...change,reason}));
  const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
  return 'switch-'+Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('').slice(0,48);
}
export function checkReason(reason:string):string|null{
  return reason!==reason.trim()||reason.length<1||reason.length>500?'原因必填，1–500 个字符，首尾不能有空格。':null;
}

export type StepResult={change:Change;ok:true;control_revision:number|null}|{change:Change;ok:false;kind:WriteFailure};
/** Runs the changes in order through the governed entry; stops at the first refused or unknown result. */
export async function apply(changes:Change[],reason:string,act=workbenchAct):Promise<StepResult[]>{
  const results:StepResult[]=[];
  for(const change of changes){
    const {from_revision:_from,...request}=change;void _from;
    try{
      const value=await act('set_real_dispatch',{...request,reason},await idempotencyKey(change,reason));
      const r=value.control_revision;results.push({change,ok:true,control_revision:typeof r==='number'?r:null});
    }catch(error){results.push({change,ok:false,kind:writeKindOf(error)});break;}
  }
  return results;
}

export function describe(change:Change):string{
  const what=change.scope_kind==='tenant'?`租户开关${change.enabled?`（数据模式 ${change.data_mode}）`:''}`:`Action 开关 ${change.action_type!.replace(/^eios:action:/,'')}`;
  return `${change.enabled?'打开':'关闭'}${what}（当前版本 ${change.from_revision}）`;
}
