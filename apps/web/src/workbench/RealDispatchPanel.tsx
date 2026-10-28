/** The real dispatch switches on the settings page: the three layers for every member, the one-click controls for the owner only. */
import {useState,type ReactNode} from 'react';
import {DataTable} from './DataTable';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {STATE_TEXT,WRITE_TEXT,WorkbenchError,kindOf,offers,readMe,readRealDispatchSwitches,writeKindOf,type Me} from './api';
import {statusLabel} from './status-labels';
import {EmptyState,FormField} from './kit';
import {DELIVER,apply,checkReason,describe,plan,realDispatch,type Change,type RealDispatch,type StepResult} from './realDispatch';

const onOff=(v:boolean|undefined)=>v===undefined?'未设置（关）':v?'开':'关';

export function RealDispatchPanel(){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const state=useQuery({queryKey:['workbench','real-dispatch'],queryFn:async()=>realDispatch(await readRealDispatchSwitches())});
  return <section className="real-dispatch" aria-labelledby="real-dispatch-title">
    <h2 id="real-dispatch-title">真实派发开关</h2>
    <p className="note">外部效应（真实企业交付等）只有三层同时打开才会派发：部署层、租户层、Action 层。</p>
    {state.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :state.isError?<EmptyState kind="unavailable" subject="开关状态" reason={STATE_TEXT[kindOf(state.error)]} onRetry={()=>void state.refetch()}/>
      :<Layers state={state.data}/>}
    {state.data?<Outcome state={state.data}/>:null}
    {state.data&&member.data&&offers(member.data,'set_real_dispatch')?<Controls state={state.data} member={member.data} onRead={()=>state.refetch()}/>
      :member.data?<p className="note">只读：只有负责人可以开关。</p>:null}
  </section>;
}

/** F06: the end result in one line. The deployment layer is not readable here, so "all open" is never claimed. */
export function outcomeText(state:RealDispatch):{dispatchable:'unconfirmed'|'no';text:string}{
  const deliver=state.actions.find(a=>a.action_type===DELIVER);
  const tenant=!!state.tenant?.enabled&&state.tenant.data_mode==='real';
  if(tenant&&deliver?.enabled)return {dispatchable:'unconfirmed',text:'真实交付：无法确认。租户层与交付 Action 层已开启；部署层工作台读不到，部署层也开启时才会派发。'};
  return {dispatchable:'no',text:`真实交付：不可派发（${[!tenant?'租户层未开启':'',!deliver?.enabled?'交付 Action 层未开启':''].filter(Boolean).join('，')}）。`};
}
function Outcome({state}:{state:RealDispatch}){
  const o=outcomeText(state);
  return <p className={'rd-outcome '+(o.dispatchable==='no'?'note':'warning')} role="status" data-dispatchable={o.dispatchable}>{o.text}</p>;
}

/** The three layers as a table (also on the capability catalog, from the same source). */
export function Layers({state}:{state:RealDispatch}){
  const deliver=state.actions.find(a=>a.action_type===DELIVER);
  const others=state.actions.filter(a=>a.action_type!==DELIVER);
  const pill=(on:boolean|undefined)=><span className={'rd-pill '+(on?'rd-on':'rd-off')}>{onOff(on)}</span>;
  const rows:{layer:string;status:ReactNode;note:ReactNode}[]=[
    {layer:'部署层 REAL_DISPATCH_ENABLED',status:<span className="rd-pill rd-unknown">未知</span>,note:<>由部署层控制（APP_HOST 的 compose 环境变量），平时保持打开；需在 APP_HOST 部署层开启，工作台无法读取或修改。</>},
    {layer:'租户层',status:pill(state.tenant?.enabled),note:`数据模式：${state.tenant?.data_mode?statusLabel('data_mode',state.tenant.data_mode):'—'} · 版本 ${state.tenant?.revision??0}${state.tenant?.enabled&&state.tenant.data_mode!=='real'?'（测试模式按关闭处理）':''}`},
    {layer:'Action 层 · enterprise.deliver:1',status:pill(deliver?.enabled),note:`版本 ${deliver?.revision??0}`},
    ...others.map(a=>({layer:`Action 层 · ${a.action_type.replace(/^eios:action:(nexloop\.)?/,'')}`,status:pill(a.enabled),note:`版本 ${a.revision}`}))];
  return <DataTable className="real-dispatch-layers" label="真实派发三层开关" rows={rows} rowKey={r=>r.layer} columns={[
    {label:'层',main:true,cell:r=>r.layer},{label:'状态',cell:r=>r.status},{label:'说明',wide:true,cell:r=>r.note}]}/>;
}

function Controls({state,onRead}:{state:RealDispatch;member:Me;onRead:()=>Promise<unknown>}){
  const cache=useQueryClient();
  const [mode,setMode]=useState<'open'|'close'|null>(null);const [reason,setReason]=useState('');
  const [busy,setBusy]=useState(false);const [results,setResults]=useState<StepResult[]|null>(null);
  const changes:Change[]=mode?plan(state,mode):[];const problem=checkReason(reason);
  async function confirm(){
    if(!mode||problem||!changes.length)return;
    setBusy(true);
    // Like the script: plan from the state read right before the requests (another owner may have changed a switch meanwhile).
    try{const fresh=realDispatch(await readRealDispatchSwitches());setResults(await apply(plan(fresh,mode),reason));}
    catch(error){setResults([{change:changes[0],ok:false,kind:error instanceof WorkbenchError?'invalid':writeKindOf(error)}]);}
    finally{await cache.invalidateQueries({queryKey:['workbench']});await onRead();setBusy(false);setMode(null);}   // the state after is read again, whatever happened
  }
  return <div className="real-dispatch-controls">
    <div className="rd-buttons">
      <button type="button" className="primary" onClick={()=>{setMode('open');setResults(null);}} disabled={busy}>开启租户层与交付 Action 两层</button>
      <button type="button" className="rd-close" onClick={()=>{setMode('close');setResults(null);}} disabled={busy}>关闭租户层与全部 Action 层</button>
    </div>
    {mode?<div className="rd-confirm" role="dialog" aria-modal="false" aria-labelledby="rd-confirm-title">
      <h3 id="rd-confirm-title">{mode==='open'?'确认开启租户层与交付 Action 两层':'确认关闭租户层与全部 Action 层'}</h3>
      {changes.length?<><p>将依次执行：</p><ol>{changes.map((c,i)=><li key={i}>{describe(c)}</li>)}</ol></>:<p className="note">当前状态已经是目标状态，不需要变更。</p>}
      <p className="impact-summary" role="note">影响：只改变上面列出的{changes.length}个开关；部署层不在此变更。完成后{mode==='open'?'真实交付状态为“无法确认”：部署层也开启时才会派发':'真实交付不可派发'}。</p>
      {mode==='open'?<p className="rd-warning" role="note">部署层 REAL_DISPATCH_ENABLED 的状态工作台读不到：它平时保持打开；若部署层关闭，开启后也不会真实派发。</p>:null}
      <FormField id="rd-reason" label="原因（必填）" error={reason&&problem?problem:undefined}>
        <textarea id="rd-reason" value={reason} onChange={e=>setReason(e.target.value)} maxLength={500} rows={2} required aria-describedby={reason&&problem?'rd-reason-error':undefined}/></FormField>
      <div className="rd-buttons"><button type="button" className={mode==='close'?'btn-danger':'primary'} onClick={()=>void confirm()} disabled={busy||!!problem||!changes.length}>{busy?'正在提交…':'确认执行'}</button>
        <button type="button" className="btn-secondary" onClick={()=>setMode(null)} disabled={busy}>取消</button></div>
    </div>:null}
    {results?<div className="rd-results" role="status"><h3>执行结果</h3><ul>{results.map((r,i)=><li key={i} className={r.ok?'rd-ok':'rd-fail'}>
      {describe(r.change)}：{r.ok?`完成，新版本 ${r.control_revision??'—'}`:<span title={r.kind}>未完成：{WRITE_TEXT[r.kind]}{r.detail?<>{' '}<span className="refusal-reason">{r.detail}</span></>:null}</span>}</li>)}</ul>
      {results.some(r=>!r.ok)?<p className="note">已停止后续步骤，未自动重试；上方状态为重新读取后的结果。</p>:null}</div>:null}
  </div>;
}
