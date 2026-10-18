import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,commitmentList,commitmentQueryString,consumerAlias,consumerQueryString,consumers,type CommitmentQuery} from '../src/workbench/api';
import {Commitments,commitmentSummary,dueBefore,nextStep,promiser} from '../src/workbench/pages/Commitments';
import {ConsumerName,Consumers} from '../src/workbench/pages/Consumers';
import {crumbs} from '../src/workbench/WorkbenchApp';
import {parseRoute,routePath} from '../src/workbench/route';
// Owner walkthrough 2026-10-18, batch 7 (F03 commitment semantics, F04 name states, F05 one scope, F09 list tools, F10 context).

const id=(c:string)=>c.repeat(64);
const view=(n:string,extra:Record<string,unknown>={},props:Record<string,unknown>={})=>({commitment_id:id(n),consumer_id:id('a'),revision:1,
  properties:{status:'open',due_at:'2026-10-20T00:00:00+00:00',due_precision:'exact',late:false,made_by:{sender_kind:'agent'},...props},quote:'我明天下午把发票寄给您，'+'顺丰到付请查收短信单号。'.repeat(6),
  quote_status:'ok',source_available:true,origin:'claim',registered_at:'2026-10-18T01:00:00+00:00',
  evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[],consumer_display:{status:'ok',name:'合成客户甲'},...extra});

test('F03: the summary is the readable promised words, cut at 60, else why not; promiser and next step in words',()=>{
  const list=commitmentList({commitments:[view('1'),view('2',{quote:null,quote_status:'restricted'}),view('3',{quote:null,quote_status:'unavailable'}),
    view('4',{},{status:'breached',made_by:{sender_kind:'human_takeover',sender_principal:'member-principal-x'}}),view('5',{},{status:'conditional'})],exceptions:[],
    counts:{open:5,ended:0,exceptions_in_scope:0,exceptions_all:0},total:5,next_cursor:null});
  const [a,b,c,d,e]=list.commitments;
  expect(commitmentSummary(a).length).toBe(61);expect(commitmentSummary(a).endsWith('…')).toBe(true);
  expect([commitmentSummary(b),commitmentSummary(c)]).toEqual(['内容受限','来源消息已删除']);
  expect([promiser(a),promiser(d)]).toEqual(['Agent','成员 memb']);   // D6: an alias, never the principal value
  expect([nextStep(a),nextStep(d),nextStep(e)]).toEqual(['系统跟进（承诺方）','负责人处理','等客户条件成立']);   // D1: derived only
  expect(nextStep({...a,status:'fulfilled'})).toBe('—');
  expect(a.consumer_display).toEqual({status:'ok',name:'合成客户甲'});
  expect(commitmentList({commitments:[{...view('6'),consumer_display:undefined}],exceptions:[]}).commitments[0].consumer_display).toBeNull();   // older server
  expect(()=>commitmentList({commitments:[],exceptions:[],next_cursor:'abc'})).toThrow(WorkbenchError);
});

test('F09: filters live in the URL, only declared keys with valid values survive, and the API query follows them',()=>{
  const r=parseRoute('/workbench/commitments','?status=ended&consumer='+id('a')+'&late=true&due_from=2026-10-01&sort=due_at&page=50&q=发票&evil=1&status=x');
  expect(r).toEqual({page:'commitments',query:{status:'ended',consumer:id('a'),late:'true',due_from:'2026-10-01',sort:'due_at',page:'50',q:'发票'}});
  expect(parseRoute(routePath(r).split('?')[0],'?'+routePath(r).split('?')[1])).toEqual(r);   // round trip
  expect(parseRoute('/workbench/commitments/'+id('1'),'?status=ended')).toEqual({page:'commitments',id:id('1')});   // details carry no filters
  expect(parseRoute('/workbench/consumers','?restricted=yes&open=true&sort=display_name')).toEqual({page:'consumers',query:{open:'true'}});
  const q:CommitmentQuery={status_group:'ended',consumer_id:id('a'),late:true,sort:'due_at',cursor:'50'};
  expect(Object.fromEntries(new URLSearchParams(commitmentQueryString(q)))).toEqual({status_group:'ended',consumer_id:id('a'),sort:'due_at',cursor:'50',late:'true',limit:'50'});
  expect(Object.fromEntries(new URLSearchParams(consumerQueryString({restricted:true,sort:'open_commitments',order:'desc'})))).toEqual({restricted:'true',sort:'open_commitments',order:'desc',limit:'50'});
});

test('F10: breadcrumbs lead back to the customer of a conversation and to that customer\'s commitments',()=>{
  const ctx={consumerId:id('a'),label:'合成客户甲'};
  expect(crumbs({page:'consumers',id:id('c'),sub:'conversation'},ctx).map(c=>[c.label,c.to?routePath(c.to):null])).toEqual([
    ['消费者','/workbench/consumers'],['合成客户甲','/workbench/consumers/'+id('a')],['会话',null]]);
  expect(crumbs({page:'commitments',id:id('1')},ctx).map(c=>[c.label,c.to?routePath(c.to):null])).toEqual([
    ['承诺 / 交付','/workbench/commitments'],['合成客户甲 的承诺','/workbench/commitments?consumer='+id('a')+'&status=all'],['承诺详情',null]]);
  expect(crumbs({page:'consumers',id:id('c'),sub:'conversation'}).map(c=>c.label)).toEqual(['消费者','会话']);   // before the customer is known
});

function render(node:ReturnType<typeof h>,key:unknown[],data:unknown){
  const cache=new QueryClient();cache.setQueryData(key,data);
  return renderToStaticMarkup(h(QueryClientProvider,{client:cache},node));
}

test('F03/F05: the list answers who, what, when, status and next step; tabs carry counts; exceptions are this scope\'s',()=>{
  const data=commitmentList({commitments:[view('1')],exceptions:[{subject_ref:'commitment:'+id('1'),consumer_id:id('a'),reason:'deadline_breached',raised_at:'2026-10-19T01:00:00+00:00'}],
    counts:{open:1,ended:2,exceptions_in_scope:1,exceptions_all:3},total:1,next_cursor:null});
  const query={consumer:id('a')};
  const html=render(h(Commitments,{onOpen:()=>{},query}),['workbench','commitments',{status_group:'open',consumer_id:id('a'),late:undefined,due_after:undefined,due_before:undefined,
    sort:undefined,order:undefined,cursor:undefined}],data);
  for(const header of ['承诺','客户','期限','状态','承诺方','下一步','ID'])expect(html).toContain('>'+header+'</th>');
  expect(html).toContain('我明天下午把发票寄给您');expect(html).toContain('合成客户甲');expect(html).toContain('系统跟进（承诺方）');
  expect(html).toContain('未结束<span class="tab-count">（1）</span>');expect(html).toContain('已结束<span class="tab-count">（2）</span>');
  expect(html).toContain('本范围的异常（1）');expect(html).toContain('全部承诺异常（3）');
  expect(html).toContain('客户：合成客户甲');expect(html).toContain('共 1 条');   // the customer filter as a chip; the total
});

test('F04: the same name state reads the same everywhere; restricted and missing keep the stable alias',()=>{
  const name=(display:Parameters<typeof ConsumerName>[0]['display'])=>renderToStaticMarkup(h(ConsumerName,{consumerId:id('a'),display}));
  expect(name({status:'restricted',reason:'withheld'})).toContain(consumerAlias(id('a'))+'<small>（名称受限）</small>');
  expect(name({status:'restricted',reason:'forbidden'})).toContain('（名称受限）');
  expect(name({status:'missing'})).toContain('（未登记名称）');expect(name({status:'failed'})).toContain('（名称加载失败）');
  const page=consumers({items:[{consumer_id:id('a'),restricted:true,open_commitments:2,active_plans:0,conversations:1,display:{status:'restricted',reason:'withheld'}}],
    next_cursor:null,total:1,tenant_total:4});
  const html=render(h(Consumers,{onOpen:()=>{},onCommitments:()=>{},query:{restricted:'true'}}),['workbench','consumers',{restricted:true,has_open_commitments:undefined,sort:'id',
    order:undefined,cursor:undefined}],page);
  expect(html).toContain('（名称受限）');expect(html).toContain('共 1 条');
  expect(html).toContain('aria-label="查看 '+consumerAlias(id('a'))+'（名称受限） 的 2 条未结束承诺"');   // the count leads to the filtered commitments
});

test('PR #9 review: "到 <day>" sends the next day\'s midnight in Beijing as the exclusive bound (23:59:59.5 that day included)',()=>{
  expect(dueBefore('2026-10-20')).toBe('2026-10-21T00:00:00+08:00');
  expect([dueBefore('2026-10-31'),dueBefore('2026-12-31'),dueBefore('2028-02-28')]).toEqual(['2026-11-01T00:00:00+08:00','2027-01-01T00:00:00+08:00','2028-02-29T00:00:00+08:00']);
  const due=Date.parse('2026-10-20T23:59:59.5+08:00');
  expect(due<Date.parse(dueBefore('2026-10-20'))&&due>=Date.parse('2026-10-20T00:00:00+08:00')).toBe(true);
  expect(Object.fromEntries(new URLSearchParams(commitmentQueryString({due_before:dueBefore('2026-10-20')}))).due_before).toBe('2026-10-21T00:00:00+08:00');
});
