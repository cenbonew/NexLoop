/**
 * World model W4 (docs/design/workbench-world-model-design.md §2/§5; owner 2026-10-19 D1-D7): the tenant's data assets in
 * NexLoop's own terms, read from the owner's metrics read (L6 0225 `data_assets`, 0216 `candidates`, 0146 `backup`): Artifacts,
 * Claims and extraction runs, the recall index (vectors per embedding profile, backfills), the embedding budget, backups and the
 * merge configuration. Counts, profile ids and times only: never a message, a Claim value, a recall body or an embedding. A count
 * the backend does not report is "不可用", never 0 (AT-045).
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readArtifacts,readMetrics,STATE_TEXT as LOAD_TEXT,type ArtifactRow,type Metrics} from '../api';
import {routePath} from '../route';
import {Failure,Page} from './Page';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge} from '../ui';
import {when} from './Consumers';
import {BackupBlock,Counts} from './Observability';

const KIND_TEXT:Record<string,string>={verified_fact:'已核实事实',user_statement:'用户陈述',hypothesis:'推测',preference:'偏好',commitment:'承诺'};
const STATE_TEXT:Record<string,string>={resolved:'已解决',unresolved:'未解决',superseded:'已被取代',rejected:'已拒绝'};
const JOB_TEXT:Record<string,string>={pending:'待处理',running:'运行中',retry_wait:'等待重试',succeeded:'完成',failed:'失败',dead_lettered:'死信'};
const ENTRY_TEXT:Record<string,string>={definition:'定义',instance:'实例',alias:'别名'};

function Block({title,children,note}:{title:string;children:ReactNode;note?:ReactNode}){
  return <article className="workbench-block" aria-label={title}><h2 style={{marginTop:0}}>{title}</h2>{children}{note?<p className="note">{note}</p>:null}</article>;
}
const Unavailable=({what}:{what:string})=><Empty kind="unavailable" title={`${what}不可用`} next="后端尚未提供这一项；这不表示数量为零。"/>;

export function DataAssets(){
  const query=useQuery({queryKey:['workbench','metrics'],queryFn:readMetrics});
  return <Page title="数据资产" query={query}
    lead="NexLoop 自己的数据资产（只读）：Artifact、Claim 与提取、召回索引、embedding 预算、备份与合并配置。只有计数和时间，不含消息原文。">{m=><Body m={m}/>}</Page>;
}

function Body({m}:{m:Metrics}){
  const d=m.data_assets,c=m.candidates;
  return <>
    <p className="note">快照时间 {when(m.computed_at)}。</p>
    <div className="workbench-grid">
      <Block title="Artifact" note={<>只显示元数据，不显示内容；每条列出的 Artifact 都会记入读取审计。文件快照见“备份”中的“最近一次文件快照”。</>}>
        <ArtifactList/>
      </Block>
      <Block title="Claim 与提取">
        {d?<>
          <p><strong>{d.claims.total}</strong> 条 Claim · 提取运行 <strong>{d.extraction_runs.total}</strong> 次{d.extraction_runs.last_at?`，最近 ${when(d.extraction_runs.last_at)}`:''}</p>
          <h3>按认知类型</h3><Counts values={d.claims.by_epistemic_kind} labels={KIND_TEXT}/>
          <h3>按解决状态</h3><Counts values={d.claims.by_resolution_state} labels={STATE_TEXT}/>
          <h3>提取队列</h3><Counts values={d.extraction_jobs} labels={JOB_TEXT}/>
          <p className="note"><a href={routePath({page:'knowledge'})}>知识工作台</a>中审核由 Claim 产生的候选。</p>
        </>:<Unavailable what="Claim 与提取的统计"/>}
      </Block>
      <Block title="召回索引">
        {d?<>
          <h3>条目</h3><Counts values={d.recall.entries} labels={ENTRY_TEXT}/>
          <h3>Embedding profile</h3>
          {d.recall.profiles.length?<DataTable label="Embedding profile" rows={d.recall.profiles} rowKey={p=>p.profile_id} columns={[
            {label:'Profile',main:true,cell:p=><code>{p.profile_id}</code>},{label:'维度',num:true,cell:p=>p.dimension},{label:'向量',num:true,cell:p=>p.vectors},
            {label:'创建时间',cell:p=><span style={{whiteSpace:'nowrap'}}>{when(p.created_at)}</span>}]}/>
            :<Empty kind="none" title="还没有 embedding profile" next="召回索引服务以配置的 embedding 激活 profile 后出现在这里。"/>}
          <h3>回填记录</h3>
          {d.recall.backfills.length?<DataTable label="回填记录" rows={d.recall.backfills} rowKey={b=>b.profile_id} columns={[
            {label:'Profile',main:true,cell:b=><code>{b.profile_id}</code>},{label:'次数',num:true,cell:b=>b.runs},
            {label:'最近一次',cell:b=><span style={{whiteSpace:'nowrap'}}>{b.last_at?when(b.last_at):'—'}</span>},
            {label:'最近排队',num:true,cell:b=>b.last_queued??'不可用'},{label:'最近定义类型',num:true,cell:b=>b.last_definition_types??'不可用'}]}/>
            :<Empty kind="none" title="还没有回填记录"/>}
        </>:<Unavailable what="召回索引的统计"/>}
      </Block>
      <Block title="Embedding 预算">
        {!d?<Unavailable what="Embedding 预算"/>:d.embedding_budget.status==='unavailable'
          ?<p><StatusBadge text="不可用" tone="limited" code={d.embedding_budget.reason}/> 目前没有记录 embedding 花费的预算或成本类别（预算只有 model / incentive）；不表示花费为 0。</p>
          :<p><StatusBadge text="已记录" tone="ok"/></p>}
      </Block>
      <Block title="合并配置">
        {c?<p>{c.merge_configuration_active?<StatusBadge text="已生效" tone="ok" code="merge_configuration_active"/>:<StatusBadge text="未生效" tone="fail" code="merge_configuration_missing"/>}
          {' '}待粘合候选 {c.staged} 个{!c.merge_configuration_active&&c.staged?`，其中 ${c.staged_without_configuration} 个因没有合并配置而等待（claim-matcher 空转）`:''}。</p>
          :<Unavailable what="合并配置状态"/>}
      </Block>
      <BackupBlock backup={m.backup}/>
    </div>
  </>;
}

const PURPOSE_TEXT:Record<ArtifactRow['purpose'],string>={message_context:'消息上下文',fallback_context:'兜底上下文',role_context:'角色上下文',other:'其他'};
export function bytes(n:number):string{return n<1024?`${n} B`:n<1024*1024?`${(n/1024).toFixed(1)} KB`:`${(n/1024/1024).toFixed(1)} MB`;}
const short=(id:string,n=8)=>id.slice(0,n)+'…';

/** The tenant's stored Artifacts, newest first, a page at a time (cursor stack: 上一页 returns to the page before). */
export function ArtifactList(){
  const [cursors,setCursors]=useState<string[]>([]);
  const before=cursors[cursors.length-1];
  const page=useQuery({queryKey:['workbench','artifacts',before??''],queryFn:()=>readArtifacts(before)});
  if(page.isPending)return <p role="status">{LOAD_TEXT.loading}</p>;
  if(page.isError)return kindOf(page.error)==='forbidden'?<Empty kind="forbidden"/>:<Failure kind={kindOf(page.error)} onRetry={()=>void page.refetch()}/>;
  const p=page.data;
  return <>
    <p role="status">共 <strong>{p.total}</strong> 个 Artifact · 本页 {p.items.length} 个{p.hidden?<> · <StatusBadge text={`隐藏 ${p.hidden} 个（所属客户正在删除）`} tone="limited" code="hidden"/></>:null}</p>
    {p.items.length?<DataTable label="Artifact" rows={p.items} rowKey={a=>a.artifact_id} columns={[
      {label:'Artifact',main:true,cell:a=><code title={a.artifact_id}>{short(a.artifact_id)}</code>},{label:'用途',cell:a=>PURPOSE_TEXT[a.purpose]},
      {label:'类型',cell:a=><code>{a.media_type}</code>},{label:'大小',num:true,cell:a=>bytes(a.size_bytes)},{label:'SHA-256',cell:a=><code>{a.sha256_prefix}</code>},
      {label:'创建',cell:a=><span style={{whiteSpace:'nowrap'}}>{when(a.created_at)}</span>},{label:'保留至',cell:a=><span style={{whiteSpace:'nowrap'}}>{when(a.retention_until)}</span>},
      {label:'Run',cell:a=>a.run_id?<code title={a.run_id}>{short(a.run_id)}</code>:'—'},
      {label:'客户',cell:a=>a.consumer_id?<a href={routePath({page:'consumers',id:a.consumer_id})} title={a.consumer_id}>{short(a.consumer_id)}</a>:'—'}]}/>
      :<Empty kind="none" title={cursors.length?'这一页没有 Artifact':'还没有 Artifact'}/>}
    <div className="pager">{cursors.length?<button type="button" onClick={()=>setCursors(cursors.slice(0,-1))}>上一页</button>:null}
      {p.next_before?<button type="button" onClick={()=>setCursors([...cursors,p.next_before!])}>下一页</button>:null}</div>
  </>;
}
