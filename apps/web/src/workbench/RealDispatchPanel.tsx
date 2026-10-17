/** The real dispatch switches on the settings page: the three layers for every member, the one-click controls for the owner only. */
import {useState} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {STATE_TEXT,WRITE_TEXT,WorkbenchError,kindOf,offers,readMe,readRealDispatchSwitches,writeKindOf,type Me} from './api';
import {DELIVER,apply,checkReason,describe,plan,realDispatch,type Change,type RealDispatch,type StepResult} from './realDispatch';

const onOff=(v:boolean|undefined)=>v===undefined?'未设置（关）':v?'开':'关';

export function RealDispatchPanel(){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const state=useQuery({queryKey:['workbench','real-dispatch'],queryFn:async()=>realDispatch(await readRealDispatchSwitches())});
  return <section className="real-dispatch" aria-labelledby="real-dispatch-title">
    <h2 id="real-dispatch-title">真实派发开关</h2>
    <p className="note">外部效应（真实企业交付等）只有三层同时打开才会派发：部署层、租户层、Action 层。</p>
    {state.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :state.isError?<p className="note" role="status">开关状态暂不可读：{STATE_TEXT[kindOf(state.error)]}</p>
      :<Layers state={state.data}/>}
    {state.data&&member.data&&offers(member.data,'set_real_dispatch')?<Controls state={state.data} member={member.data} onRead={()=>state.refetch()}/>
      :member.data?<p className="note">只读：只有负责人（owner）可以开关。</p>:null}
  </section>;
}

function Layers({state}:{state:RealDispatch}){
  const deliver=state.actions.find(a=>a.action_type===DELIVER);
  const others=state.actions.filter(a=>a.action_type!==DELIVER);
  return <table className="real-dispatch-layers"><thead><tr><th>层</th><th>状态</th><th>说明</th></tr></thead><tbody>
    <tr><td>部署层 REAL_DISPATCH_ENABLED</td><td><span className="rd-pill rd-unknown">未知</span></td><td>由部署层控制：APP_HOST 的 compose 环境变量，平时保持打开；需在 APP_HOST 部署层开启，工作台无法读取或修改。</td></tr>
    <tr><td>租户层</td><td><span className={'rd-pill '+(state.tenant?.enabled?'rd-on':'rd-off')}>{onOff(state.tenant?.enabled)}</span></td>
      <td>数据模式 {state.tenant?.data_mode??'—'} · 版本 {state.tenant?.revision??0}{state.tenant?.enabled&&state.tenant.data_mode!=='real'?'（test 模式按关闭处理）':''}</td></tr>
    <tr><td>Action 层 · enterprise.deliver:1</td><td><span className={'rd-pill '+(deliver?.enabled?'rd-on':'rd-off')}>{onOff(deliver?.enabled)}</span></td><td>版本 {deliver?.revision??0}</td></tr>
    {others.map(a=><tr key={a.action_type}><td>Action 层 · {a.action_type.replace(/^eios:action:(nexloop\.)?/,'')}</td>
      <td><span className={'rd-pill '+(a.enabled?'rd-on':'rd-off')}>{onOff(a.enabled)}</span></td><td>版本 {a.revision}</td></tr>)}
  </tbody></table>;
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
      <button type="button" onClick={()=>{setMode('open');setResults(null);}} disabled={busy}>一键开启</button>
      <button type="button" className="rd-close" onClick={()=>{setMode('close');setResults(null);}} disabled={busy}>一键关闭</button>
    </div>
    {mode?<div className="rd-confirm" role="dialog" aria-modal="false" aria-labelledby="rd-confirm-title">
      <h3 id="rd-confirm-title">{mode==='open'?'确认开启真实派发':'确认关闭全部真实派发开关'}</h3>
      {changes.length?<><p>将依次执行：</p><ol>{changes.map((c,i)=><li key={i}>{describe(c)}</li>)}</ol></>:<p className="note">当前状态已经是目标状态，不需要变更。</p>}
      {mode==='open'?<p className="rd-warning" role="note">部署层 REAL_DISPATCH_ENABLED 的状态工作台读不到：它平时保持打开；若部署层关闭，开启后也不会真实派发。</p>:null}
      <div className="field"><label htmlFor="rd-reason">原因（必填）</label>
        <textarea id="rd-reason" value={reason} onChange={e=>setReason(e.target.value)} maxLength={500} rows={2} required/></div>
      {reason&&problem?<p className="error">{problem}</p>:null}
      <div className="rd-buttons"><button type="button" onClick={()=>void confirm()} disabled={busy||!!problem||!changes.length}>{busy?'正在提交…':'确认执行'}</button>
        <button type="button" className="rd-close" onClick={()=>setMode(null)} disabled={busy}>取消</button></div>
    </div>:null}
    {results?<div className="rd-results" role="status"><h3>执行结果</h3><ul>{results.map((r,i)=><li key={i} className={r.ok?'rd-ok':'rd-fail'}>
      {describe(r.change)}：{r.ok?`完成，新版本 ${r.control_revision??'—'}`:`未完成（${r.kind}）：${WRITE_TEXT[r.kind]}`}</li>)}</ul>
      {results.some(r=>!r.ok)?<p className="note">已停止后续步骤，未自动重试；上方状态为重新读取后的结果。</p>:null}</div>:null}
  </div>;
}
