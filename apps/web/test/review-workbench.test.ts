import {afterEach,expect,test,vi} from 'vitest';
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {ApiError} from '../src/api';
import {Account} from '../src/Account';
import {WORKBENCH_REVIEW,candidateDetail,decide,readCandidate} from '../src/review-api';
import {ReviewDecisions,ReviewDecisionsView} from '../src/workbench/pages/Knowledge';

const id='0f0e0d0c-0b0a-4908-8706-050403020100';
const scores={lexical_similarity:0.2,core_term_containment:0,vector_cluster:0.26,rule_whitelist:0,weighted_total:0.092,best_match_ref:'eios:property:Consumer/favorite_sport',threshold:0.375,config_version:'nx045-glue-v1-test64'};
const detail={candidate_id:id,kind:'property',status:'pending_review',revision:2,created_at:'2026-10-09T01:00:00Z',dependent_claim_count:2,config_version:'nx045-glue-v1-test64',merge_scores:scores,
  candidate:{proposed:{display_name:'常用付款方式',name:'payment_method'},recall:[{ref:'eios:property:Consumer/favorite_sport',method:'fts',score:0.21}]},
  decisions:{enabled:true,reason:'governed_human_review_action',actions:['approve','merge_into','reject']},cooldown:null,evidence:[],similar:[],
  derivation_impact:{applies:true,type_name:'Consumer',property_group:'purchase_behavior',restriction_configured:true,restricted:true,auto_access:[]}};
function client(){return new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});}
const render=(c:QueryClient,node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},node));
afterEach(()=>vi.unstubAllGlobals());

test('the customer home carries no staff blocks: no candidate review, no operations status',()=>{
  const html=render(client(),h(Account,{session:{authenticated:true,tenant_id:'synthetic-a',principal_id:'synthetic-customer',restricted:false},onLogout:()=>{}}));
  expect(html).toContain('已登录 NexLoop');
  expect(html).not.toContain('候选定义审核');expect(html).not.toContain('审核服务');expect(html).not.toContain('运营服务');
});

test('workbench reviewer decisions use the workbench realm routes and CSRF',async()=>{
  const calls:{url:string;method:string;headers:Record<string,string>}[]=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    calls.push({url,method:init?.method??'GET',headers:(init?.headers??{}) as Record<string,string>});
    if(url==='/api/v1/workbench/auth/csrf')return Response.json({csrf_token:'synthetic-workbench-csrf'});
    if(url.endsWith('/decisions'))return Response.json({replay:false,decision_id:'1f0e0d0c-0b0a-4908-8706-050403020100',outcome:'rejected',reflow_status:'none'});
    return Response.json(detail);
  }));
  expect((await readCandidate(id,WORKBENCH_REVIEW)).revision).toBe(2);
  const result=await decide(id,{decision:'reject',expected_revision:2,rationale:'不是业务概念'},'review-synthetic-key-0003',WORKBENCH_REVIEW);
  expect(result.outcome).toBe('rejected');
  expect(calls.map(c=>`${c.method} ${c.url}`)).toEqual([`GET /api/v1/workbench/review-decisions/candidates/${id}`,'POST /api/v1/workbench/auth/csrf',
    `POST /api/v1/workbench/review-decisions/candidates/${id}/decisions`]);
  expect(calls[2].headers['X-CSRF-Token']).toBe('synthetic-workbench-csrf');
});

test('the evidence page shows the three decisions to a reviewer and nothing to a member without the review grant',()=>{
  const reviewer=client();reviewer.setQueryData(['workbench','review-decision',id],candidateDetail(detail));
  const html=render(reviewer,h(ReviewDecisions,{candidateId:id}));
  expect(html).toContain('审核决定');for(const label of ['批准发布','并入已有定义','拒绝'])expect(html).toContain(label);
  expect(render(client(),h(ReviewDecisionsView,{state:{status:'error',error:new ApiError(403)}}))).toBe('');
  expect(render(client(),h(ReviewDecisionsView,{state:{status:'error',error:new ApiError(503)}}))).toContain('暂不可用');
});
