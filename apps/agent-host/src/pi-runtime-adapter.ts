import { providerToolNames } from './provider-tool-names.js';
/** Frozen Pi harness adapter. No DB/admin/channel credentials or CodingTools. */
import {createHash} from 'node:crypto';
import {constants,closeSync,fsyncSync,lstatSync,mkdirSync,openSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {BACKGROUND_CONTEXT} from '@earendil-works/chord/context';
import {lazyStream,type Model,type Api,type Models,type AssistantMessageEventStream,type AssistantMessage} from '@earendil-works/pi-ai';
import {Harness,GenerationTask,hook,ToolResultEntry,createRegistry,defineDoc,type ConversationId,type SubmissionId,type ModelRef,type ToolRegistration} from '@earendil-works/pi-durable';
import {openNodeSqliteDatabase} from '@earendil-works/pi-durable/storage/sqlite/node';
import {SqliteStorage} from '@earendil-works/pi-durable/storage/sqlite';
import {safeProviderMessage,safeProviderStream,type ProviderFailure} from './runtime-provider-boundary.js';

/** 10/18 stage: one allowlisted stderr line per failed model call (fixed fields only; never a message, body or key). */
export function modelFailureLine(runId:string,call:number,elapsedMs:number,failure:ProviderFailure):string{
  return JSON.stringify({ts:new Date().toISOString(),service:'agent-host',level:'warning',event:'model call failed',
    detail:{run_id:runId,call_sequence:call,elapsed_ms:Math.max(0,Math.round(elapsedMs)),kind:failure.kind,http_status:failure.http_status,exception:failure.exception}})+'\n';
}
import {purgeRunDirectory,validRunId} from './run-purge.js';
import {modelTiming} from './model-timing.js';
import {MAXIMUM_REQUEST_BYTES} from './trusted-model-profile.js';
import {RuntimeError,validateRunCommand,type RunCommand,type RuntimeAdapter,type RuntimeReceipt,type RuntimeInspection} from './runtime-adapter.js';
export type RuntimeOperation='start'|'resume'|'inspect'|'cancel'|'model'|'tool';
/** Actual provider request of one model call (Context v6, AT-027). The guard recomputes
 * every digest from request_text and records it before the call is allowed. */
export type ModelRequestSnapshot={call_sequence:number;model_provider:string;model_id:string;request_text:string;request_digest:string;tool_manifest_digest:string;settings_digest:string};
/** Outcome of one recorded request (status, token usage, cost, response digest); written once. */
export type ModelResult={call_sequence:number;result_status:'succeeded'|'failed'|'unknown';usage:{input:number;output:number;cache_read:number;cache_write:number;total:number}|null;cost:string|null;response_digest:string|null};
const SECRET_SETTINGS=new Set(['apiKey','headers','signal','onPayload','authContext','credentials']);
/** Tools available to a request: the context's own list, then each system message's
 * additions/removals in order (Pi declares tools in the transcript). Same rule in SQL. */
function toolManifest(context:Record<string,unknown>):unknown[]{
  const messages=Array.isArray(context.messages)?context.messages as Array<Record<string,unknown>>:[];
  return [context.tools??[],...messages.filter(m=>m.role==='system').map(m=>({added:m.toolsAdded??[],removed:m.toolsRemoved??[]}))];
}
export type PiRuntimeOptions={
  root:string;models:Models;model:ModelRef;tools?:readonly ToolRegistration[];
  /** Trusted Host registry factory; each closure receives its immutable Run binding. */
  toolsForRun?:(command:RunCommand)=>readonly ToolRegistration[];
  /** Agent instructions of every new Run conversation (real-model profiles; the deterministic profiles script their turns). */
  instructions?:string;
  costPolicy?:{kind:'deterministic_zero';currency:string}|{kind:'bounded_request';currency:string;maximum_request_cost:string};
  /** Trusted code-only diagnostic observer; never an authorization gate.
   * Not exposed through Host JSON/HTTP. Bounded metadata only, no model body,
   * tool parameters, HookApi or backend credential. */
  observeModelResponse?:(event:{run_id:string;response_digest:string;tool_call_ids:readonly string[]})=>Promise<void>;
  assertOwner:()=>void;
  /** Context v6: send each actual model request to the guard before the call. */
  recordModelRequests?:boolean|((command:RunCommand)=>boolean);
  /** Trusted server implementation must check current identity, PG lease/fence and control revisions. */
  authorize:(command:RunCommand,operation:RuntimeOperation,request?:ModelRequestSnapshot,result?:ModelResult)=>Promise<{ever_execution_authorized:boolean}>;
};
type Binding={model_cost_reserved:string;command_digest:string;run_id:string;request_id:string;payload_digest:string;conversation_id:number;submission_id:number;started_at:number;model_calls:number;tool_calls:number};
const BindingDoc=defineDoc<Binding>({kind:'nexloop.run-binding',version:1,scope:'session',initial:()=>({model_cost_reserved:'0',command_digest:'',run_id:'',request_id:'',payload_digest:'',conversation_id:0,submission_id:0,started_at:0,model_calls:0,tool_calls:0}),checkpointWhen:()=>true});
function canonical(value:unknown):string{
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value!==null&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical((value as Record<string,unknown>)[k])).join(',')+'}';
  return JSON.stringify(value);
}
function moneyUnits(value:string):bigint{
  if(!/^\d{1,64}(\.\d{1,8})?$/.test(value))throw new RuntimeError('runtime_cost_policy_missing');
  const [whole,fraction='']=value.split('.');return BigInt(whole!)*100000000n+BigInt(fraction.padEnd(8,'0'));
}
function privateDirectory(path:string){
  const info=lstatSync(path);
  if(!info.isDirectory()||info.isSymbolicLink()||info.uid!==process.getuid!()||(info.mode&0o777)!==0o700)throw new RuntimeError('runtime_storage_refused');
}
function syncDirectory(path:string){const fd=openSync(path,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);try{fsyncSync(fd);}finally{closeSync(fd);}}
const liveStorageOwners=new Set<string>();
type OpenRun={path:string;device:number;inode:number;timer?:ReturnType<typeof setTimeout>;settled?:Promise<void>;db:Awaited<ReturnType<typeof openNodeSqliteDatabase>>;harness:Harness;command:RunCommand;binding:Binding};
export class PiRuntimeAdapter implements RuntimeAdapter{
  private readonly options:PiRuntimeOptions;
  private readonly root:string;
  private readonly runs=new Map<string,OpenRun>();
  private tail:Promise<void>=Promise.resolve();
  private closed=false;
  constructor(options:PiRuntimeOptions){
    if(process.versions.node.split('.')[0]!=='24')throw new RuntimeError('runtime_node_version');
    this.options={...options,model:{...options.model},costPolicy:options.costPolicy?{...options.costPolicy}:undefined,tools:(options.tools??[]).map(tool=>({...tool}))};this.root=resolve(options.root);
    if(typeof options.assertOwner!=='function'||typeof options.authorize!=='function')throw new RuntimeError('runtime_authority_required');
    this.owner();privateDirectory(this.root);
    for(const tool of this.options.tools??[])if(!/^nexloop\.[A-Za-z0-9_.-]+$/.test(tool.name))throw new RuntimeError('runtime_tool_refused');
  }
  private owner(){try{this.options.assertOwner();}catch{throw new RuntimeError('runtime_owner_unavailable');}}
  private serial<T>(operation:()=>Promise<T>):Promise<T>{
    const promise=this.tail.then(operation);this.tail=promise.then(()=>{},()=>{});return promise;
  }
  private async authorize(command:RunCommand,operation:RuntimeOperation,request?:ModelRequestSnapshot,result?:ModelResult){
    if(this.closed)throw new RuntimeError('runtime_closed');
    this.owner();
    if(Date.parse(command.not_after)<=Date.now())throw new RuntimeError('runtime_command_expired');
    let authority:{ever_execution_authorized:boolean};
    try{authority=await this.options.authorize(structuredClone(command),operation,request,result);if(!authority||typeof authority.ever_execution_authorized!=='boolean')throw new Error();}
    // An unanswered guard stays distinguishable from a denial all the way to the Run outcome (NX-031); anything else is a denial.
    catch(error){throw new RuntimeError(error instanceof RuntimeError&&error.code==='runtime_authorization_unavailable'?error.code:'runtime_authorization_denied');}
    this.owner();
    if(Date.parse(command.not_after)<=Date.now())throw new RuntimeError('runtime_command_expired');
    return authority;
  }
  private digest(command:RunCommand,input:string){
    if(typeof input!=='string'||Buffer.byteLength(input,'utf8')>131072)throw new RuntimeError('runtime_input_refused');
    return createHash('sha256').update(canonical({command,input})).digest('hex');
  }
  private async open(command:RunCommand,create:boolean,creationAuthorized=false):Promise<OpenRun>{
    const existing=this.runs.get(command.run_id.toLowerCase());if(existing){
      const existingPath=join(this.root,command.run_id.toLowerCase(),'runtime.sqlite');
      try{privateDirectory(join(this.root,command.run_id.toLowerCase()));const info=lstatSync(existingPath);if(info.dev!==existing.device||info.ino!==existing.inode||info.isSymbolicLink()||info.nlink!==1)throw new RuntimeError('runtime_storage_refused');}
      catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')throw new RuntimeError('runtime_state_missing');throw error;}
      return existing;
    }
    const directory=join(this.root,command.run_id.toLowerCase()),file=join(directory,'runtime.sqlite');
    let fresh=false;
    try{privateDirectory(directory);}catch(error){
      if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;
      if(!create||!creationAuthorized)throw new RuntimeError('runtime_state_missing');
      mkdirSync(directory,{mode:0o700});syncDirectory(this.root);fresh=true;
    }
    privateDirectory(directory);
    if(fresh){const fd=openSync(file,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600);try{fsyncSync(fd);}finally{closeSync(fd);}syncDirectory(directory);}
    else{
      try{const stat=lstatSync(file);if(!stat.isFile()||stat.isSymbolicLink()||stat.uid!==process.getuid!()||stat.nlink!==1||(stat.mode&0o777)!==0o600)throw new RuntimeError('runtime_storage_refused');}
      catch(error){
        if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;
        if(!create||!creationAuthorized)throw new RuntimeError('runtime_state_missing');
        // Preserve any orphaned WAL/SHM as recovery evidence. This branch only
        // repairs an empty directory left before the initial SQLite creation.
        for(const sidecar of [file+'-wal',file+'-shm']){
          try{lstatSync(sidecar);throw new RuntimeError('runtime_state_missing');}
          catch(sidecarError){if((sidecarError as NodeJS.ErrnoException).code!=='ENOENT')throw sidecarError;}
        }
        const fd=openSync(file,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600);
        try{fsyncSync(fd);}finally{closeSync(fd);}syncDirectory(directory);fresh=true;
      }
    }
    for(const sidecar of [file+'-wal',file+'-shm']){
      try{const info=lstatSync(sidecar);if(!info.isFile()||info.isSymbolicLink()||info.uid!==process.getuid!()||info.nlink!==1||(info.mode&0o777)!==0o600)throw new RuntimeError('runtime_storage_refused');}
      catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
    }
    const before=lstatSync(file);
    if(liveStorageOwners.has(file))throw new RuntimeError('runtime_storage_already_owned');
    liveStorageOwners.add(file);
    let db:Awaited<ReturnType<typeof openNodeSqliteDatabase>>;
    try{db=await openNodeSqliteDatabase(file);}catch{liveStorageOwners.delete(file);throw new RuntimeError('runtime_storage_refused');}
    let storage:SqliteStorage|undefined,harness:Harness|undefined;
    try{
      privateDirectory(directory);const after=lstatSync(file);
      if(after.dev!==before.dev||after.ino!==before.ino||after.isSymbolicLink())throw new RuntimeError('runtime_storage_refused');
      await db.exec('PRAGMA synchronous = FULL');
      if((await db.get<{synchronous:number}>('PRAGMA synchronous'))?.synchronous!==2||(await db.get<{journal_mode:string}>('PRAGMA journal_mode'))?.journal_mode!=='wal')throw new RuntimeError('runtime_durability_refused');
      storage=await SqliteStorage.open(db);
      const registry=createRegistry();
      const requestParts=(request:{model:Model<Api>;context:unknown;options:unknown})=>{
        // Exact provider request: model ref, transcript context and settings (credentials, headers
        // and callbacks excluded). Digests are over these bytes; the guard recomputes them.
        const settings=JSON.parse(JSON.stringify(request.options??{},(key,value)=>SECRET_SETTINGS.has(key)?undefined:value)??{}) as unknown;
        const context=JSON.parse(JSON.stringify(request.context)) as Record<string,unknown>;
        const request_text=canonical({model:{api:request.model.api,id:request.model.id,provider:request.model.provider},context,settings});
        // Every model request, recorded or not: the reservation floor bounds input tokens by this size (MAXIMUM_REQUEST_BYTES).
        if(Buffer.byteLength(request_text,'utf8')>MAXIMUM_REQUEST_BYTES)throw new RuntimeError('runtime_model_request_too_large');
        return {settings,context,request_text};
      };
      const snapshot=(call:number,request:{model:Model<Api>;context:unknown;options:unknown}):ModelRequestSnapshot=>{
        const {settings,context,request_text}=requestParts(request);
        const hash=(value:string)=>createHash('sha256').update(value,'utf8').digest('hex');
        return {call_sequence:call,model_provider:request.model.provider,model_id:request.model.id,request_text,request_digest:hash(request_text),
          tool_manifest_digest:hash(canonical(toolManifest(context))),settings_digest:hash(canonical(settings))};
      };
      // Fixed per Run (NX-031 multi-protocol deterministic Host: only v6 Runs record their model requests).
      const recordRequests=typeof this.options.recordModelRequests==='function'?this.options.recordModelRequests(command):this.options.recordModelRequests===true;
      const consume=async(kind:'model'|'tool',request?:{model:Model<Api>;context:unknown;options:unknown})=>{
        if(kind==='model'&&recordRequests&&!request)throw new RuntimeError('runtime_model_request_unrecorded');
        if(kind==='model'&&request)requestParts(request);  // too large: refused before any reservation or authorization
        await this.authorize(command,kind);
        await harness!.commit(async tx=>{
          const state=await tx.doc(BindingDoc);
          if(!state.run_id||Date.now()-state.started_at>=command.budget.active_timeout_seconds*1000)throw new RuntimeError('runtime_budget_exhausted');
          const key=kind==='model'?'model_calls':'tool_calls';
          if(kind==='model'){
            const policy=this.options.costPolicy;
            // D-g option 2: USD or CNY, and always the Run budget's own currency (no conversion; a mismatch is refused).
            if(!policy||!['USD','CNY'].includes(policy.currency)||policy.currency!==command.budget.currency)throw new RuntimeError('runtime_cost_policy_missing');
            const reserved=policy.kind==='deterministic_zero'?0n:moneyUnits(policy.maximum_request_cost);
            if(policy.kind==='bounded_request'&&reserved===0n)throw new RuntimeError('runtime_cost_policy_missing');
            const next=BigInt(state.model_cost_reserved)+reserved;
            if(next>moneyUnits(command.budget.maximum_cost))throw new RuntimeError('runtime_budget_exhausted');
            state.model_cost_reserved=next.toString();
          }
          const maximum=kind==='model'?command.budget.maximum_model_turns:command.budget.maximum_tool_calls;
          if(state[key]>=maximum)throw new RuntimeError('runtime_budget_exhausted');
          state[key]++;
        },BACKGROUND_CONTEXT);
        const latest=await harness!.snapshot(BindingDoc,BACKGROUND_CONTEXT);
        if(!latest)throw new RuntimeError('runtime_budget_exhausted');
        // The durable per-Run counter numbers the request; the guard requires 1,2,3… densely.
        await this.authorize(command,kind,kind==='model'&&recordRequests?snapshot(latest.model_calls,request!):undefined);
        if(Date.now()-latest.started_at>=command.budget.active_timeout_seconds*1000)throw new RuntimeError('runtime_budget_exhausted');
        return latest.model_calls;
      };
      const settleFor=(call:number)=>{
        // One outcome per recorded call; the guard refuses a different second outcome.
        let settled=false;
        return async(message:AssistantMessage|null)=>{
          if(settled)return;settled=true;
          const usage=message?.usage;const whole=(value:unknown)=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?value:0;
          const ok=message!==null&&message.stopReason!=='error'&&message.stopReason!=='aborted';
          const cost=usage?.cost?.total;
          await this.authorize(command,'model',undefined,{call_sequence:call,result_status:message===null?'unknown':ok?'succeeded':'failed',
            usage:usage?{input:whole(usage.input),output:whole(usage.output),cache_read:whole(usage.cacheRead),cache_write:whole(usage.cacheWrite),total:whole(usage.totalTokens)}:null,
            cost:typeof cost==='number'&&Number.isFinite(cost)&&cost>=0?cost.toFixed(8):null,
            response_digest:message===null?null:createHash('sha256').update(canonical(message),'utf8').digest('hex')});
        };
      };
      const registrations=[...(this.options.tools??[]),...(this.options.toolsForRun?.(structuredClone(command))??[])];
      if(registrations.some(tool=>!/^nexloop\.[A-Za-z0-9_.-]+$/.test(tool.name))||new Set(registrations.map(tool=>tool.name)).size!==registrations.length)throw new RuntimeError('runtime_tool_refused');
      const wireNames=providerToolNames(registrations.map(tool=>tool.name),command.runtime_profile==='deepseek-flash'&&this.options.model.provider==='deepseek');
      const tools=registrations.map((tool,index)=>({...tool,name:wireNames[index]!,execute:async(args,api,context)=>{await consume('tool');const gatewayApi=new Proxy(api,{get:(target,key)=>{if(key==='models')throw new RuntimeError('runtime_tool_model_access_refused');const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});try{return await tool.execute(args,gatewayApi,context);}catch(error){
        // 10/18 stage (Run 1ab1d785): keep the tool's own fixed runtime code (e.g. runtime_effect_unavailable), never its text.
        throw new RuntimeError(error instanceof RuntimeError&&/^[a-z][a-z_]{0,63}$/.test(error.code)?error.code:'runtime_tool_unavailable');}}} satisfies ToolRegistration));
      const checkCost=(message:AssistantMessage)=>{
        const cost=message.usage?.cost?.total,policy=this.options.costPolicy;
        if(typeof cost!=='number'||!Number.isFinite(cost)||cost<0||!policy)throw new RuntimeError('runtime_model_cost_unavailable');
        if(policy.kind==='deterministic_zero'&&cost!==0)throw new RuntimeError('runtime_model_cost_mismatch');
        if(policy.kind==='bounded_request'&&(cost*100000000>Number.MAX_SAFE_INTEGER||BigInt(Math.ceil(cost*100000000))>moneyUnits(policy.maximum_request_cost)))throw new RuntimeError('runtime_model_cost_mismatch');
      };
      const observer=this.options.observeModelResponse;
      registry.install({name:'nexloop-governed',tools,...(observer?{hooks:[hook(GenerationTask,{afterResponse:async message=>{
        const ids=message.content.filter(item=>item.type==='toolCall').map(item=>item.id);
        // Explicit bound for the diagnostic metadata. No message/reference API
        // crosses this callback; frozen Pi's hook exception semantics apply.
        if(ids.length>64||ids.some(id=>Buffer.byteLength(id,'utf8')>256))throw new RuntimeError('runtime_response_observer_refused');
        await observer(Object.freeze({run_id:command.run_id,response_digest:createHash('sha256').update(canonical(message)).digest('hex'),tool_call_ids:Object.freeze([...ids])}));
      }})]}:{})});
      const guardedModels=new Proxy(this.options.models,{get:(target,property)=>{
        const value=Reflect.get(target,property,target);
        if(property==='streamSimple'&&typeof value==='function')return (...args:unknown[])=>safeProviderStream(lazyStream(args[0] as Model<Api>,async()=>{const call=await consume('model',{model:args[0] as Model<Api>,context:args[1],options:args[2]});const began=Date.now();
          let reported=false;  // the harness reads both the stream and its result: one line per call
          const report=(failure:ProviderFailure)=>{if(reported)return;reported=true;process.stderr.write(modelFailureLine(command.run_id,call,Date.now()-began,failure));};
          try{return safeProviderStream(Reflect.apply(value,target,args) as AssistantMessageEventStream,checkCost,recordRequests?settleFor(call):undefined,report);}catch(error){report({kind:'stream_exception',http_status:null,exception:error instanceof Error&&/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(error.constructor.name)?error.constructor.name:null});throw new RuntimeError('runtime_model_unavailable');}}));
        if(property==='fetchDeferred'&&typeof value==='function')return async(...args:unknown[])=>{if(recordRequests)throw new RuntimeError('runtime_model_request_unrecorded');await consume('model');try{const message=safeProviderMessage(await Reflect.apply(value,target,args) as AssistantMessage);checkCost(message);return message;}catch{throw new RuntimeError('runtime_model_unavailable');}};
        if(property==='cancelDeferred'&&typeof value==='function')return async(...args:unknown[])=>{await this.authorize(command,'cancel');try{return await Reflect.apply(value,target,args);}catch{throw new RuntimeError('runtime_model_unavailable');}};
        if(typeof value==='function')return (...args:unknown[])=>{try{const result=Reflect.apply(value,target,args);if(result instanceof Promise)return result.catch(()=>{throw new RuntimeError('runtime_model_unavailable');});return result;}catch{throw new RuntimeError('runtime_model_unavailable');}};
        return value;
      }});
      harness=await Harness.open(storage,{models:guardedModels,registry,settings:{compaction:{enabled:false}}},BACKGROUND_CONTEXT);
      const binding=await harness.snapshot(BindingDoc,BACKGROUND_CONTEXT);
      if(!binding?.run_id){
        if(!create||!creationAuthorized)throw new RuntimeError('runtime_state_missing');
        const empty=await harness.commit(async tx=>{
          const conversations=await tx.scanConversations({},1),tasks=await tx.scanTasks({},1);
          return conversations.items.length===0&&tasks.items.length===0;
        },BACKGROUND_CONTEXT);
        if(!empty)throw new RuntimeError('runtime_state_missing');
      }
      const run={path:file,device:before.dev,inode:before.ino,db,harness,command,binding:binding??{model_cost_reserved:'0',command_digest:'',run_id:'',request_id:'',payload_digest:'',conversation_id:0,submission_id:0,started_at:0,model_calls:0,tool_calls:0}};
      this.runs.set(command.run_id.toLowerCase(),run);return run;
    }catch(error){try{if(harness)await harness.close(BACKGROUND_CONTEXT);else if(storage)await storage.close(BACKGROUND_CONTEXT);else await db.close();}finally{liveStorageOwners.delete(file);}throw error;}
  }
  private receipt(run:OpenRun):RuntimeReceipt{return {run_id:run.binding.run_id,request_id:run.binding.request_id,conversation_id:run.binding.conversation_id,submission_id:run.binding.submission_id};}
  private check(run:OpenRun,command:RunCommand,digest?:string){
    const binding=run.binding;
    if(binding.run_id&&(binding.run_id!==command.run_id||binding.request_id!==command.request_id||binding.command_digest!==createHash('sha256').update(canonical(command)).digest('hex')||(digest!==undefined&&binding.payload_digest!==digest)))throw new RuntimeError('runtime_request_conflict');
  }
  private admit(command:unknown,input:string,resume:boolean){
    // NX-031 (lane-history-fix): the first guard authorization runs before the serial queue (as for inspect); the second one,
    // after the Run's storage exists and before its submission, stays inside. Closed, owner and not_after are rechecked inside.
    let valid:RunCommand,digest:string;
    try{valid=validateRunCommand(command);digest=this.digest(valid,input);}catch(error){return Promise.reject(error);}
    return this.authorize(valid,resume?'resume':'start').then(authority=>this.admitAuthorized(valid,input,digest,resume,authority));
  }
  private admitAuthorized(valid:RunCommand,input:string,digest:string,resume:boolean,authority:{ever_execution_authorized:boolean}){return this.serial(async()=>{
    if(this.closed)throw new RuntimeError('runtime_closed');
    this.owner();
    if(Date.parse(valid.not_after)<=Date.now())throw new RuntimeError('runtime_command_expired');
    const run=await this.open(valid,!resume,authority.ever_execution_authorized===false);this.check(run,valid,digest);
    let conversation=run.binding.conversation_id?await run.harness.conversation(run.binding.conversation_id as ConversationId,BACKGROUND_CONTEXT):undefined;
    if(!conversation){
      if(resume||run.binding.run_id||authority.ever_execution_authorized)throw new RuntimeError('runtime_state_missing');
      conversation=await run.harness.createConversation({ownership:{kind:'ownerless'},agent:{model:this.options.model,...(this.options.instructions?{instructions:this.options.instructions}:{})},init:async(tx,id)=>{
        const state=await tx.doc(BindingDoc);Object.assign(state,{command_digest:createHash('sha256').update(canonical(valid)).digest('hex'),run_id:valid.run_id,request_id:valid.request_id,payload_digest:digest,conversation_id:id,submission_id:0,started_at:Date.now(),model_calls:0,tool_calls:0,model_cost_reserved:'0'});
      }},BACKGROUND_CONTEXT);
      run.binding=(await run.harness.snapshot(BindingDoc,BACKGROUND_CONTEXT))!;
    }
    await this.authorize(valid,resume?'resume':'start');
    if(!run.timer){
      const deadline=Math.min(Date.parse(valid.not_after),run.binding.started_at+valid.budget.active_timeout_seconds*1000);
      run.timer=setTimeout(()=>{try{this.owner();void conversation!.abort(BACKGROUND_CONTEXT).catch(()=>{});}catch{/* Lost ownership prevents touching storage. */}},Math.max(1,deadline-Date.now()));
      run.timer.unref();
    }
    const submission=await conversation.submit({type:'input',requestId:valid.request_id,content:input},BACKGROUND_CONTEXT);
    // ADR-022 §4: the Run counts as active until its submission settled and no task of it is live.
    // A closed harness or an abort at the deadline also ends it here.
    run.settled=(async()=>{try{await submission.wait(BACKGROUND_CONTEXT);await run.harness.waitForIdle(BACKGROUND_CONTEXT);}catch{/* no longer executing here */}})();
    await run.harness.commit(async tx=>{const state=await tx.doc(BindingDoc);state.submission_id=submission.id;},BACKGROUND_CONTEXT);
    run.binding=(await run.harness.snapshot(BindingDoc,BACKGROUND_CONTEXT))!;
    return this.receipt(run);
  });}
  /** Resolves once the Run's latest admitted submission settled and its harness is idle (or the Run is closed). */
  settled(command:unknown):Promise<void>{const valid=validateRunCommand(command);return this.runs.get(valid.run_id.toLowerCase())?.settled??Promise.resolve();}
  start(command:unknown,input:string){return this.admit(command,input,false);}
  resume(command:unknown,input:string){return this.admit(command,input,true);}
  inspect(command:unknown):Promise<RuntimeInspection>{
    // NX-031 (lane-history-fix): the guard authorization of an inspect runs before the Host's single serial queue, not inside
    // it. Inside, every inspect of every Run waited for the others' guard round trips, so as guard latency grew with the
    // tenant's data the queue capped the whole Host near one Run every seven seconds and pushed requests past the dispatcher's
    // limit. The checks that guard storage (closed, owner, not_after) are repeated inside the queue.
    let valid:RunCommand;
    try{valid=validateRunCommand(command);}catch(error){return Promise.reject(error);}
    return this.authorize(valid,'inspect').then(()=>this.inspectAuthorized(valid));
  }
  private inspectAuthorized(valid:RunCommand):Promise<RuntimeInspection>{return this.serial(async()=>{
    if(this.closed)throw new RuntimeError('runtime_closed');
    this.owner();
    if(Date.parse(valid.not_after)<=Date.now())throw new RuntimeError('runtime_command_expired');
    const run=await this.open(valid,false);this.check(run,valid);
    if(!run.binding.submission_id)throw new RuntimeError('runtime_state_missing');
    const submission=await run.harness.submission(run.binding.submission_id as SubmissionId,BACKGROUND_CONTEXT);
    if(!submission)throw new RuntimeError('runtime_state_missing');
    const status=await submission.status(BACKGROUND_CONTEXT),inspection=await run.harness.inspect(BACKGROUND_CONTEXT);
    const synchronous=(await run.db.get<{synchronous:number}>('PRAGMA synchronous'))?.synchronous;
    const journal=(await run.db.get<{journal_mode:string}>('PRAGMA journal_mode'))?.journal_mode;
    if(synchronous!==2||journal!=='wal')throw new RuntimeError('runtime_durability_refused');
    // Scan the real durable history: empty active_tasks or a settled submission
    // alone must never turn a failed/faulted Tool/Generation into success.
    let scanned:readonly Parameters<typeof modelTiming>[0][number][]=[];
    // NX-031: a failed Run names an authorization failure with one allowlisted code (no transcript): an unanswered guard
    // (runtime_authorization_unavailable) wins over a denial, so a retryable cause is never reported as a refusal.
    let unavailable=false,denied=false;
    // 10/16 stage: a failed Run named no cause. Now also a returned tool error (its own code, e.g. outside_catalog_terms) or a
    // failed task's fixed runtime code: codes only (^[a-z_]{1,64}$), never a message, parameter or transcript text.
    let toolError:string|undefined,taskError:string|undefined,failedTool:string|undefined;
    const code=(value:unknown)=>typeof value==='string'&&/^[a-z][a-z_]{0,63}$/.test(value)?value:undefined;
    const toolCode=(message:{content?:unknown}):string=>{
      const text=Array.isArray(message.content)?message.content.find((part:{type?:string;text?:unknown})=>part?.type==='text')?.text:undefined;
      // A returned {code} JSON, or a thrown tool's fixed runtime code (the whole text is the code), else tool_error.
      try{return code((JSON.parse(String(text)) as {code?:unknown}).code)??'tool_error';}catch{return code(String(text).trim())??'tool_error';}
    };
    // 10/18: which tool failed, by its canonical registered name; a name this Run never offered is unregistered_tool.
    const registered=new Map<string,string>();
    try{for(const tool of [...(this.options.tools??[]),...(this.options.toolsForRun?.(structuredClone(valid))??[])]){registered.set(tool.name,tool.name);registered.set(tool.name.replaceAll('.','_'),tool.name);}}catch{/* names only */}
    const toolName=(message:{toolName?:unknown}):string=>typeof message.toolName==='string'&&registered.get(message.toolName)||'unregistered_tool';
    const marks=(message:unknown)=>{if(message==='runtime_authorization_unavailable')unavailable=true;else if(message==='runtime_authorization_denied')denied=true;};
    const runtimeOutcome=await run.harness.commit(async tx=>{
      const tasks=[];let cursor;
      do{const page=await tx.scanTasks({},256,cursor);tasks.push(...page.items);cursor=page.next;
        if(tasks.length>1024||cursor&&tasks.length>=1024)return 'unknown' as const;
      }while(cursor);
      scanned=tasks;
      if(tasks.some(task=>task.state.status!=='terminal'))return 'running' as const;
      for(const task of tasks)if(task.state.status==='terminal'&&task.state.outcome.status==='failed'){
        const message=(task.state.outcome as {error?:{message?:unknown}}).error?.message;marks(message);taskError??=code(message);
      }
      const taskFailed=tasks.some(task=>task.state.status==='terminal'&&['failed','faulted','orphaned'].includes(task.state.outcome.status));
      if(!taskFailed&&tasks.some(task=>task.state.status==='terminal'&&task.state.outcome.status==='aborted'))return 'cancelled' as const;
      // Pi deliberately completes ToolTask for a returned isError result.
      // Check public persisted ToolResultEntry across every conversation used
      // by this per-Run Harness; never return its transcript to the dispatcher.
      let entryCount=0;
      const conversations=new Set([run.binding.conversation_id as ConversationId,...tasks.map(task=>task.conversationId)]);
      for(const conversationId of conversations){
        let entryCursor;
        do{const page=await tx.scanEntries({conversationId},256,entryCursor);entryCount+=page.items.length;entryCursor=page.next;
          if(entryCount>1024||entryCursor&&entryCount>=1024)return taskFailed?'failed' as const:'unknown' as const;
          for(const entry of page.items)if(ToolResultEntry.is(entry)){
            for(const diagnostic of entry.data.diagnostics)if(diagnostic.severity==='error')marks(diagnostic.message);
            const errored=entry.model?.find(message=>message.role==='toolResult'&&message.isError);
            if(errored&&toolError===undefined){toolError=toolCode(errored as {content?:unknown});failedTool=toolName(errored as {toolName?:unknown});}
            if(!taskFailed&&(errored||entry.data.diagnostics.some(diagnostic=>diagnostic.severity==='error')))return 'failed' as const;
          }
        }while(entryCursor);
      }
      if(taskFailed)return 'failed' as const;

      if(status.type!=='input'||status.status!=='done')return status.status==='unanswered'?'failed' as const:'unknown' as const;
      const answers=await tx.scanEntries({conversationId:run.binding.conversation_id as ConversationId,minEntryId:status.answer,maxEntryId:status.answer},1);
      const answer=answers.items[0],generation=tasks.find(task=>task.id===answer?.byTaskId&&task.kind.includes('generation'));
      if(!generation||generation.state.status!=='terminal'||generation.state.outcome.status!=='completed')return 'unknown' as const;
      return 'succeeded' as const;
    },BACKGROUND_CONTEXT);
    // NX-031 A2: numbers only, from the same durable task records (a restarted Host reports the same figure).
    return {...this.receipt(run),submission_status:status.status,scheduling:inspection.scheduling,active_tasks:inspection.tasks.length,runtime_outcome:runtimeOutcome,persistence:{journal_mode:'wal',synchronous:2},
      model_timing:modelTiming(scanned),...(runtimeOutcome!=='failed'?{}:unavailable?{failure_code:'runtime_authorization_unavailable' as const}:denied?{failure_code:'runtime_authorization_denied' as const}
        :toolError?{failure_code:'runtime_tool_error' as const,failure_detail:toolError,failure_tool:failedTool}:taskError?{failure_code:'runtime_task_failed' as const,failure_detail:taskError}:{})};
  });}
  cancel(command:unknown):Promise<{run_id:string;status:'cancel_requested'}>{return this.serial(async()=>{
    const valid=validateRunCommand(command);await this.authorize(valid,'cancel');const run=await this.open(valid,false);this.check(run,valid);
    const conversation=await run.harness.conversation(run.binding.conversation_id as ConversationId,BACKGROUND_CONTEXT);
    if(!conversation)throw new RuntimeError('runtime_state_missing');
    await conversation.abort(BACKGROUND_CONTEXT);return {run_id:valid.run_id,status:'cancel_requested'};
  });}
  /** NX-029 D8: closes a settled Run (if this process opened it) and removes its private directory. */
  purge(runId:string):Promise<'purged'|'absent'>{return this.serial(async()=>{
    if(!validRunId(runId))throw new RuntimeError('invalid_runtime_request');
    const open=this.runs.get(runId);
    if(open){
      const settled=await Promise.race([(open.settled??Promise.resolve()).then(()=>true),new Promise<boolean>(resolve=>setTimeout(()=>resolve(false),50))]);
      if(!settled)throw new RuntimeError('runtime_run_active');
      if(open.timer)clearTimeout(open.timer);
      await open.harness.close(BACKGROUND_CONTEXT);liveStorageOwners.delete(open.path);this.runs.delete(runId);
    }
    return purgeRunDirectory(this.root,runId);
  });}
  close(){return this.serial(async()=>{this.closed=true;for(const run of this.runs.values())if(run.timer)clearTimeout(run.timer);const results=await Promise.allSettled([...this.runs.values()].map(async run=>{await run.harness.close(BACKGROUND_CONTEXT);liveStorageOwners.delete(run.path);}));this.runs.clear();if(results.some(r=>r.status==='rejected'))throw new RuntimeError('runtime_close_failed');});}
}
