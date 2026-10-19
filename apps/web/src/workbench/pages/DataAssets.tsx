/**
 * World model W4 (docs/design/workbench-world-model-design.md §2/§5; owner 2026-10-19 D1-D7): the tenant's data assets in
 * NexLoop's own terms, read from the owner's metrics read (L6 0224 `data_assets`, 0216 `candidates`, 0146 `backup`): Artifacts,
 * Claims and extraction runs, the recall index (vectors per embedding profile, backfills), the embedding budget, backups and the
 * merge configuration. Counts, profile ids and times only: never a message, a Claim value, a recall body or an embedding. A count
 * the backend does not report is "不可用", never 0 (AT-045).
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readMetrics,type Metrics} from '../api';
import {routePath} from '../route';
import {Page} from './Page';
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
      <Block title="Artifact" note={<>Artifact 的文件快照见“备份”中的“最近一次文件快照”。</>}>
        <Empty kind="unavailable" title="Artifact 列表不可用" next="工作台还没有列出 Artifact 的只读接口（只有按 ID 读取的服务接口）；这不表示没有 Artifact。"/>
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
