import {afterEach,expect,test,vi} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {readCandidate,readQueue,reviewError,candidateDetail,decide,decisionMessage,decisionResult} from '../src/review-api';
import {CandidateView,DecisionBar,DisabledDecisions,QueueList,ReviewWorkbench,mergeTargets} from '../src/Review';
import {ApiError} from '../src/api';

const id='0f0e0d0c-0b0a-4908-8706-050403020100';
const scores={lexical_similarity:0.2,core_term_containment:0,vector_cluster:0.26,rule_whitelist:0,weighted_total:0.092,best_match_ref:'eios:property:Consumer/favorite_sport',threshold:0.375,config_version:'nx045-glue-v1-test64'};
const disabled={enabled:false,reason:'awaiting_nx044_review_actions',actions:['approve','merge_into','reject']};
const decisions={enabled:true,reason:'governed_human_review_action',actions:['approve','merge_into','reject']};
const item={candidate_id:id,kind:'property',status:'pending_review',revision:2,created_at:'2026-10-09T01:00:00Z',dependent_claim_count:2,config_version:'nx045-glue-v1-test64',merge_scores:scores,
  candidate:{proposed:{display_name:'常用付款方式',name:'payment_method'},recall:[{ref:'eios:property:Consumer/favorite_sport',method:'fts',score:0.21}]}};
const detail={...item,decisions,cooldown:null,
  evidence:[{claim_id:'a'.repeat(64),quote:'我一般用花呗付款',source_message_id:'b'.repeat(64),span_start:0,span_end:8,predicate:'常用付款方式',value:{type:'string',value:'花呗'},resolution_state:'awaiting_definition'}],
  similar:[{candidate_id:'1f0e0d0c-0b0a-4908-8706-050403020100',status:'superseded',proposed:{display_name:'付款方式'},merge_scores:{...scores,weighted_total:0.5}}]};
afterEach(()=>vi.unstubAllGlobals());

test('403 means the queue is not visible; other failures are labelled, never an empty success',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({code:'forbidden'},{status:403})));
  expect(await readQueue()).toEqual({visible:false});
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({code:'dependency_unavailable'},{status:503})));
  await expect(readQueue()).rejects.toBeInstanceOf(ApiError);
  expect(reviewError(new ApiError(503))).toContain('暂不可用');expect(reviewError(new ApiError(404))).toContain('不在审核队列');
  expect(reviewError(new ApiError(401))).toContain('登录已失效');expect(reviewError(new Error('响应无效'))).toContain('格式无效');
});

test('strict parsing: malformed decision state or scores are rejected',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({items:[item],decisions})));
  const queue=await readQueue();expect(queue.visible&&queue.items[0].display_name).toBe('常用付款方式');
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({items:[item],decisions:{...decisions,enabled:'yes'}})));
  await expect(readQueue()).rejects.toThrow('响应无效');
  expect(()=>candidateDetail({...detail,merge_scores:{...scores,weighted_total:7}})).toThrow('响应无效');
  expect(()=>candidateDetail({...detail,evidence:[{...detail.evidence[0],span_start:5,span_end:2}]})).toThrow('响应无效');
  await expect(readCandidate('../../etc')).rejects.toThrow('候选编号无效');
});

const withClient=(element:ReturnType<typeof createElement>)=>createElement(QueryClientProvider,{client:new QueryClient()},element);
test('detail shows span, recall, scores with config version, dependent Claims and similar candidates',()=>{
  const html=renderToStaticMarkup(withClient(createElement(CandidateView,{detail:candidateDetail(detail)})));
  for(const fragment of ['我一般用花呗付款','第 0–8 字符','依赖 Claim：2 条','nx045-glue-v1-test64','eios:property:Consumer/favorite_sport（fts，21%）','9% / 38%','付款方式（已并入本候选，相似度 50%）'])
    expect(html).toContain(fragment);
});

test('without the governed Action the decision buttons stay disabled with the reason',()=>{
  const html=renderToStaticMarkup(createElement(DisabledDecisions,{decisions:candidateDetail({...detail,decisions:disabled}).decisions}));
  expect(html.match(/<button[^>]*disabled=""[^>]*aria-disabled="true"/g)).toHaveLength(3);
  for(const label of ['批准发布','并入已有定义','拒绝'])expect(html).toContain(label);
  expect(html).toContain('未启用');expect(html).not.toContain('onclick');
  expect(renderToStaticMarkup(createElement(QueueList,{items:[],selected:'',onSelect:()=>{}}))).toContain('审核队列为空');
});

test('workbench renders a loading label first, never a blank success',()=>{
  vi.stubGlobal('fetch',vi.fn(()=>new Promise(()=>{})));
  const client=new QueryClient();
  const html=renderToStaticMarkup(createElement(QueryClientProvider,{client},createElement(ReviewWorkbench)));
  expect(html).toContain('正在读取审核队列');
});

test('enabled decisions require a rationale first and offer only same-kind merge targets',()=>{
  const parsed=candidateDetail(detail);
  expect(mergeTargets(parsed)).toEqual(['eios:property:Consumer/favorite_sport']);
  expect(mergeTargets(candidateDetail({...detail,kind:'object_instance'}))).toEqual([]);
  const html=renderToStaticMarkup(withClient(createElement(DecisionBar,{detail:parsed})));
  expect(html).toContain('决定理由（必填，将写入审计记录）');expect(html).toContain('填写理由后才能提交决定');
  expect(html.match(/<button[^>]*disabled=""/g)).toHaveLength(3);
  expect(html).toContain('<option value="eios:property:Consumer/favorite_sport" selected="">');
});

test('decide posts the governed decision with CSRF and idempotency key; results and errors are labelled',async()=>{
  const calls:{path:string;init?:RequestInit}[]=[];
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{calls.push({path,init});
    if(path==='/api/v1/auth/csrf')return Response.json({authenticated:true,tenant_id:'t',principal_id:'p',restricted:false,csrf_token:'synthetic-csrf'});
    return Response.json({replay:false,decision_id:id,outcome:'publication_failed',reflow_status:'none',record:{},publication:{schema_revision_before:'Consumer@1',gate_failures:['property_already_exists'],applied_claim_count:0}});}));
  const result=await decide(id,{decision:'approve',expected_revision:2,rationale:'新增属性'},'review-synthetic-key-0001');
  const post=calls.find(c=>c.path.endsWith('/decisions'))!;
  expect(post.path).toBe(`/api/v1/review/candidates/${id}/decisions`);
  expect(new Headers(post.init!.headers).get('X-CSRF-Token')).toBe('synthetic-csrf');expect(new Headers(post.init!.headers).get('Idempotency-Key')).toBe('review-synthetic-key-0001');
  expect(JSON.parse(String(post.init!.body))).toEqual({decision:'approve',expected_revision:2,rationale:'新增属性'});
  expect(decisionMessage(result)).toBe('发布未通过门槛，候选仍在审核队列：property_already_exists');
  expect(decisionMessage(decisionResult({replay:true,decision_id:id,outcome:'rejected',reflow_status:'none'}))).toContain('重复提交');
  expect(decisionMessage(decisionResult({replay:false,decision_id:id,outcome:'published',reflow_status:'pending',publication:{schema_revision_before:'Consumer@1',schema_revision_after:'Consumer@2',gate_failures:[]}}))).toContain('Consumer@2');
  expect(()=>decisionResult({replay:false,decision_id:id,outcome:'publication_failed',reflow_status:'none',publication:{gate_failures:[]}})).toThrow('响应无效');
  await expect(decide(id,{decision:'reject',expected_revision:2,rationale:'  '},'review-synthetic-key-0002')).rejects.toThrow('请填写决定理由');
  expect(reviewError(new ApiError(409))).toContain('已被处理或已更新');expect(reviewError(new ApiError(501))).toContain('未启用');
});
