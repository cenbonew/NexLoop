/**
 * Operator persona pages (design docs/design/operator-persona-design.md, owner decisions 2026-10-19). Every member reads the
 * configuration (0229); every change is an owner Action with a reason (0227/0228): publish or retire a persona version, the
 * tenant's default persona and segment rules, one customer's assignment, the AI disclosure. A version is immutable once
 * published: the editor is a local draft that publishes as the next version. The consumer detail block shows the customer's
 * persona@version with its source and their own communication preferences (owner only: they come from their statements).
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {PERSONA_SOURCE_TEXT,PREFERENCE_TEXT,kindOf,readContactPreferences,readMe,readOperatorPersonas,readOutreachControls,readOutreachApprovals,readOutreachPurposes,type ConsentMode,type OutreachSettings,type AiDisclosure,type ContactPreference,type PersonaDocument,
  type PersonaOverview,type PersonaRule,type PersonaVersion} from '../api';
import {DataTable} from '../DataTable';
import {StatusBadge,TabPanel,Tabs,Time} from '../ui';
import {CheckboxGroup,ChoiceGroup,EmptyState,FormField,IdText,KeyValue,shorten} from '../kit';
import {ConfirmedForm,Form,WithMember} from '../actions';
import {Page} from './Page';
import {OutreachInsights} from './OutreachInsights';

type Tab='personas'|'rules'|'disclosure'|'outreach'|'insights';
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
  // 0232: the outreach controls tab is the owner's only (the read and the Action are owner only).
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const owner=member.data?.role==='owner';
  const tabs:{key:Tab;label:string}[]=[{key:'personas',label:'人设与版本'},{key:'rules',label:'分配'},{key:'disclosure',label:'AI 身份披露'},...(owner?[{key:'outreach' as Tab,label:'外联控制'},{key:'insights' as Tab,label:'外联指标与证据'}]:[])];
  return <Page title="运营人设" query={query} actions={actions} lead="运营在对话中的身份与说话方式：人设版本、分配规则与 AI 身份披露。"
    toolbar={<Tabs id="persona-tabs" label="人设设置" value={tab} onChange={setTab} tabs={tabs}/>}>{data=><TabPanel id="persona-tabs" value={tab}>{
    tab==='personas'?<PersonaVersions data={data}/>:tab==='rules'?<Assignment data={data}/>:tab==='outreach'&&owner?<OutreachControlsTab/>:tab==='insights'&&owner?<OutreachInsights/>:<Disclosure setting={data.ai_disclosure}/>}</TabPanel>}</Page>;
}

// ---- versions and the editor ----
function PersonaVersions({data}:{data:PersonaOverview}){
  const rows=[...data.personas].sort((a,b)=>a.persona_id.localeCompare(b.persona_id)||b.version-a.version);
  return <>
    <p className="note">人设是客户看到的“运营”的身份与说话方式。版本一经发布不可修改；Agent 不能修改人设。</p>
    {rows.length?<DataTable label="人设版本" rows={rows} rowKey={p=>personaRef(p)} columns={[
      {label:'人设',main:true,cell:p=><>{p.document.display_name} <code>{personaRef(p)}</code></>},
      {label:'状态',cell:p=><StatusBadge domain="persona_status" code={p.status}/>},
      {label:'语气',wide:true,cell:p=>p.document.tone},
      {label:'发布时间',cell:p=><Time value={p.created_at}/>},
      {label:'操作',cell:p=><WithMember>{member=>p.status==='published'
        ?<ConfirmedForm operation="publish_operator_persona" member={member} tone="danger" trigger="停用…" title={`确认停用 ${personaRef(p)}`}
          impact="影响：分配到这个版本的客户下次对话起不再使用它（按分配规则或租户默认人设）；可以再重新启用。"
          body={reason=>({persona_id:p.persona_id,version:p.version,status:'retired',document:null,reason})}/>
        :<ConfirmedForm operation="publish_operator_persona" member={member} trigger="重新启用…" title={`确认重新启用 ${personaRef(p)}`}
          impact="影响：这个版本重新可被分配；已分配的客户下次对话起使用它。"
          body={reason=>({persona_id:p.persona_id,version:p.version,status:'published',document:p.document,reason})}/>}</WithMember>}]}/>
      :<EmptyState kind="empty" subject="人设" next="负责人在下方编辑并发布第一个人设；在此之前，客户对话不带人设。"/>}
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
  const field=(key:keyof Draft,label:string,opts:{area?:boolean;hint?:string;max?:number}={})=><FormField id={'persona-'+key} label={label} hint={opts.hint}>
    {opts.area?<textarea id={'persona-'+key} value={draft[key]} aria-describedby={opts.hint?'persona-'+key+'-hint':undefined} onChange={e=>set(key)(e.target.value)}/>
      :<input id={'persona-'+key} value={draft[key]} maxLength={opts.max} aria-describedby={opts.hint?'persona-'+key+'-hint':undefined} onChange={e=>set(key)(e.target.value)}/>}</FormField>;
  return <Form operation="publish_operator_persona" member={member} title={`发布为 ${idOk?draft.persona_id:'新人设'}@${next}`} className="persona-editor"
    disabled={!idOk||!draft.display_name.trim()||!draft.tone.trim()||!draft.reason.trim()}
    body={()=>({persona_id:draft.persona_id,version:next,status:'published',document:documentOf(draft),reason:draft.reason.trim()})}>
    <p className="note">草稿只保存在本页，发布后成为新版本，旧版本不变。</p>
    <FormField id="persona-base" label="从哪个人设开始"><select id="persona-base" value={base} onChange={e=>pick(e.target.value)}>
      {ids.map(id=><option key={id} value={id}>{latest(id)?.document.display_name} · {id}</option>)}<option value="">新人设</option></select></FormField>
    {base===''?field('persona_id','人设标识',{hint:'小写字母开头，可含数字、- 和 _，最长 64 个字符；发布后不可更改。',max:64}):null}
    {field('display_name','名字',{max:40})}{field('signature','署名',{max:40})}{field('self_intro','自我介绍',{area:true})}
    {field('tone','语气',{max:200,hint:'例如：亲切、简洁'})}{field('values','价值观（每行一条，最多 5 条）',{area:true})}
    <FormField id="persona-max_chars" label="单条消息字数上限" width="sm"><input id="persona-max_chars" type="number" min={20} max={2000} value={draft.max_chars}
      onChange={e=>set('max_chars')(e.target.value)}/></FormField>
    <FormField id="persona-emoji" label="表情" width="sm"><select id="persona-emoji" value={draft.emoji} onChange={e=>set('emoji')(e.target.value)}>
      <option value="none">不用</option><option value="few">少用</option><option value="allowed">可用</option></select></FormField>
    {field('vocabulary','用词与称呼',{area:true})}{field('forbidden','禁用说法（每行一条）',{area:true})}{field('language','语言',{max:16})}
    {field('scenarios','适用场景（每行一条）',{area:true})}{field('not_for','不适用场景（每行一条）',{area:true})}
    {field('statement','表明 AI 身份时的话术',{area:true,hint:'只在“AI 身份披露”设为表明时使用。'})}
    {field('deflect','被问到是否 AI 时的回避话术',{area:true,hint:'“被直接询问时”选择回避时使用；为空时转人工。'})}
    {field('reason','发布原因',{max:500})}
  </Form>;
}

// ---- assignment: default, rules, one customer ----
function ruleText(rule:PersonaRule):string{return 'role_id' in rule.when?`拥有角色 ${shorten(rule.when.role_id)}`:`属性 ${rule.when.property} 等于“${rule.when.equals}”`;}
type RuleDraft={kind:'role_id'|'property';role_id:string;property:string;equals:string;persona_id:string};
function Assignment({data}:{data:PersonaOverview}){
  const usable=[...usableVersions(data.personas).values()];
  const name=(id:string|null)=>id?(usable.find(p=>p.persona_id===id)?.document.display_name??id)+`（${id}）`:'无';
  return <>
    <h2>当前分配</h2>
    <p>顺序：负责人对单个客户的指定 → 分群规则（第一条命中的生效）→ 租户默认。同一客户的人设保持稳定，每次更换都留下记录。</p>
    {data.rules?<><p>规则版本 v{data.rules.version} · 租户默认：{name(data.rules.default_persona_id)} · 发布于 <Time value={data.rules.published_at}/></p>
      {data.rules.rules.length?<DataTable label="分群规则" rows={data.rules.rules.map((r,i)=>({...r,i}))} rowKey={r=>String(r.i)} columns={[
        {label:'顺序',main:true,cell:r=>`第 ${r.i+1} 条`},{label:'条件',wide:true,cell:r=>ruleText(r)},{label:'人设',cell:r=>name(r.persona_id)}]}/>
        :<EmptyState kind="empty" subject="分群规则" next="所有客户使用租户默认人设。"/>}</>
      :<EmptyState kind="empty" subject="已发布的分配规则" next="发布规则并设置租户默认人设后，客户对话才会带上人设。"/>}
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
    <FormField id="rules-default" label="租户默认人设"><select id="rules-default" value={fallback} onChange={e=>setFallback(e.target.value)}>
      <option value="">不设默认</option>{options}</select></FormField>
    {rows.map((r,i)=><fieldset key={i} className="rule-row"><legend>规则 {i+1}</legend>
      <FormField id={`rule-${i}-kind`} label="条件类型" width="full"><select id={`rule-${i}-kind`} value={r.kind} onChange={e=>change(i,{kind:e.target.value as RuleDraft['kind']})}>
        <option value="role_id">客户拥有某个角色</option><option value="property">客户属性等于</option></select></FormField>
      {r.kind==='role_id'?<FormField id={`rule-${i}-role`} label="角色 ID（64 位）" width="full"><input id={`rule-${i}-role`} value={r.role_id} onChange={e=>change(i,{role_id:e.target.value.trim()})}/></FormField>
        :<><FormField id={`rule-${i}-property`} label="属性名" width="full"><input id={`rule-${i}-property`} value={r.property} onChange={e=>change(i,{property:e.target.value.trim()})}/></FormField>
          <FormField id={`rule-${i}-equals`} label="属性值" width="full"><input id={`rule-${i}-equals`} value={r.equals} onChange={e=>change(i,{equals:e.target.value})}/></FormField></>}
      <FormField id={`rule-${i}-persona`} label="人设" width="full"><select id={`rule-${i}-persona`} value={r.persona_id} onChange={e=>change(i,{persona_id:e.target.value})}><option value="">选择人设</option>{options}</select></FormField>
      <button type="button" onClick={()=>setRows(rows.filter((_,j)=>j!==i))}>删除这条</button></fieldset>)}
    <p><button type="button" onClick={()=>setRows([...rows,{kind:'property',role_id:'',property:'',equals:'',persona_id:''}])}>添加规则</button></p>
    <FormField id="rules-reason" label="发布原因"><input id="rules-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></FormField>
    <p className="note">新规则发布后，受影响的客户在下次对话时更换人设，每次更换都留下记录。</p>
  </Form>;
}
function AssignForm({member,usable}:{member:Parameters<typeof Form>[0]['member'];usable:PersonaVersion[]}){
  const [consumer,setConsumer]=useState('');const [persona,setPersona]=useState('');const [reason,setReason]=useState('');
  return <Form operation="assign_operator_persona" member={member} title="为单个客户指定人设" disabled={!/^[a-f0-9]{64}$/.test(consumer)||!reason.trim()}
    body={()=>({consumer_id:consumer,persona_id:persona||null,reason:reason.trim()})}>
    <FormField id="assign-consumer" label="客户 ID" hint="在客户详情页复制客户 ID 后粘贴（64 位）。"><input id="assign-consumer" value={consumer} aria-describedby="assign-consumer-hint" onChange={e=>setConsumer(e.target.value.trim())}/></FormField>
    <FormField id="assign-persona" label="人设"><select id="assign-persona" value={persona} onChange={e=>setPersona(e.target.value)}>
      <option value="">清除指定（回到规则）</option>{usable.map(p=><option key={p.persona_id} value={p.persona_id}>{p.document.display_name} · {personaRef(p)}</option>)}</select></FormField>
    <FormField id="assign-reason" label="原因"><input id="assign-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></FormField>
  </Form>;
}

// ---- AI disclosure ----
const MODE_TEXT:Record<AiDisclosure['mode'],string>={none:'不主动表明',first_contact:'首次接触时表明一次',always:'每次会话开始时表明'};
export function Disclosure({setting}:{setting:AiDisclosure}){
  return <>
    <KeyValue className="facts" label="当前 AI 身份披露配置" items={[{key:'主动表明',value:MODE_TEXT[setting.mode]},
      {key:'被直接询问是否 AI 时',value:setting.when_asked==='deflect'?'用人设里的回避话术回答（话术为空时转人工）':'转人工'},
      {key:'拦截“自称人类”的说法',value:setting.block_human_claims?'拦截':'不拦截'},{key:'消息加“AI 生成”标识',value:setting.content_label?'加':'不加'},
      {key:'修订号',value:setting.revision?`第 ${setting.revision} 版`:'未设置（使用默认值）'}]}/>
    <p className="note">是否让客户知道是 AI 由使用方决定；不同法域对 AI 生成内容的标识有不同要求，合规责任由使用方承担。系统只提供开关与话术配置，并记录每次处理。</p>
    <WithMember>{member=><DisclosureForm member={member} setting={setting}/>}</WithMember>
  </>;
}
function DisclosureForm({member,setting}:{member:Parameters<typeof Form>[0]['member'];setting:AiDisclosure}){
  const [mode,setMode]=useState(setting.mode);const [asked,setAsked]=useState(setting.when_asked);
  const [block,setBlock]=useState(setting.block_human_claims);const [label,setLabel]=useState(setting.content_label);const [reason,setReason]=useState('');
  return <Form operation="set_ai_disclosure" member={member} title="保存 AI 身份披露配置" disabled={!reason.trim()}
    body={()=>({mode,when_asked:asked,block_human_claims:block,content_label:label,reason:reason.trim()})}>
    <FormField id="ai-mode" label="主动表明"><select id="ai-mode" value={mode} onChange={e=>setMode(e.target.value as AiDisclosure['mode'])}>
      {Object.entries(MODE_TEXT).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></FormField>
    <ChoiceGroup legend="被直接询问是否 AI 时" name="ai-asked" value={asked} onChange={setAsked} options={[
      {value:'deflect',label:'用回避话术回答',hint:'使用人设里的回避话术；话术为空时转人工。'},{value:'handoff',label:'转人工'}]}/>
    <CheckboxGroup legend="附加处理" items={[
      {key:'block',label:'拦截“我是真人”一类自称人类的说法',checked:block,onChange:setBlock},
      {key:'label',label:'在消息上加“AI 生成”标识',hint:'仅在渠道支持时生效。',checked:label,onChange:setLabel}]}/>
    <FormField id="ai-reason" label="原因"><input id="ai-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></FormField>
  </Form>;
}

// ---- consumer detail: persona@version, its source, the customer's own preferences ----
export function ConsumerPersona({consumerId}:{consumerId:string}){
  const overview=useQuery({queryKey:['workbench','operator-personas',consumerId],queryFn:()=>readOperatorPersonas(consumerId)});
  const prefs=useQuery({queryKey:['workbench','contact-preferences',consumerId],queryFn:()=>readContactPreferences(consumerId),retry:false});
  return <section aria-labelledby="consumer-persona-title"><h2 id="consumer-persona-title">运营人设与沟通偏好</h2>
    {overview.isPending?<EmptyState kind="loading" subject="运营人设" compact/>:overview.isError?<EmptyState kind={kindOf(overview.error)==='forbidden'?'forbidden':'unavailable'}
      subject="运营人设" onRetry={()=>void overview.refetch()}/>
      :<PersonaLine data={overview.data}/>}
    <h3>这位客户自己说过的沟通偏好</h3>
    {prefs.isPending?<EmptyState kind="loading" subject="沟通偏好" compact/>:prefs.isError?<EmptyState kind={kindOf(prefs.error)==='forbidden'?'forbidden':'unavailable'}
      subject="沟通偏好" reason={kindOf(prefs.error)==='forbidden'?'只有负责人可以查看客户的沟通偏好':undefined} onRetry={()=>void prefs.refetch()}/>
      :prefs.data.length?<DataTable label="沟通偏好" rows={prefs.data} rowKey={p=>p.claim_id} columns={[
        {label:'偏好',main:true,cell:p=>PREFERENCE_TEXT[p.dimension]},{label:'内容',wide:true,cell:p=>preferenceText(p)},
        {label:'状态',cell:p=><StatusBadge text={p.dismissed?'不采纳':'采纳'} tone={p.dismissed?'neutral':'ok'} code={p.dismissed?'dismissed':'adopted'}/>},
        {label:'操作',cell:p=><WithMember>{member=><ConfirmedForm operation="dismiss_contact_preference" member={member} trigger={p.dismissed?'恢复采纳…':'不采纳…'}
          title={p.dismissed?'确认恢复采纳':'确认不采纳'} tone={p.dismissed?undefined:'danger'}
          impact={p.dismissed?'影响：这条偏好重新在派发和对话中生效。':'影响：这条客户自己说过的偏好不再约束派发与对话；可以再恢复采纳。'}
          body={reason=>({claim_id:p.claim_id,dismissed:!p.dismissed,reason})}/>}</WithMember>}]}/>
      :<EmptyState kind="empty" subject="沟通偏好" next="客户说出联系时段、频率、渠道、主题或语气方面的要求后，会出现在这里。"/>}
    <p className="note">时段、频率、渠道、主题在派发时强制执行，只会比租户规则更严；语气只供对话参考。负责人只能不采纳，不能替客户新增偏好。</p>
  </section>;
}
function preferenceText(p:ContactPreference):string{
  const v=p.dimension==='frequency'?`${p.value} 次`:String(p.value);
  return p.polarity==='negated'?(p.dimension==='topic'?`不要：${v}`:`不要用：${v}`):p.dimension==='channel'?`只用：${v}`:v;
}
function PersonaLine({data}:{data:PersonaOverview}){
  const usable=usableVersions(data.personas);const c=data.consumer;
  if(!c?.settled&&!c?.desired?.persona_id)return <EmptyState kind="empty" subject="生效的人设" next="租户还没有可用的人设或分配规则；对话不带人设。"/>;
  const now=c.settled?usable.get(c.settled.persona_id):undefined;const next=c.desired?.persona_id?usable.get(c.desired.persona_id):undefined;
  const changes=c.desired?.persona_id&&(c.desired.persona_id!==c.settled?.persona_id||c.desired.source!==c.settled?.source);
  return <KeyValue className="facts" label="运营人设" items={[
    {key:'当前生效',value:c.settled?<>{now?.document.display_name??c.settled.persona_id} <code>{now?personaRef(now):c.settled.persona_id+'（已停用）'}</code> · {PERSONA_SOURCE_TEXT[c.settled.source]}</>:'尚未在对话中使用'},
    ...(changes&&c.desired?.persona_id&&c.desired.source?[{key:'下次对话起',value:<>{next?.document.display_name??c.desired.persona_id} <code>{next?personaRef(next):c.desired.persona_id}</code> · {PERSONA_SOURCE_TEXT[c.desired.source]}</>}]:[])]}/>;
}


// ---- outreach controls (0232, owner only; outreach design P3/P8, plan O1/O5) ----
const CONSENT_TEXT:Record<ConsentMode,string>={opt_out:'默认可发，客户表示不同意即停',opt_in:'只发给明确同意的客户'};
/** A time zone in words where it is the workbench's own (Beijing); any other IANA name as written. */
const zoneText=(z:string)=>z==='Asia/Shanghai'?'北京时间（Asia/Shanghai）':z;
export function OutreachControlsTab(){
  const query=useQuery({queryKey:['workbench','outreach-controls'],queryFn:readOutreachControls});
  if(query.isPending)return <EmptyState kind="loading" subject="外联控制"/>;
  if(query.isError)return <EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="外联控制" onRetry={()=>void query.refetch()}/>;
  const s=query.data.settings;
  return <>
    <p className="note">外联控制决定系统何时、多久主动联系客户。目前还没有外发功能，这些设置先用于派发预演；联系限制与客户本人的偏好始终更优先。</p>
    <KeyValue className="facts" label="当前外联控制" items={[
      {key:'修订号',value:s.source==='default'?'未设置（使用默认值）':`第 ${s.revision} 版`},
      {key:'时区',value:zoneText(s.time_zone)},{key:'勿扰时段',value:s.quiet_hours??'不设'},
      {key:'频率上限（每位客户）',value:`24 小时 ${s.per_24h} 条 · 7 天 ${s.per_7d} 条`,hint:'含带通知的服务消息，不含回复'},
      {key:'营销',value:<><StatusBadge text={s.marketing.enabled?'已开启':'已关闭'} tone={s.marketing.enabled?'limited':'neutral'}/> {CONSENT_TEXT[s.marketing.consent_mode]}</>},
      {key:'营销额度',value:`7 天 ${s.marketing.per_7d} 条`},{key:'退订话术',value:s.marketing.unsubscribe_notice},{key:'发送方标识',value:s.marketing.sender_label||'不设'}]}/>
    {query.data.events.length?<DataTable label="外联控制修改记录" rows={query.data.events} rowKey={e=>String(e.revision)} columns={[
      {label:'修订号',main:true,cell:e=>`第 ${e.revision} 版`},{label:'营销',cell:e=>e.marketing_enabled?'开启':'关闭'},
      {label:'确认的同意模式',cell:e=>e.consent_mode_confirmed?CONSENT_TEXT[e.consent_mode_confirmed]:'—'},{label:'时间',cell:e=><Time value={e.recorded_at}/>}]}/>:null}
    <p className="note">营销的合规要求（同意、退订、发送时段、发送方标识）因法域而异，由使用方负责；opt-out 在部分法域不被允许。</p>
    <WithMember>{member=><OutreachControlsForm member={member} current={s}/>}</WithMember>
    <OutreachPurposes/>
    <OutreachApprovals/>
  </>;
}
/** The settings the Action takes, from the form state (empty quiet hours means none). */
export function outreachSettingsOf(f:{time_zone:string;quiet:boolean;quiet_from:string;quiet_to:string;per_24h:string;per_7d:string;enabled:boolean;consent:ConsentMode;
  notice:string;marketing_7d:string;sender:string}):OutreachSettings{
  return {time_zone:f.time_zone.trim(),quiet_hours:f.quiet?`${f.quiet_from}-${f.quiet_to}`:null,per_24h:Number(f.per_24h),per_7d:Number(f.per_7d),
    marketing:{enabled:f.enabled,consent_mode:f.consent,unsubscribe_notice:f.notice.trim(),per_7d:Number(f.marketing_7d),sender_label:f.sender.trim()}};
}
/** O5: switching marketing on needs the confirmation; the marketing limit within the 7-day limit; a reason. SQL decides again. */
export function outreachReady(f:{time_zone:string;notice:string;marketing_7d:string;per_7d:string;enabled:boolean},current:OutreachSettings,confirmed:boolean,reason:string):boolean{
  const switchingOn=f.enabled&&!current.marketing.enabled;
  return !!f.time_zone.trim()&&!!f.notice.trim()&&Number(f.marketing_7d)<=Number(f.per_7d)&&!!reason.trim()&&(!switchingOn||confirmed);
}
function OutreachControlsForm({member,current}:{member:Parameters<typeof Form>[0]['member'];current:OutreachSettings}){
  const [f,setF]=useState({time_zone:current.time_zone,quiet:current.quiet_hours!==null,quiet_from:current.quiet_hours?.split('-')[0]??'21:00',
    quiet_to:current.quiet_hours?.split('-')[1]??'09:00',per_24h:String(current.per_24h),per_7d:String(current.per_7d),enabled:current.marketing.enabled,
    consent:current.marketing.consent_mode,notice:current.marketing.unsubscribe_notice,marketing_7d:String(current.marketing.per_7d),sender:current.marketing.sender_label});
  const [confirmed,setConfirmed]=useState(false);const [reason,setReason]=useState('');
  const set=(patch:Partial<typeof f>)=>{setF({...f,...patch});if('enabled' in patch||'consent' in patch)setConfirmed(false);};
  const switchingOn=f.enabled&&!current.marketing.enabled;
  const valid=outreachReady(f,current,confirmed,reason);
  return <Form operation="set_outreach_controls" member={member} title="保存外联控制" disabled={!valid}
    body={()=>({settings:outreachSettingsOf(f),confirm_consent_mode:switchingOn?f.consent:null,reason:reason.trim()})}>
    <section className="form-section"><h3>时间与频率</h3>
    <FormField id="oc-tz" label="时区" hint="IANA 时区名，例如 Asia/Shanghai（北京时间）。"><input id="oc-tz" value={f.time_zone} aria-describedby="oc-tz-hint" onChange={e=>set({time_zone:e.target.value})}/></FormField>
    <CheckboxGroup legend="勿扰时段" items={[{key:'quiet',label:'设置勿扰时段',hint:'时段内不主动联系；可跨午夜。',checked:f.quiet,onChange:quiet=>set({quiet})}]}/>
    {f.quiet?<fieldset className="choice-group"><legend>勿扰时段</legend><div className="field-pair">
      <FormField id="oc-quiet-from" label="开始" width="sm"><input id="oc-quiet-from" type="time" value={f.quiet_from} onChange={e=>set({quiet_from:e.target.value})}/></FormField>
      <FormField id="oc-quiet-to" label="结束" width="sm"><input id="oc-quiet-to" type="time" value={f.quiet_to} onChange={e=>set({quiet_to:e.target.value})}/></FormField></div></fieldset>:null}
    <div className="field-pair">
      <FormField id="oc-24h" label="每位客户 24 小时最多（条）" width="sm"><input id="oc-24h" type="number" min={0} max={100} value={f.per_24h} onChange={e=>set({per_24h:e.target.value})}/></FormField>
      <FormField id="oc-7d" label="每位客户 7 天最多（条）" width="sm"><input id="oc-7d" type="number" min={0} max={1000} value={f.per_7d} onChange={e=>set({per_7d:e.target.value})}/></FormField></div>
    </section>
    <section className="form-section"><h3>营销</h3>
    <CheckboxGroup legend="营销开关" items={[{key:'enabled',label:'开启营销',checked:f.enabled,onChange:enabled=>set({enabled})}]}/>
    <ChoiceGroup legend="营销同意模式" name="oc-consent" value={f.consent} onChange={consent=>set({consent})} options={[
      {value:'opt_out',label:CONSENT_TEXT.opt_out,hint:'客户退订、拒绝营销主题或要求别再联系即停。'},{value:'opt_in',label:CONSENT_TEXT.opt_in,hint:'需要客户的同意记录。'}]}/>
    {switchingOn?<CheckboxGroup legend="确认" items={[{key:'confirm',label:<>我确认营销同意模式为「{CONSENT_TEXT[f.consent]}」，并知悉合规责任由使用方承担</>,checked:confirmed,onChange:setConfirmed}]}/>:null}
    <FormField id="oc-mk7d" label="营销额度：7 天最多（条）" hint="不超过上面的 7 天上限。" width="sm"><input id="oc-mk7d" type="number" min={0} value={f.marketing_7d} aria-describedby="oc-mk7d-hint" onChange={e=>set({marketing_7d:e.target.value})}/></FormField>
    <FormField id="oc-notice" label="退订话术" hint="追加在每条营销消息末尾。"><input id="oc-notice" value={f.notice} maxLength={200} aria-describedby="oc-notice-hint" onChange={e=>set({notice:e.target.value})}/></FormField>
    <FormField id="oc-sender" label="发送方标识（可选）"><input id="oc-sender" value={f.sender} maxLength={40} onChange={e=>set({sender:e.target.value})}/></FormField>
    </section>
    <FormField id="oc-reason" label="原因"><input id="oc-reason" value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></FormField>
  </Form>;
}


// ---- outreach purposes (outreach slice 3, owner only): the purpose list is trusted configuration; the owner switches purposes ----
const KIND_TEXT={commitment_followup:'承诺跟进',service_followup:'服务回访',plan_defined:'计划目的',marketing:'营销'} as const;
const TRIGGER_TEXT:Record<string,string>={commitment_due:'承诺到期前',service_receipt:'服务回执',plan_reassess:'计划复评',owner_request:'负责人发起'};
export function OutreachPurposes(){
  const query=useQuery({queryKey:['workbench','outreach-purposes'],queryFn:readOutreachPurposes});
  return <section aria-labelledby="outreach-purposes-title"><h2 id="outreach-purposes-title">外联目的</h2>
    <p className="note">外联只能用目的清单里登记的目的；清单是可信配置，新增目的（包括计划目的）要发布新版清单。负责人在这里按租户启用或停用。</p>
    {query.isPending?<EmptyState kind="loading" subject="外联目的" compact/>:query.isError?<EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="外联目的" onRetry={()=>void query.refetch()}/>
      :<><p className="note">目的清单 v{query.data.list_version}</p><DataTable label="外联目的" rows={query.data.purposes} rowKey={p=>p.purpose_id} columns={[
        {label:'目的',main:true,cell:p=><>{KIND_TEXT[p.kind]} <code>{p.purpose_id}</code></>},
        {label:'状态',cell:p=><StatusBadge text={p.enabled?'已启用':'已停用'} tone={p.enabled?'ok':'neutral'}/>},
        {label:'触发源',cell:p=>TRIGGER_TEXT[p.trigger]??p.trigger},
        {label:'频率',cell:p=>p.frequency_class==='marketing'?'营销额度':'服务额度'},
        {label:'同意',cell:p=>p.kind!=='marketing'?'—':p.requires_consent?'只发给已同意的客户':'跟随租户同意模式'},
        {label:'内容',wide:true,cell:p=>`最多 ${p.max_chars} 字${p.unsubscribe_notice_required?'，末尾附退订话术':''}`},
        {label:'审批',cell:p=>p.requires_approval?'需要':'不需要'},
        {label:'操作',cell:p=><WithMember>{member=><Form operation="set_outreach_purpose" member={member} className="inline-action" title={p.enabled?'停用':'启用'}
          body={()=>({purpose_id:p.purpose_id,enabled:!p.enabled,reason:p.enabled?'负责人停用':'负责人启用'})}><span/></Form>}</WithMember>}]}/></>}
  </section>;
}


// ---- outreach approvals (outreach slice 6, owner only): sends of a purpose that needs approval wait here ----
export function OutreachApprovals(){
  const query=useQuery({queryKey:['workbench','outreach-approvals'],queryFn:readOutreachApprovals});
  return <section aria-labelledby="outreach-approvals-title"><h2 id="outreach-approvals-title">待审批外联</h2>
    <p className="note">目的配置为“需要审批”的外联在这里等待；发起人不能审批自己的请求。批准后派发时仍要通过全部闸门（联系限制最先），拒绝或超时即终止。</p>
    {query.isPending?<EmptyState kind="loading" subject="待审批外联" compact/>:query.isError?<EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="待审批外联" onRetry={()=>void query.refetch()}/>
      :<><p className="note">审批时限 {Math.round(query.data.window_seconds/3600)} 小时</p>
      {query.data.pending.length===0?<EmptyState kind="empty" subject="待审批的外联"/>:<DataTable label="待审批外联" rows={query.data.pending} rowKey={p=>p.intent_id} columns={[
        {label:'外联',main:true,cell:p=><IdText value={p.intent_id.replace(/^outreach:/,'')} kind="外联"/>},
        {label:'目的',cell:p=><code>{p.purpose_id}</code>},
        {label:'发起时间',cell:p=><Time value={p.requested_at}/>},
        {label:'截止',cell:p=><Time value={p.expires_at}/>},
        {label:'发起人',cell:p=><IdText value={p.requested_by} kind="成员" copy={false}/>},
        {label:'操作',cell:p=><WithMember>{member=><>
          <Form operation="decide_outreach_approval" member={member} className="inline-action" title="批准" body={()=>({intent_id:p.intent_id,decision:'approve',reason:'负责人批准'})}><span/></Form>
          <Form operation="decide_outreach_approval" member={member} className="inline-action" title="拒绝" body={()=>({intent_id:p.intent_id,decision:'reject',reason:'负责人拒绝'})}><span/></Form>
        </>}</WithMember>}]}/>}</>}
  </section>;
}
