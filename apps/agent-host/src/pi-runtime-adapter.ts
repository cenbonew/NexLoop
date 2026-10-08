/** Frozen Pi harness adapter. No DB/admin/channel credentials or CodingTools. */
import {createHash} from 'node:crypto';
import {constants,closeSync,fsyncSync,lstatSync,mkdirSync,openSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {BACKGROUND_CONTEXT} from '@earendil-works/chord/context';
import {lazyStream,type Model,type Api,type Models,type AssistantMessageEventStream,type AssistantMessage} from '@earendil-works/pi-ai';
import {Harness,GenerationTask,hook,ToolResultEntry,createRegistry,defineDoc,type ConversationId,type SubmissionId,type ModelRef,type ToolRegistration} from '@earendil-works/pi-durable';
import {openNodeSqliteDatabase} from '@earendil-works/pi-durable/storage/sqlite/node';
import {SqliteStorage} from '@earendil-works/pi-durable/storage/sqlite';
import {safeProviderMessage,safeProviderStream} from './runtime-provider-boundary.js';
import {RuntimeError,validateRunCommand,type RunCommand,type RuntimeAdapter,type RuntimeReceipt,type RuntimeInspection} from './runtime-adapter.js';
export type RuntimeOperation='start'|'resume'|'inspect'|'cancel'|'model'|'tool';
export type PiRuntimeOptions={
  root:string;models:Models;model:ModelRef;tools?:readonly ToolRegistration[];
  /** Trusted Host registry factory; each closure receives its immutable Run binding. */
  toolsForRun?:(command:RunCommand)=>readonly ToolRegistration[];
  costPolicy?:{kind:'deterministic_zero';currency:string}|{kind:'bounded_request';currency:string;maximum_request_cost:string};
  /** Trusted code-only diagnostic observer; never an authorization gate.
   * Not exposed through Host JSON/HTTP. Bounded metadata only, no model body,
   * tool parameters, HookApi or backend credential. */
  observeModelResponse?:(event:{run_id:string;response_digest:string;tool_call_ids:readonly string[]})=>Promise<void>;
  assertOwner:()=>void;
  /** Trusted server implementation must check current identity, PG lease/fence and control revisions. */
  authorize:(command:RunCommand,operation:RuntimeOperation)=>Promise<{ever_execution_authorized:boolean}>;
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
type OpenRun={path:string;device:number;inode:number;timer?:ReturnType<typeof setTimeout>;db:Awaited<ReturnType<typeof openNodeSqliteDatabase>>;harness:Harness;command:RunCommand;binding:Binding};
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
  private async authorize(command:RunCommand,operation:RuntimeOperation){
    if(this.closed)throw new RuntimeError('runtime_closed');
    this.owner();
    if(Date.parse(command.not_after)<=Date.now())throw new RuntimeError('runtime_command_expired');
    let authority:{ever_execution_authorized:boolean};
    try{authority=await this.options.authorize(structuredClone(command),operation);if(!authority||typeof authority.ever_execution_authorized!=='boolean')throw new Error();}catch{throw new RuntimeError('runtime_authorization_denied');}
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
      const consume=async(kind:'model'|'tool')=>{
        await this.authorize(command,kind);
        await harness!.commit(async tx=>{
          const state=await tx.doc(BindingDoc);
          if(!state.run_id||Date.now()-state.started_at>=command.budget.active_timeout_seconds*1000)throw new RuntimeError('runtime_budget_exhausted');
          const key=kind==='model'?'model_calls':'tool_calls';
          if(kind==='model'){
            const policy=this.options.costPolicy;
            if(!policy||policy.currency!=='USD'||policy.currency!==command.budget.currency)throw new RuntimeError('runtime_cost_policy_missing');
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
        await this.authorize(command,kind);
        const latest=await harness!.snapshot(BindingDoc,BACKGROUND_CONTEXT);
        if(!latest||Date.now()-latest.started_at>=command.budget.active_timeout_seconds*1000)throw new RuntimeError('runtime_budget_exhausted');
      };
      const registrations=[...(this.options.tools??[]),...(this.options.toolsForRun?.(structuredClone(command))??[])];
      if(registrations.some(tool=>!/^nexloop\.[A-Za-z0-9_.-]+$/.test(tool.name))||new Set(registrations.map(tool=>tool.name)).size!==registrations.length)throw new RuntimeError('runtime_tool_refused');
      const tools=registrations.map(tool=>({...tool,execute:async(args,api,context)=>{await consume('tool');const gatewayApi=new Proxy(api,{get:(target,key)=>{if(key==='models')throw new RuntimeError('runtime_tool_model_access_refused');const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});try{return await tool.execute(args,gatewayApi,context);}catch{throw new RuntimeError('runtime_tool_unavailable');}}} satisfies ToolRegistration));
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
        if(property==='streamSimple'&&typeof value==='function')return (...args:unknown[])=>safeProviderStream(lazyStream(args[0] as Model<Api>,async()=>{await consume('model');try{return safeProviderStream(Reflect.apply(value,target,args) as AssistantMessageEventStream,checkCost);}catch{throw new RuntimeError('runtime_model_unavailable');}}));
        if(property==='fetchDeferred'&&typeof value==='function')return async(...args:unknown[])=>{await consume('model');try{const message=safeProviderMessage(await Reflect.apply(value,target,args) as AssistantMessage);checkCost(message);return message;}catch{throw new RuntimeError('runtime_model_unavailable');}};
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
  private admit(command:unknown,input:string,resume:boolean){return this.serial(async()=>{
    const valid=validateRunCommand(command),digest=this.digest(valid,input);
    const authority=await this.authorize(valid,resume?'resume':'start');
    const run=await this.open(valid,!resume,authority.ever_execution_authorized===false);this.check(run,valid,digest);
    let conversation=run.binding.conversation_id?await run.harness.conversation(run.binding.conversation_id as ConversationId,BACKGROUND_CONTEXT):undefined;
    if(!conversation){
      if(resume||run.binding.run_id||authority.ever_execution_authorized)throw new RuntimeError('runtime_state_missing');
      conversation=await run.harness.createConversation({ownership:{kind:'ownerless'},agent:{model:this.options.model},init:async(tx,id)=>{
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
    await run.harness.commit(async tx=>{const state=await tx.doc(BindingDoc);state.submission_id=submission.id;},BACKGROUND_CONTEXT);
    run.binding=(await run.harness.snapshot(BindingDoc,BACKGROUND_CONTEXT))!;
    return this.receipt(run);
  });}
  start(command:unknown,input:string){return this.admit(command,input,false);}
  resume(command:unknown,input:string){return this.admit(command,input,true);}
  inspect(command:unknown):Promise<RuntimeInspection>{return this.serial(async()=>{
    const valid=validateRunCommand(command);await this.authorize(valid,'inspect');const run=await this.open(valid,false);this.check(run,valid);
    if(!run.binding.submission_id)throw new RuntimeError('runtime_state_missing');
    const submission=await run.harness.submission(run.binding.submission_id as SubmissionId,BACKGROUND_CONTEXT);
    if(!submission)throw new RuntimeError('runtime_state_missing');
    const status=await submission.status(BACKGROUND_CONTEXT),inspection=await run.harness.inspect(BACKGROUND_CONTEXT);
    const synchronous=(await run.db.get<{synchronous:number}>('PRAGMA synchronous'))?.synchronous;
    const journal=(await run.db.get<{journal_mode:string}>('PRAGMA journal_mode'))?.journal_mode;
    if(synchronous!==2||journal!=='wal')throw new RuntimeError('runtime_durability_refused');
    // Scan the real durable history: empty active_tasks or a settled submission
    // alone must never turn a failed/faulted Tool/Generation into success.
    const runtimeOutcome=await run.harness.commit(async tx=>{
      const tasks=[];let cursor;
      do{const page=await tx.scanTasks({},256,cursor);tasks.push(...page.items);cursor=page.next;
        if(tasks.length>1024||cursor&&tasks.length>=1024)return 'unknown' as const;
      }while(cursor);
      if(tasks.some(task=>task.state.status!=='terminal'))return 'running' as const;
      if(tasks.some(task=>task.state.status==='terminal'&&['failed','faulted','orphaned'].includes(task.state.outcome.status)))return 'failed' as const;
      if(tasks.some(task=>task.state.status==='terminal'&&task.state.outcome.status==='aborted'))return 'cancelled' as const;
      // Pi deliberately completes ToolTask for a returned isError result.
      // Check public persisted ToolResultEntry across every conversation used
      // by this per-Run Harness; never return its transcript to the dispatcher.
      let entryCount=0;
      const conversations=new Set([run.binding.conversation_id as ConversationId,...tasks.map(task=>task.conversationId)]);
      for(const conversationId of conversations){
        let entryCursor;
        do{const page=await tx.scanEntries({conversationId},256,entryCursor);entryCount+=page.items.length;entryCursor=page.next;
          if(entryCount>1024||entryCursor&&entryCount>=1024)return 'unknown' as const;
          for(const entry of page.items)if(ToolResultEntry.is(entry)){
            if(entry.model?.some(message=>message.role==='toolResult'&&message.isError)
              ||entry.data.diagnostics.some(diagnostic=>diagnostic.severity==='error'))return 'failed' as const;
          }
        }while(entryCursor);
      }

      if(status.type!=='input'||status.status!=='done')return status.status==='unanswered'?'failed' as const:'unknown' as const;
      const answers=await tx.scanEntries({conversationId:run.binding.conversation_id as ConversationId,minEntryId:status.answer,maxEntryId:status.answer},1);
      const answer=answers.items[0],generation=tasks.find(task=>task.id===answer?.byTaskId&&task.kind.includes('generation'));
      if(!generation||generation.state.status!=='terminal'||generation.state.outcome.status!=='completed')return 'unknown' as const;
      return 'succeeded' as const;
    },BACKGROUND_CONTEXT);
    return {...this.receipt(run),submission_status:status.status,scheduling:inspection.scheduling,active_tasks:inspection.tasks.length,runtime_outcome:runtimeOutcome,persistence:{journal_mode:'wal',synchronous:2}};
  });}
  cancel(command:unknown):Promise<{run_id:string;status:'cancel_requested'}>{return this.serial(async()=>{
    const valid=validateRunCommand(command);await this.authorize(valid,'cancel');const run=await this.open(valid,false);this.check(run,valid);
    const conversation=await run.harness.conversation(run.binding.conversation_id as ConversationId,BACKGROUND_CONTEXT);
    if(!conversation)throw new RuntimeError('runtime_state_missing');
    await conversation.abort(BACKGROUND_CONTEXT);return {run_id:valid.run_id,status:'cancel_requested'};
  });}
  close(){return this.serial(async()=>{this.closed=true;for(const run of this.runs.values())if(run.timer)clearTimeout(run.timer);const results=await Promise.allSettled([...this.runs.values()].map(async run=>{await run.harness.close(BACKGROUND_CONTEXT);liveStorageOwners.delete(run.path);}));this.runs.clear();if(results.some(r=>r.status==='rejected'))throw new RuntimeError('runtime_close_failed');});}
}
