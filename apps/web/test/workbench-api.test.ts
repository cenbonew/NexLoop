import {afterEach,expect,test,vi} from 'vitest';
import {INTENT_STATE_TEXT,STATE_TEXT,SECTION_TEXT,WorkbenchError,commitmentView,contact,failureOf,matchedText,overview,readContact,readOverview,readWorkbenchSession,section} from '../src/workbench/api';
import {dueText,quoteText} from '../src/workbench/pages/Commitments';
import {when} from '../src/workbench/pages/Consumers';
import {dispatchText} from '../src/workbench/pages/Actions';
import {parseRoute,routePath} from '../src/workbench/route';

const id=(c:string)=>c.repeat(64);
const okOverview={goals:{status:'ok',data:{goals:[{goal_id:'grow',goal_kind:'long_term',objective:'提高续费',current_version:2,key_results:1}],control_revision:7,paused_scopes:[]}},
  commitments:{status:'ok',data:{open:2,breached:1,exceptions:[{reason:'breached',count:1}]}},actions:{status:'ok',data:{count:0,oldest_age_seconds:null}},
  contact:{status:'forbidden',data:null},takeovers:{status:'unavailable',data:null},backlog:{status:'unavailable',data:null},commercial:{status:'unavailable',data:null}};
const restricted={message_id:id('4'),rule_version:1,rule_id:'stop-contact',certainty:'refuse',matched_text:null,matched_text_status:'restricted',recorded_at:'2026-10-10T09:00:00+00:00'};
const restriction={...restricted,consumer_id:id('2'),active:true,control_revision:7,conversation_id:id('5'),restricted_at:'2026-10-10T09:00:00+00:00',released_at:null,released_by:null,release_reason:null,hits:[restricted]};
const view={commitment_id:id('1'),consumer_id:id('2'),revision:2,properties:{status:'open',due_at:'2026-10-11T10:00:00+08:00',due_precision:'latest_bound'},quote:null,quote_status:'restricted',
  source_available:true,origin:'claim',registered_at:'2026-10-10T09:00:00+00:00',evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[]};
afterEach(()=>vi.unstubAllGlobals());
function respond(status:number,body:unknown){vi.stubGlobal('fetch',vi.fn(async()=>new Response(typeof body==='string'?body:JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})));}

test('AT-045: exact failure kinds from status codes, never an empty success',async()=>{
  expect([401,403,404,422,500,503].map(failureOf)).toEqual(['unauthenticated','forbidden','not_found','invalid_request','unavailable','unavailable']);
  for(const [status,kind] of [[401,'unauthenticated'],[403,'forbidden'],[503,'unavailable']] as const){
    respond(status,{code:kind});await expect(readOverview()).rejects.toMatchObject({kind});
  }
  vi.stubGlobal('fetch',vi.fn(async()=>{throw new TypeError('network down');}));
  await expect(readOverview()).rejects.toMatchObject({kind:'unavailable'});
  respond(200,'not json');await expect(readOverview()).rejects.toMatchObject({kind:'invalid'});
  expect(STATE_TEXT.unavailable).toContain('不代表没有数据');expect(STATE_TEXT.forbidden).toContain('403');
});

test('AT-045: partial overview keeps each block\'s own status; a malformed block rejects the whole response',async()=>{
  respond(200,okOverview);
  const value=await readOverview();
  expect(value.contact).toEqual({status:'forbidden',data:null});expect(value.takeovers.status).toBe('unavailable');
  expect(value.commitments).toEqual({status:'ok',data:{open:2,breached:1,exceptions:[{reason:'breached',count:1}]}});
  expect(SECTION_TEXT.unavailable).toContain('不代表数量为零');
  expect(()=>overview({...okOverview,contact:{status:'forbidden',data:{restricted:0,escalations:0}}})).toThrow(WorkbenchError);
  expect(()=>overview({...okOverview,goals:{status:'ok',data:{goals:'x'}}})).toThrow(WorkbenchError);
  const {commercial,...missing}=okOverview;void commercial;expect(()=>overview(missing)).toThrow(WorkbenchError);
  expect(()=>section({status:'weird',data:null},d=>d)).toThrow(WorkbenchError);
});

test('AT-003 interface: restricted matched text stays hidden and labelled; a leaked text under "restricted" is rejected',async()=>{
  respond(200,{restrictions:[restriction],escalations:[]});
  const value=await readContact();
  expect(value.restrictions[0].matched_text).toBeNull();
  expect(matchedText(value.restrictions[0])).toBe('已命中规则 stop-contact（原文需授权（缺少该消息的读取权限））');
  expect(()=>contact({restrictions:[{...restriction,matched_text:'请不要再给我发短信了'}],escalations:[]})).toThrow(WorkbenchError);
  expect(matchedText({...restricted,matched_text:'不要再给我发',matched_text_status:'ok'})).toBe('不要再给我发');
});

test('commitment view: status only from the object, latest bound is a derived value, problem column is unavailable (AT-040/041)',()=>{
  const value=commitmentView(view);
  expect(value.status).toBe('open');expect(dueText(value)).toBe('最晚界 '+when('2026-10-11T10:00:00+08:00')+'（推导值）');   // the object's due_precision (walkthrough F03)
  expect(quoteText(value)).toContain('原文需授权');
  expect(quoteText(commitmentView({...view,quote:'我们明天下午前给您反馈',quote_status:'ok'}))).toBe('我们明天下午前给您反馈');
  expect(quoteText(commitmentView({...view,quote_status:'unavailable'}))).toBe('来源消息已删除，原文不可用');
  expect(()=>commitmentView({...view,evidence:{...view.evidence,problem_resolved:'resolved'}})).toThrow(WorkbenchError);
  expect(()=>commitmentView({...view,quote_status:'guessed'})).toThrow(WorkbenchError);
});

test('unknown effects read "待核对"; without the slice 2 prediction the page says so instead of guessing',()=>{
  expect(INTENT_STATE_TEXT.unknown).toBe('待核对');
  expect(dispatchText({status:'unavailable'})).toBe('派发预判暂不可用');
  expect(dispatchText({status:'ok',prediction:{dispatchable:false,reason:'control_paused'}})).toBe('已暂停');
  expect(dispatchText({status:'ok',prediction:{dispatchable:false,reason:'control_revision_stale'}})).toContain('等待复评');
  expect(dispatchText({status:'ok',prediction:{dispatchable:true,reason:null}})).toBe('可派发');
});

test('workbench session uses its own realm and treats 401 as signed out',async()=>{
  const calls:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>{calls.push(path);return new Response('{}',{status:401});}));
  expect(await readWorkbenchSession()).toBeNull();expect(calls).toEqual(['/api/v1/workbench/auth/session']);
});

test('routes are URL state and reject malformed ids',()=>{
  expect(parseRoute('/workbench')).toEqual({page:'overview'});
  expect(parseRoute('/workbench/commitments/'+id('a'))).toEqual({page:'commitments',id:id('a')});
  expect(parseRoute('/workbench/consumers/conversations/'+id('b'))).toEqual({page:'consumers',id:id('b'),sub:'conversation'});
  expect(parseRoute('/workbench/commitments/not-an-id')).toEqual({page:'commitments'});
  expect(parseRoute('/workbench/unknown')).toEqual({page:'overview'});
  expect(routePath({page:'consumers',id:id('c'),sub:'conversation'})).toBe('/workbench/consumers/conversations/'+id('c'));
});

test('ADR-025: owner-restricted properties are listed as withheld; the audit is parsed strictly',async()=>{
  const {consumer,audit}=await import('../src/workbench/api');
  const detail=consumer({consumer_id:id('2'),paused:false,restriction:null,conversations:[],commitments:[],properties:{status:'ok',values:{favorite_sport:'网球'},withheld:['income_band']}});
  expect(detail.properties).toEqual({status:'ok',values:{favorite_sport:'网球'},withheld:['income_band']});
  expect(audit({items:[{audit_id:3,principal_id:'p',role:'owner',object_kind:'message',target_resource:'eios:object:Message/'+id('4'),read_purpose:'conversation',read_at:'2026-10-10T09:00:00+00:00'}]})[0].read_purpose).toBe('conversation');
  expect(()=>audit({items:[{audit_id:3,principal_id:'p',role:'owner',object_kind:'claim',target_resource:'x',read_purpose:'x',read_at:'x'}]})).toThrow(WorkbenchError);
});

test('reviewer evidence: pending items show bodies or the restricted label; an ended review shows nothing readable',async()=>{
  const {reviewEvidence,reviewQueue,REVIEW_ENDED_TEXT}=await import('../src/workbench/api');
  const cid='5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f';
  expect(reviewQueue({items:[{candidate_id:cid,kind:'property',display_name:'联系偏好',created_at:'2026-10-10T09:00:00+00:00',evidence_count:1}]})[0].evidence_count).toBe(1);
  const pending=reviewEvidence({candidate_id:cid,status:'pending_review',kind:'property',display_name:'联系偏好',
    evidence:[{claim_id:'c1',predicate:'联系偏好',source_message_id:id('4'),content:{status:'ok',body:'请不要再给我发短信了'}},{claim_id:'c2',predicate:null,source_message_id:id('5'),content:{status:'restricted'}}]});
  expect(pending.status==='pending_review'&&pending.evidence.map(e=>e.content.status)).toEqual(['ok','restricted']);
  expect(reviewEvidence({candidate_id:cid,status:'ended',evidence:[]})).toEqual({candidate_id:cid,status:'ended'});
  expect(()=>reviewEvidence({candidate_id:cid,status:'ended',evidence:[{claim_id:'c1'}]})).toThrow(WorkbenchError);
  expect(REVIEW_ENDED_TEXT).toContain('不再可读');
  expect(parseRoute('/workbench/knowledge/'+cid)).toEqual({page:'knowledge',id:cid});
});

test('desktop usability: readable names, a non-identifying alias otherwise, and conversations with their last message time',async()=>{
  const {consumers,consumer,consumerAlias,DISPLAY_NOTE}=await import('../src/workbench/api');
  const {when}=await import('../src/workbench/pages/Consumers');
  const row={consumer_id:id('2'),restricted:false,open_commitments:0,active_plans:0,conversations:1};
  expect(consumers({items:[{...row,display:{status:'ok',name:'合成客户甲'}}],next_cursor:null}).items[0].display).toEqual({status:'ok',name:'合成客户甲'});
  // F04: one vocabulary; older servers' withheld / forbidden / absent map onto it.
  expect(consumers({items:[{...row,display:{status:'restricted',reason:'withheld'}}],next_cursor:null}).items[0].display).toEqual({status:'restricted',reason:'withheld'});
  expect(consumers({items:[{...row,display:{status:'withheld'}}],next_cursor:null}).items[0].display).toEqual({status:'restricted',reason:'withheld'});
  expect(consumers({items:[{...row,display:{status:'forbidden'}}],next_cursor:null}).items[0].display).toEqual({status:'restricted',reason:'forbidden'});
  expect(consumers({items:[row],next_cursor:null}).items[0].display).toEqual({status:'missing'});   // an older server
  expect(consumers({items:[{...row,display:{status:'failed'}}],next_cursor:null}).items[0].display).toEqual({status:'failed'});
  expect(()=>consumers({items:[{...row,display:{status:'restricted',reason:'guess'}}],next_cursor:null})).toThrow(WorkbenchError);
  expect(()=>consumers({items:[{...row,display:{status:'guess',name:'x'}}],next_cursor:null})).toThrow(WorkbenchError);
  const alias=consumerAlias(id('2'));expect(alias).toMatch(/^客户 \S{4}·\S{3}$/);expect(alias.length).toBeLessThan(id('2').length);
  expect([DISPLAY_NOTE.restricted,DISPLAY_NOTE.missing,DISPLAY_NOTE.failed]).toEqual(['名称受限','未登记名称','名称加载失败']);
  const detail=consumer({consumer_id:id('2'),paused:false,restriction:null,display:{status:'withheld'},commitments:[],properties:{status:'ok',values:{},withheld:['display_name']},
    conversations:[{conversation_id:id('3'),last_sequence:4,last_message_at:'2026-10-17 13:13:54.071455+00'},{conversation_id:id('4'),last_sequence:0,last_message_at:null}]});
  expect(detail.conversations.map(c=>c.last_message_at)).toEqual(['2026-10-17 13:13:54.071455+00',null]);
  expect(when('2026-10-17 13:13:54.071455+00').startsWith(new Date('2026-10-17T13:13:54.071Z').toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})+'（')).toBe(true);   // Beijing + relative (F15)
  expect(when(null)).toBe('—');expect(when('not a time')).toBe('not a time');
});

test('X3 (l2/ui-backend-x1-x4, fixtures): the newest conversation time per customer, its sort key in the URL and the query; an older server omits it',async()=>{
  const {consumers,consumerQueryString}=await import('../src/workbench/api');
  const row={consumer_id:id('2'),restricted:false,open_commitments:0,active_plans:0,conversations:1};
  expect(consumers({items:[{...row,last_conversation_at:'2026-10-28T02:30:00.123456Z'},{...row,consumer_id:id('3'),last_conversation_at:null}],next_cursor:null}).items
    .map(c=>c.last_conversation_at)).toEqual(['2026-10-28T02:30:00.123456Z',null]);
  expect('last_conversation_at' in consumers({items:[row],next_cursor:null}).items[0]).toBe(false);
  expect(parseRoute('/workbench/consumers','?sort=last_conversation_at&order=desc')).toEqual({page:'consumers',query:{sort:'last_conversation_at',order:'desc'}});
  expect(consumerQueryString({sort:'last_conversation_at',order:'desc'})).toContain('sort=last_conversation_at');
});

test('X3: the consumer list shows a 最近会话 column only when the server sends the time, and offers the sort',async()=>{
  const {createElement:h}=await import('react');const {renderToStaticMarkup}=await import('react-dom/server');
  const {QueryClient,QueryClientProvider}=await import('@tanstack/react-query');
  const {consumers}=await import('../src/workbench/api');const {Consumers}=await import('../src/workbench/pages/Consumers');
  const row={consumer_id:id('2'),restricted:false,open_commitments:0,active_plans:0,conversations:1,display:{status:'ok',name:'合成客户甲'}};
  const page=(items:unknown[])=>{const c=new QueryClient({defaultOptions:{queries:{staleTime:Infinity}}});
    c.setQueryData(['workbench','consumers',{sort:'id'}],consumers({items,next_cursor:null}));
    return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Consumers,{onOpen:()=>{}})));};
  const withTime=page([{...row,last_conversation_at:'2026-10-28T02:30:00.000000Z'}]);
  expect(withTime).toContain('>最近会话</th>');expect(withTime).toContain('<option value="last_conversation_at:desc">最近有会话的在前</option>');
  expect(page([row])).not.toContain('>最近会话</th>');
});
