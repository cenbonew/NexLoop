/**
 * World model W3 (docs/design/workbench-world-model-design.md §2/§5; owner 2026-10-19 D1-D7): the capability catalog, read only.
 * What the tenant can do and how far it may reach: the governed capability registry, the business Actions with their effect
 * categories (undeclared = closed, customer contact by default), inbound channels and delivery routes, and the real dispatch
 * switches. The switches are parsed and shown exactly as on the settings page (realDispatch(), Layers, outcomeText: the same SQL
 * source); opening or closing them stays the owner's control on the settings page. Nothing here writes.
 */
import {useQuery} from '@tanstack/react-query';
import {readCapabilities,type Capabilities as Catalog,type CatalogAction} from '../api';
import type {Query} from '../route';
import {routePath} from '../route';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {StatusBadge,Tabs,Time,type Tone} from '../ui';
import {EmptyState} from '../kit';
import {realDispatch,type RealDispatch} from '../realDispatch';
import {Layers,outcomeText} from '../RealDispatchPanel';

type Tab='capabilities'|'actions'|'channels'|'dispatch';
const TABS:{key:Tab;label:string}[]=[{key:'capabilities',label:'受治理能力'},{key:'actions',label:'业务 Action'},{key:'channels',label:'渠道与交付路由'},{key:'dispatch',label:'真实派发'}];
const SUBJECT_TEXT={human:'仅人类',agent_or_human:'Agent 或人类'} as const;
const TRUST_TEXT={server:'服务端',signed:'签名',client:'客户端陈述'} as const;
const TRUST_TONE:Record<keyof typeof TRUST_TEXT,Tone>={server:'ok',signed:'ok',client:'limited'};

/** UI batch 3: a technical name as secondary text under a readable form of it (the product prefix dropped, separators as spaces). */
export const readableName=(name:string)=>name.replace(/^(nexloop|eios:action:nexloop)\./,'').replace(/[._:]+/g,' ').trim();
export function NameCell({name}:{name:string}){return <><span className="name-main">{readableName(name)}</span><code className="tech-name">{name}</code></>;}

/** An Action's effect category in words: declared categories as declared; undeclared is closed (customer contact by default). */
export function categoryBadge(a:CatalogAction){
  if(!a.effect_category_declared||!a.effect_category)return <StatusBadge text="未声明（按客户联系处理，关闭）" tone="limited" code="undeclared"/>;
  return a.effect_category==='customer_contact'?<StatusBadge text="客户联系" tone="pending" code="customer_contact"/>
    :<StatusBadge text="非联系服务" tone="neutral" code="non_contact_service"/>;
}

export function Capabilities({query,onQuery}:{query?:Query;onQuery:(query:Query)=>void}){
  const result=useQuery({queryKey:['workbench','capabilities'],queryFn:readCapabilities});
  const tab=(TABS.some(t=>t.key===query?.tab)?query!.tab:'capabilities') as Tab;
  return <Page title="能力目录" query={result}
    lead="系统能做什么、能触达到哪里（只读）：受治理能力、业务 Action 及其效应类别、渠道与交付路由、真实派发开关。">{c=><Body catalog={c} tab={tab}
      onTab={t=>onQuery(t==='capabilities'?{}:{tab:t})}/>}</Page>;
}

function Body({catalog:c,tab,onTab}:{catalog:Catalog;tab:Tab;onTab:(tab:Tab)=>void}){
  let dispatch:RealDispatch|null=null;
  try{dispatch=realDispatch(c.real_dispatch);}catch{dispatch=null;}
  const undeclared=c.actions.filter(a=>a.active&&!a.effect_category_declared).length;
  const counts:Record<Tab,number>={capabilities:c.capabilities.length,actions:c.actions.length,channels:c.channels.length+c.routes.length,dispatch:dispatch?1+dispatch.actions.length:0};
  return <>
    <p className="workbench-summary" role="status">受治理能力 {c.capabilities.length} 项 · 业务 Action {c.actions.length} 个（生效 {c.actions.filter(a=>a.active).length}）
      {undeclared?` · ${undeclared} 个生效 Action 未声明效应类别（关闭）`:''} · 渠道 {c.channels.length} 个 · 交付路由 {c.routes.length} 条
      {dispatch?` · ${outcomeText(dispatch).text}`:''}</p>
    <Tabs label="能力目录" tabs={TABS} value={tab} onChange={onTab} announce={`${TABS.find(t=>t.key===tab)!.label}：${counts[tab]} 项`}/>
    <div role="tabpanel" aria-label={TABS.find(t=>t.key===tab)!.label}>
      {tab==='capabilities'?(c.capabilities.length?<DataTable label="受治理能力" rows={c.capabilities} rowKey={x=>x.capability} columns={[
          {label:'能力',main:true,cell:x=><NameCell name={x.capability}/>},{label:'操作',cell:x=><code className="tech-name">{x.operation}</code>},
          {label:'谁可以执行',cell:x=><StatusBadge text={SUBJECT_TEXT[x.subject_rule]} tone={x.subject_rule==='human'?'ok':'neutral'} code={x.subject_rule}/>},
          {label:'来源任务',cell:x=>x.source_task},{label:'登记时间',cell:x=><Time value={x.registered_at}/>}]}/>
        :<EmptyState kind="empty" subject="登记的受治理能力"/>)
      :tab==='actions'?(c.actions.length?<DataTable label="业务 Action" rows={c.actions} rowKey={a=>a.resource_id} columns={[
          {label:'Action',main:true,cell:a=><NameCell name={a.stable_name}/>},{label:'版本',num:true,cell:a=>`v${a.version}`},
          {label:'状态',cell:a=><StatusBadge text={a.active?'生效':'停用'} tone={a.active?'ok':'neutral'} code={a.active?'active':'inactive'}/>},
          {label:'效应类别',cell:categoryBadge},
          {label:'副作用',cell:a=><StatusBadge text={a.has_side_effects?'有':'无'} tone={a.has_side_effects?'limited':'neutral'}/>},
          {label:'能力',wide:true,cell:a=>a.capability?<><span>{readableName(a.capability)}</span><code className="tech-name">{a.capability}{a.capability_version?' @'+a.capability_version:''}</code></>:'—'},
          {label:'效应清单',num:true,cell:a=>a.effect_manifest_version?`v${a.effect_manifest_version}`:'—'}]}/>
        :<EmptyState kind="empty" subject="业务 Action"/>)
      :tab==='channels'?<>
          <h2>来信渠道</h2>
          {c.channels.length?<DataTable label="来信渠道" rows={c.channels} rowKey={x=>x.namespace} columns={[
            {label:'命名空间',main:true,cell:x=><NameCell name={x.namespace}/>},
            {label:'可信度',cell:x=><StatusBadge text={TRUST_TEXT[x.trust]} tone={TRUST_TONE[x.trust]} code={x.trust}/>},
            {label:'允许时钟偏差',num:true,cell:x=>`${x.skew_seconds} 秒`}]}/>:<EmptyState kind="empty" subject="登记的来信渠道"/>}
          <h2>交付路由</h2>
          {c.routes.length?<DataTable label="交付路由" rows={c.routes} rowKey={x=>x.delivery_action} columns={[
            {label:'交付 Action',main:true,cell:x=><NameCell name={x.delivery_action}/>},{label:'生效的目录服务',num:true,cell:x=>x.active_offerings}]}/>
            :<EmptyState kind="empty" subject="经目录服务交付的 Action"/>}
          <p className="note">渠道供应商路由由部署配置决定（{c.provider_routes==='deployment_configuration'?'部署配置':c.provider_routes}），工作台不读取其中的地址和凭据。</p>
        </>
      :dispatch?<>
          <p className={'rd-outcome '+(outcomeText(dispatch).dispatchable==='no'?'note':'warning')} role="status" data-dispatchable={outcomeText(dispatch).dispatchable}>{outcomeText(dispatch).text}</p>
          <Layers state={dispatch}/>
          <p className="note">开关只能由负责人在 <a href={routePath({page:'settings'})}>设置与治理</a> 中开启或关闭；这里只读。</p>
        </>
      :<EmptyState kind="unavailable" subject="真实派发开关"/>}
    </div>
  </>;
}
