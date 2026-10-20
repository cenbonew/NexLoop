/**
 * World model W4 (docs/design/workbench-world-model-design.md §2/§5; owner 2026-10-19 D1-D7): the tenant's data assets in
 * NexLoop's own terms, read from the owner's metrics read (L6 0225 `data_assets`, 0216 `candidates`, 0146 `backup`): Artifacts,
 * Claims and extraction runs, the recall index (vectors per embedding profile, backfills), the embedding budget, backups and the
 * merge configuration. Counts, profile ids and times only: never a message, a Claim value, a recall body or an embedding. A count
 * the backend does not report is "不可用", never 0 (AT-045).
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readArtifacts,readMetrics,type Metrics} from '../api';
import {routePath} from '../route';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {StatusBadge,Time} from '../ui';
import {Card,CardGrid,EmptyState,IdText} from '../kit';
import {statusLabel} from '../status-labels';
import {BackupBlock,Counts} from './Observability';

const Unavailable=({what}:{what:string})=><EmptyState kind="unavailable" subject={what} reason="后端尚未提供这一项" compact/>;

export function DataAssets(){
  const query=useQuery({queryKey:['workbench','metrics'],queryFn:readMetrics});
  return <Page title="数据资产" query={query}
    lead="NexLoop 自己的数据资产（只读）：Artifact、Claim 与提取、召回索引、Embedding 预算、备份与合并配置。只有计数和时间，不含消息原文。">{m=><Body m={m}/>}</Page>;
}

function Body({m}:{m:Metrics}){
  const d=m.data_assets,c=m.candidates;
  return <>
    <p className="note">快照时间 <Time value={m.computed_at}/>。</p>
    <CardGrid label="数据资产" min="24rem">
      <Card title="Artifact" footer={<p className="note">只显示元数据，不显示内容；每条列出的 Artifact 都会记入读取审计。文件快照见“备份”中的“最近一次文件快照”。</p>}>
        <ArtifactList/>
      </Card>
      <Card title="Claim 与提取" footer={d?<p className="note"><a href={routePath({page:'knowledge'})}>知识工作台</a>中审核由 Claim 产生的候选。</p>:null}>
        {d?<>
          <p><strong>{d.claims.total}</strong> 条 Claim · 提取运行 <strong>{d.extraction_runs.total}</strong> 次{d.extraction_runs.last_at?<>，最近 <Time value={d.extraction_runs.last_at}/></>:null}</p>
          <h3>按认知类型</h3><Counts values={d.claims.by_epistemic_kind} domain="claim_kind" subject="Claim"/>
          <h3>按解决状态</h3><Counts values={d.claims.by_resolution_state} domain="claim_resolution" subject="Claim"/>
          <h3>提取队列</h3><Counts values={d.extraction_jobs} domain="job_status" subject="提取任务"/>
        </>:<Unavailable what="Claim 与提取的统计"/>}
      </Card>
      <Card title="召回索引">
        {d?<>
          <h3>条目</h3><Counts values={d.recall.entries} domain="recall_index" subject="召回条目"/>
          <h3>Embedding profile</h3>
          <DataTable label="Embedding profile" empty={{subject:'Embedding profile'}} rows={d.recall.profiles} rowKey={p=>p.profile_id} columns={[
            {label:'Profile',main:true,cell:p=><code className="id-code">{p.profile_id}</code>},{label:'维度',num:true,cell:p=>p.dimension},{label:'向量',num:true,cell:p=>p.vectors},
            {label:'创建时间',cell:p=><Time value={p.created_at}/>}]}/>
          <h3>回填记录</h3>
          <DataTable label="回填记录" empty={{subject:'回填记录'}} rows={d.recall.backfills} rowKey={b=>b.profile_id} columns={[
            {label:'Profile',main:true,cell:b=><code className="id-code">{b.profile_id}</code>},{label:'次数',num:true,cell:b=>b.runs},
            {label:'最近一次',cell:b=><Time value={b.last_at}/>},
            {label:'最近排队',num:true,cell:b=>b.last_queued??<StatusBadge text="不可用" tone="neutral"/>},{label:'最近定义类型',num:true,cell:b=>b.last_definition_types??<StatusBadge text="不可用" tone="neutral"/>}]}/>
        </>:<Unavailable what="召回索引的统计"/>}
      </Card>
      <Card title="Embedding 预算">
        {!d?<Unavailable what="Embedding 预算"/>:d.embedding_budget.status==='unavailable'
          ?<EmptyState kind="unavailable" subject="Embedding 预算" reason="目前没有记录 Embedding 花费的预算或成本类别（预算只有模型费用和激励）" next="不表示花费为 0。" compact/>
          :<p><StatusBadge text="已记录" tone="ok"/></p>}
      </Card>
      <Card title="合并配置">
        {c?<p><StatusBadge domain="merge_configuration" code={c.merge_configuration_active?'active':'missing'} tone={c.merge_configuration_active?'ok':'fail'}/>
          {' '}待粘合候选 {c.staged} 个{!c.merge_configuration_active&&c.staged?`，其中 ${c.staged_without_configuration} 个因没有合并配置而等待（Claim 匹配空转）`:''}。</p>
          :<Unavailable what="合并配置状态"/>}
      </Card>
      <BackupBlock backup={m.backup}/>
    </CardGrid>
  </>;
}

export function bytes(n:number):string{return n<1024?`${n} B`:n<1024*1024?`${(n/1024).toFixed(1)} KB`:`${(n/1024/1024).toFixed(1)} MB`;}

/** The tenant's stored Artifacts, newest first, a page at a time (cursor stack: 上一页 returns to the page before). */
export function ArtifactList(){
  const [cursors,setCursors]=useState<string[]>([]);
  const before=cursors[cursors.length-1];
  const page=useQuery({queryKey:['workbench','artifacts',before??''],queryFn:()=>readArtifacts(before)});
  if(page.isPending)return <EmptyState kind="loading" subject="Artifact" compact/>;
  if(page.isError)return <EmptyState kind={kindOf(page.error)==='forbidden'?'forbidden':'unavailable'} subject="Artifact 列表" onRetry={()=>void page.refetch()} compact/>;
  const p=page.data;
  return <>
    <p role="status">共 <strong>{p.total}</strong> 个 Artifact · 本页 {p.items.length} 个{p.hidden?<> · <StatusBadge text={`隐藏 ${p.hidden} 个（所属客户正在删除）`} tone="limited" code="hidden"/></>:null}</p>
    <DataTable label="Artifact" empty={{subject:cursors.length?'这一页的 Artifact':'Artifact'}} rows={p.items} rowKey={a=>a.artifact_id} columns={[
      {label:'Artifact',main:true,cell:a=><IdText value={a.artifact_id}/>},{label:'用途',cell:a=>statusLabel('artifact_purpose',a.purpose)},
      {label:'媒体类型',cell:a=><code className="id-code">{a.media_type}</code>},{label:'大小',num:true,cell:a=>bytes(a.size_bytes)},
      {label:'SHA-256 前缀',cell:a=><code className="id-code" title="内容 SHA-256 的前缀">{a.sha256_prefix}</code>},
      {label:'创建',cell:a=><Time value={a.created_at}/>},{label:'保留至',cell:a=><Time value={a.retention_until}/>},
      {label:'Run',cell:a=>a.run_id?<IdText value={a.run_id}/>:'—'},
      {label:'客户',cell:a=>a.consumer_id?<a href={routePath({page:'consumers',id:a.consumer_id})}><IdText value={a.consumer_id} copy={false}/></a>:'—'}]}/>
    <div className="pager">{cursors.length?<button type="button" onClick={()=>setCursors(cursors.slice(0,-1))}>上一页</button>:null}
      {p.next_before?<button type="button" onClick={()=>setCursors([...cursors,p.next_before!])}>下一页</button>:null}</div>
  </>;
}
