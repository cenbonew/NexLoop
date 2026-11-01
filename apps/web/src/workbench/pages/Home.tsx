/**
 * K4f (owner Q-3, Q-A–Q-E 2026-11-01): the workbench home. 待我处理 (GET /api/v1/workbench/pending, pages/Pending) is the page; the
 * former 总览 is its 健康 tab (GET /api/v1/workbench/overview, pages/Overview). Each tab reads only its own endpoint, so a member the
 * overview refuses (the reviewer) still sees what is waiting for them. The tab lives in the URL (?tab=health), switched in place.
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readOverview,readPending} from '../api';
import {PAGES,routePath,type PageKey,type Query,type Route} from '../route';
import {Tabs} from '../ui';
import {Page} from './Page';
import {PendingBody} from './Pending';
import {HealthCards} from './Overview';

type Tab='pending'|'health';
const TABS:{key:Tab;label:string}[]=[{key:'pending',label:'待我处理'},{key:'health',label:'健康'}];

export function Home({actions,query,onQuery,go}:{actions?:ReactNode;query?:Query;onQuery:(query:Query)=>void;go:(route:Route)=>void}){
  const tab:Tab=query?.tab==='health'?'health':'pending';
  const pending=useQuery({queryKey:['workbench','pending'],queryFn:readPending,enabled:tab==='pending'});
  const overview=useQuery({queryKey:['workbench','overview'],queryFn:readOverview,enabled:tab==='health'});
  const toolbar=<Tabs label="首页" id="home" tabs={TABS} value={tab} onChange={t=>onQuery(t==='health'?{tab:'health'}:{})}/>;
  return tab==='health'
    ?<Page title={PAGES.home} query={overview} actions={actions} toolbar={toolbar}
      lead="运营健康：目标与控制、承诺、Action 与异常、联系限制、人工接管、后台积压与费用。">{data=><div id="home-panel" role="tabpanel" aria-labelledby="home-tab-health">
        <HealthCards data={data} onOpen={page=>go({page:page as PageKey})}/></div>}</Page>
    :<Page title={PAGES.home} query={pending} actions={actions} toolbar={toolbar}
      lead="你可以处理的事，按类别列出数量和最久等待时间；处理后自动消失。">{data=><div id="home-panel" role="tabpanel" aria-labelledby="home-tab-pending">
        <PendingBody data={data} go={go} href={routePath}/></div>}</Page>;
}
