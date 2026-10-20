/**
 * Operator persona pages (design docs/design/operator-persona-design.md, owner decisions 2026-10-19). Every member reads the
 * configuration (0229); every change is an owner Action with a reason (0227/0228): publish or retire a persona version, the
 * tenant's default persona and segment rules, one customer's assignment, the AI disclosure. A version is immutable once
 * published: the editor is a local draft that publishes as the next version. The consumer detail block shows the customer's
 * persona@version with its source and their own communication preferences (owner only: they come from their statements).
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {PERSONA_SOURCE_TEXT,PREFERENCE_TEXT,kindOf,readContactPreferences,readOperatorPersonas,type AiDisclosure,type ContactPreference,type PersonaDocument,
  type PersonaOverview,type PersonaRule,type PersonaVersion} from '../api';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge,Tabs,Time} from '../ui';
import {Form,WithMember} from '../actions';
import {Page} from './Page';

type Tab='personas'|'rules'|'disclosure';
const lines=(value:string)=>value.split('\n').map(x=>x.trim()).filter(Boolean);
/** The highest published version of each persona: what a customer assigned to it gets now. */
export function usableVersions(personas:PersonaVersion[]):Map<string,PersonaVersion>{
  const out=new Map<string,PersonaVersion>();
  for(const p of personas)if(p.status==='published'&&(out.get(p.persona_id)?.version??0)<p.version)out.set(p.persona_id,p);
  return out;
}
export function personaRef(p:{persona_id:string;version:number}):string{return `${p.persona_id}@${p.version}`;}

export function Personas({actions}:{actions?:ReactNode}){
  const [tab,setTab]=useState<Tab>('personas');
  const query=useQuery({queryKey:['workbench','operator-personas'],queryFn:()=>readOperatorPersonas()});
  return <Page title="运营人设" query={query} actions={actions} toolbar={<Tabs label="人设设置" value={tab} onChange={setTab}
    tabs={[{key:'personas',label:'人设与版本'},{key:'rules',label:'分配'},{key:'disclosure',label:'AI 身份披露'}]}/>}>{data=>
    tab==='personas'?<PersonaVersions data={data}/>:tab==='rules'?<Assignment data={data}/>:<Disclosure setting={data.ai_disclosure}/>}</Page>;
}

// ---- versions and the editor ----
function PersonaVersions({data}:{data:PersonaOverview}){
  const rows=[...data.personas].sort((a,b)=>a.persona_id.localeCompare(b.persona_id)||b.version-a.version);
  return <>
    <p className="note">人设是消费者看到的“运营”的身份与说话方式。版本一经发布不可修改；Agent 不能修改人设。</p>
    {rows.length?<DataTable label="人设版本" rows={rows} rowKey={p=>personaRef(p)} columns={[
      {label:'人设',main:true,cell:p=><>{p.document.display_name} <code>{personaRef(p)}</code></>},
      {label:'状态',cell:p=><StatusBadge text={p.status==='published'?'已发布':'已停用'} tone={p.status==='published'?'ok':'neutral'} code={p.status}/>},
      {label:'语气',wide:true,cell:p=>p.document.tone},
      {label:'发布时间',cell:p=><Time value={p.created_at}/>},
      {label:'操作',cell:p=><WithMember>{member=><Form operation="publish_operator_persona" member={member} title={p.status==='published'?'停用':'重新启用'}
        className="inline-action" body={()=>p.status==='published'
          ?{persona_id:p.persona_id,version:p.version,status:'retired',document:null,reason:'负责人停用'}
          :{persona_id:p.persona_id,version:p.version,status:'published',document:p.document,reason:'负责人重新启用'}}><span/></Form>}</WithMember>}]}/>
      :<Empty kind="none" title="还没有人设" next="负责人在下方编辑并发布第一个人设；在此之前，消费者对话不带人设。"/>}
    <WithMember>{member=><PersonaEditor member={member} personas={data.personas}/>}</WithMember>
  </>;
}

type Draft={persona_id:string;display_name:string;signature:string;self_intro:string;tone:string;values:string;max_chars:string;emoji:PersonaDocument['expression']['emoji'];
  vocabulary:string;forbidden:string;language:string;scenarios:string;not_for:string;statement:string;deflect:string;reason:string};
function draftOf(id:string,doc?:PersonaDocument):Draft{
  return {persona_id:id,display_name:doc?.display_name??'',signature:doc?.signature??'',self_intro:doc?.self_intro??'',tone:doc?.tone??'',values:(doc?.values??[]).join('\n'),
    max_chars:String(doc?.expression.max_chars??200),emoji:doc?.expression.emoji??'none',vocabulary:doc?.expression.vocabulary??'',
    forbidden:(doc?.expression.forbidden_phrases??[]).join('\n'),language:doc?.expression.language??'',scenarios:(doc?.scenarios??[]).join('\n'),
    not_for:(doc?.not_for??[]).join('\n'),statement:doc?.ai_disclosure?.statement??'',deflect:doc?.ai_disclosure?.deflect_statement??'',reason:''};
}
/** The document a draft publishes (empty optional fields left out, as the contract allows). */
export function documentOf(d:Draft):PersonaDocument{
  const doc:PersonaDocument={display_name:d.display_name.trim(),tone:d.tone.trim(),expression:{max_chars:Number(d.max_chars),emoji:d.emoji}};
  if(d.signature.trim())doc.signature=d.signature.trim();if(d.self_intro.trim())doc.self_intro=d.self_intro.trim();
  if(lines(d.values).length)doc.values=lines(d.values);if(lines(d.scenarios).length)doc.scenarios=lines(d.scenarios);if(lines(d.not_for).length)doc.not_for=lines(d.not_for);
  if(d.vocabulary.trim())doc.expression.vocabulary=d.vocabulary.trim();if(d.language.trim())doc.expression.language=d.language.trim();
  if(lines(d.forbidden).length)doc.expression.forbidden_phrases=lines(d.forbidden);
  if(d.statement.trim()||d.deflect.trim())doc.ai_disclosure={...(d.statement.trim()?{statement:d.statement.trim()}:{}),...(d.deflect.trim()?{deflect_statement:d.deflect.trim()}:{})};
  return doc;
}
function PersonaEditor({member,personas}:{member:Parameters<typeof Form>[0]['member'];personas:PersonaVersion[]}){
  const ids=[...new Set(personas.map(p=>p.persona_id))].sort();
  const latest=(id:string)=>personas.filter(p=>p.persona_id===id).sort((a,b)=>b.version-a.version)[0];
  const [base,setBase]=useState(ids[0]??'');
  const [draft,setDraft]=useState<Draft>(()=>draftOf(ids[0]??'',ids[0]?latest(ids[0])?.document:undefined));
  const set=(key:keyof Draft)=>(value:string)=>setDraft({...draft,[key]:value});
  const next=(latest(draft.persona_id)?.version??0)+1;
  const idOk=/^[a-z][a-z0-9_-]{0,63}$/.test(draft.persona_id);
  function pick(id:string){setBase(id);setDraft(draftOf(id,id?latest(id)?.document:undefined));}
  const field=(key:keyof Draft,label:string,opts:{area?:boolean;hint?:string;max?:number}={})=><div className="field"><label htmlFor={'persona-'+key}>{label}</label>
    {opts.area?<textarea id={'persona-'+key} value={draft[key]} onChange={e=>set(key)(e.target.value)}/>
      :<input id={'persona-'+key} value={draft[key]} maxLength={opts.max} onChange={e=>set(key)(e.target.value)}/>}{opts.hint?<p className="note">{opts.hint}</p>:null}</div>;
  return <Form operation="publish_operator_persona" member={member} title={`发布为 ${idOk?draft.persona_id:'新人设'}@${next}`} className="persona-editor"
    disabled={!idOk||!draft.display_name.trim()||!draft.tone.trim()||!draft.reason.trim()}
    body={()=>({persona_id:draft.persona_id,version:next,status:'published',document:documentOf(draft),reason:draft.reason.trim()})}>
    <p className="note">草稿只保存在本页，发布后成为新版本，旧版本不变。</p>
    <div className="field"><label htmlFor="persona-base">从哪个人设开始</label><select id="persona-base" value={base} onChange={e=>pick(e.target.value)}>
      {ids.map(id=><option key={id} value={id}>{latest(id)?.document.display_name} · {id}</option>)}<option value="">新人设</option></select></div>
    {base===''?field('persona_id','人设标识',{hint:'小写字母开头，可含数字、- 和 _，最长 64 个字符；发布后不可更改。',max:64}):null}
    {field('display_name','名字',{max:40})}{field('signature','署名',{max:40})}{field('self_intro','自我介绍',{area:true})}
    {field('tone','语气',{max:200,hint:'例如：亲切、简洁'})}{field('values','价值观（每行一条，最多 5 条）',{area:true})}
    <div className="field"><label htmlFor="persona-max_chars">单条消息字数上限</label><input id="persona-max_chars" type="number" min={20} max={2000} value={draft.max_chars}
      onChange={e=>set('max_chars')(e.target.value)}/></div>
    <div className="field"><label htmlFor="persona-emoji">表情</label><select id="persona-emoji" value={draft.emoji} onChange={e=>set('emoji')(e.target.value)}>
      <option value="none">不用</option><option value="few">少用</option><option value="allowed">可用</option></select></div>
    {field('vocabulary','用词与称呼',{area:true})}{field('forbidden','禁用说法（每行一条）',{area:true})}{field('language','语言',{max:16})}
    {field('scenarios','适用场景（每行一条）',{area:true})}{field('not_for','不适用场景（每行一条）',{area:true})}
    {field('statement','表明 AI 身份时的话术',{area:true,hint:'只在“AI 身份披露”设为表明时使用。'})}
    {field('deflect','被问到是否 AI 时的回避话术',{area:true,hint:'“被直接询问时”选择回避时使用；为空时转人工。'})}
    {field('reason','发布原因',{max:500})}
  </Form>;
}

// ---- assignment: default, rules, one customer ----
function ruleText(rule:PersonaRule):string{return 'role_id' in rule.when?`拥有角色 ${rule.when.role_id.slice(0,12)}…`:`属性 ${rule.when.property} = ${rule.when.equals}`;}
type RuleDraft={kind:'role_id'|'property';role_id:string;property:string;equals:string;persona_id:string};
function Assignment({data}:{data:PersonaOverview}){
  const usable=[...usableVersions(data.personas).values()];
  const name=(id:string|null)=>id?(usable.find(p=>p.persona_id===id)?.document.display_name??id)+` (${id})`:'无';
  return <>
    <h2>当前分配</h2>
    <p>顺序：负责人对单个客户的指定 → 分群规则（第一条命中的生效）→ 租户默认。同一客户的人设保持稳定，每次更换都留下记录。</p>
    {data.rules?<><p>规则版本 v{data.rules.version} · 租户默认：{name(data.rules.default_persona_id)} · 发布于 <Time value={data.rules.published_at}/></p>
      {data.rules.rules.length?<DataTable label="分群规则" rows={data.rules.rules.map((r,i)=>({...r,i}))} rowKey={r=>String(r.i)} columns={[
        {label:'顺序',num:true,main:true,cell:r=>r.i+1},{label:'条件',wide:true,cell:r=>ruleText(r)},{label:'人设',cell:r=>name(r.persona_id)}]}/>
        :<Empty kind="none" title="没有分群规则" next="所有客户使用租户默认人设。"/>}</>
      :<Empty kind="none" title="还没有发布分配规则" next="发布规则并设置租户默认人设后，客户对话才会带上人设。"/>}
    <WithMember>{member=><>
      <RulesEditor member={member} data={data} usable={usable}/>
      <AssignForm member={member} usable={usable}/></>}</WithMember>
  </>;
}
function RulesEditor({member,data,usable}:{member:Parameters<typeof Form>[0]['member'];data:PersonaOverview;usable:PersonaVersion[]}){
  const [fallback,setFallback]=useState(data.rules?.default_persona_id??'');
  const [rows,setRows]=useState<RuleDraft[]>(()=>(data.rules?.rules??[]).map(r=>'role_id' in r.when?{kind:'role_id' as const,role_id:r.when.role_id,property:'',equals:'',persona_id:r.persona_id}
    :{kind:'property' as const,role_id:'',property:r.when.property,equals:r.when.equals,persona_id:r.persona_id}));
  const [reason,setReason]=useState('');
  const next=(data.rules?.version??0)+1;
  const ok=rows.every(r=>r.persona_id&&(r.kind==='role_id'?/^[a-f0-9]{64}$/.test(r.role_id):/^[a-z][a-z0-9_]{0,63}$/.test(r.property)&&r.equals.trim()));
  const change=(i:number,patch:Partial<RuleDraft>)=>setRows(rows.map((r,j)=>j===i?{...r,...patch}:r));
  const options=<>{usable.map(p=><option key={p.persona_id} value={p.persona_id}>{p.document.display_name} · {personaRef(p)}</option>)}</>;
  return <Form operation="publish_persona_rules" member={member} title={`发布分配规则 v${next}`} disabled={!ok||!reason.trim()}
    body={()=>({version:next,default_persona_id:fallback||null,reason:reason.trim(),
      rules:rows.map(r=>({when:r.kind==='role_id'?{role_id:r.role_id}:{property:r.property,equals:r.equals.trim()},persona_id:r.persona_id}))})}>
    <div className="field"><label htmlFor="rules-default">租户默认人设</label><select id="rules-default" value={fallback} onChange={e=>setFallback(e.target.value)}>
      <option value="">不设默认</option>{options}</select></div>
    {rows.map((r,i)=><fieldset key={i} className="rule-row"><legend>规则 {i+1}</legend>
      <select aria-label={`规则 ${i+1} 条件类型`} value={r.kind} onChange={e=>change(i,{kind:e.target.value as RuleDraft['kind']})}>
        <option value="role_id">客户拥有某个角色</option><option value="property">客户属性等于</option></select>
      {r.kind==='role_id'?<input aria-label={`规则 ${i+1} 角色 ID`} placeholder="角色 ID（64 位）" value={r.role_id} onChange={e=>change(i,{role_id:e.target.value.trim()})}/>
        :<><input aria-label={`规则 ${i+1} 属性名`} placeholder="属性名" value={r.property} onChange={e=>change(i,{property:e.target.value.trim()})}/>
          <input aria-label={`规则 ${i+1} 属性值`} placeholder="值" value={r.equals} onChange={e=>change(i,{equals:e.target.value})}/></>}
      <select aria-label={`规则 ${i+1} 人设`} value={r.persona_id} onChange={e=>change(i,{persona_id:e.target.value})}><option value="">选择人设</option>{options}</select>
      <button type="button" onClick={()=>setRows(rows.filter((_,j)=>j!==i))}>删除这条</button></fieldset>)}
    <button type="button" onClick={()=>setRows([...rows,{kind:'property',role_id:'',property:'',equals:'',persona_id:''}])}>添加规则</button>
    <div className="field"><label htmlFor="rules-reason">发布原因</label><input id="rules-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></div>
    <p className="note">新规则发布后，受影响的客户在下次对话时更换人设，每次更换都留下记录。</p>
  </Form>;
}
function AssignForm({member,usable}:{member:Parameters<typeof Form>[0]['member'];usable:PersonaVersion[]}){
  const [consumer,setConsumer]=useState('');const [persona,setPersona]=useState('');const [reason,setReason]=useState('');
  return <Form operation="assign_operator_persona" member={member} title="为单个客户指定人设" disabled={!/^[a-f0-9]{64}$/.test(consumer)||!reason.trim()}
    body={()=>({consumer_id:consumer,persona_id:persona||null,reason:reason.trim()})}>
    <div className="field"><label htmlFor="assign-consumer">客户 ID</label><input id="assign-consumer" value={consumer} onChange={e=>setConsumer(e.target.value.trim())} placeholder="64 位客户 ID"/></div>
    <div className="field"><label htmlFor="assign-persona">人设</label><select id="assign-persona" value={persona} onChange={e=>setPersona(e.target.value)}>
      <option value="">清除指定（回到规则）</option>{usable.map(p=><option key={p.persona_id} value={p.persona_id}>{p.document.display_name} · {personaRef(p)}</option>)}</select></div>
    <div className="field"><label htmlFor="assign-reason">原因</label><input id="assign-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></div>
  </Form>;
}

// ---- AI disclosure ----
const MODE_TEXT:Record<AiDisclosure['mode'],string>={none:'不主动表明',first_contact:'首次接触时表明一次',always:'每次会话开始时表明'};
function Disclosure({setting}:{setting:AiDisclosure}){
  return <>
    <dl className="facts"><div><dt>主动表明</dt><dd>{MODE_TEXT[setting.mode]}</dd></div>
      <div><dt>被直接询问是否 AI 时</dt><dd>{setting.when_asked==='deflect'?'用人设里的回避话术回答（话术为空时转人工）':'转人工'}</dd></div>
      <div><dt>拦截“自称人类”的说法</dt><dd>{setting.block_human_claims?'拦截':'不拦截'}</dd></div>
      <div><dt>消息加“AI 生成”标识</dt><dd>{setting.content_label?'加':'不加'}</dd></div>
      <div><dt>配置版本</dt><dd>{setting.revision||'默认值'}</dd></div></dl>
    <p className="note">是否让消费者知道是 AI 由使用方决定；不同法域对 AI 生成内容的标识有不同要求，合规责任由使用方承担。系统只提供开关与话术配置，并记录每次处理。</p>
    <WithMember>{member=><DisclosureForm member={member} setting={setting}/>}</WithMember>
  </>;
}
function DisclosureForm({member,setting}:{member:Parameters<typeof Form>[0]['member'];setting:AiDisclosure}){
  const [mode,setMode]=useState(setting.mode);const [asked,setAsked]=useState(setting.when_asked);
  const [block,setBlock]=useState(setting.block_human_claims);const [label,setLabel]=useState(setting.content_label);const [reason,setReason]=useState('');
  return <Form operation="set_ai_disclosure" member={member} title="保存 AI 身份披露配置" disabled={!reason.trim()}
    body={()=>({mode,when_asked:asked,block_human_claims:block,content_label:label,reason:reason.trim()})}>
    <div className="field"><label htmlFor="ai-mode">主动表明</label><select id="ai-mode" value={mode} onChange={e=>setMode(e.target.value as AiDisclosure['mode'])}>
      {Object.entries(MODE_TEXT).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
    <fieldset className="choices"><legend>被直接询问是否 AI 时</legend>
      <label className="choice"><input type="radio" name="ai-asked" checked={asked==='deflect'} onChange={()=>setAsked('deflect')}/>用回避话术回答</label>
      <label className="choice"><input type="radio" name="ai-asked" checked={asked==='handoff'} onChange={()=>setAsked('handoff')}/>转人工</label></fieldset>
    <label className="choice"><input type="checkbox" checked={block} onChange={e=>setBlock(e.target.checked)}/>拦截“我是真人”一类自称人类的说法</label>
    <label className="choice"><input type="checkbox" checked={label} onChange={e=>setLabel(e.target.checked)}/>在消息上加“AI 生成”标识（渠道支持时）</label>
    <div className="field"><label htmlFor="ai-reason">原因</label><input id="ai-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></div>
  </Form>;
}

// ---- consumer detail: persona@version, its source, the customer's own preferences ----
export function ConsumerPersona({consumerId}:{consumerId:string}){
  const overview=useQuery({queryKey:['workbench','operator-personas',consumerId],queryFn:()=>readOperatorPersonas(consumerId)});
  const prefs=useQuery({queryKey:['workbench','contact-preferences',consumerId],queryFn:()=>readContactPreferences(consumerId),retry:false});
  return <section aria-labelledby="consumer-persona-title"><h2 id="consumer-persona-title">运营人设与沟通偏好</h2>
    {overview.isPending?<p role="status">正在加载…</p>:overview.isError?<Empty kind={kindOf(overview.error)==='forbidden'?'forbidden':'unavailable'}/>
      :<PersonaLine data={overview.data}/>}
    <h3>这位客户自己说过的沟通偏好</h3>
    {prefs.isPending?<p role="status">正在加载…</p>:prefs.isError?<Empty kind={kindOf(prefs.error)==='forbidden'?'forbidden':'unavailable'}
      reason={kindOf(prefs.error)==='forbidden'?'只有负责人可以查看客户的沟通偏好':undefined}/>
      :prefs.data.length?<DataTable label="沟通偏好" rows={prefs.data} rowKey={p=>p.claim_id} columns={[
        {label:'偏好',main:true,cell:p=>PREFERENCE_TEXT[p.dimension]},{label:'内容',wide:true,cell:p=>preferenceText(p)},
        {label:'状态',cell:p=><StatusBadge text={p.dismissed?'不采纳':'采纳'} tone={p.dismissed?'neutral':'ok'} code={p.dismissed?'dismissed':'adopted'}/>},
        {label:'操作',cell:p=><WithMember>{member=><Form operation="dismiss_contact_preference" member={member} className="inline-action" title={p.dismissed?'恢复采纳':'不采纳'}
          body={()=>({claim_id:p.claim_id,dismissed:!p.dismissed,reason:p.dismissed?'负责人恢复采纳':'负责人不采纳'})}><span/></Form>}</WithMember>}]}/>
      :<Empty kind="none" title="没有沟通偏好" next="客户说出联系时段、频率、渠道、主题或语气方面的要求后，会出现在这里。"/>}
    <p className="note">时段、频率、渠道、主题在派发时强制执行，只会比租户规则更严；语气只供对话参考。负责人只能不采纳，不能替客户新增偏好。</p>
  </section>;
}
function preferenceText(p:ContactPreference):string{
  const v=p.dimension==='frequency'?`${p.value} 次`:String(p.value);
  return p.polarity==='negated'?(p.dimension==='topic'?`不要：${v}`:`不要用：${v}`):p.dimension==='channel'?`只用：${v}`:v;
}
function PersonaLine({data}:{data:PersonaOverview}){
  const usable=usableVersions(data.personas);const c=data.consumer;
  if(!c?.settled&&!c?.desired?.persona_id)return <Empty kind="none" title="当前没有人设" next="租户还没有可用的人设或分配规则；对话不带人设。"/>;
  const now=c.settled?usable.get(c.settled.persona_id):undefined;const next=c.desired?.persona_id?usable.get(c.desired.persona_id):undefined;
  const changes=c.desired?.persona_id&&(c.desired.persona_id!==c.settled?.persona_id||c.desired.source!==c.settled?.source);
  return <dl className="facts">
    <div><dt>当前生效</dt><dd>{c.settled?<>{now?.document.display_name??c.settled.persona_id} <code>{now?personaRef(now):c.settled.persona_id+'（已停用）'}</code> · {PERSONA_SOURCE_TEXT[c.settled.source]}</>:'尚未在对话中使用'}</dd></div>
    {changes&&c.desired?.persona_id&&c.desired.source?<div><dt>下次对话起</dt><dd>{next?.document.display_name??c.desired.persona_id} <code>{next?personaRef(next):c.desired.persona_id}</code> · {PERSONA_SOURCE_TEXT[c.desired.source]}</dd></div>:null}
  </dl>;
}
