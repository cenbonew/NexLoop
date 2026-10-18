import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {RESTRICTED_TEXT,WorkbenchError,consumerHistory} from '../src/workbench/api';
import {CustomerHistory,beijing} from '../src/workbench/pages/Consumers';
// "客户近期往来" (owner feedback 2026-10-18): the customer's exchanges across conversations, newest first, collapsed to the latest one.

const id=(c:string)=>c.repeat(64);
const item=(n:number,conversation:string,extra:Record<string,unknown>={})=>({id:id(String(n%10)),conversation_id:id(conversation),sequence:n,
  accepted_at:`2026-10-18 0${n%10}:00:00.000000+00`,direction:'inbound',sender_kind:'consumer',content:{status:'ok',actor:null,body:`消息 ${n}`},...extra});

test('the history response is parsed strictly',()=>{
  const v=consumerHistory({consumer_id:id('a'),conversations:2,next_cursor:'2026-10-18 01:00:00+00|'+id('1'),items:[item(3,'b'),item(2,'c',{content:{status:'restricted'}})]});
  expect(v.items.map(i=>i.content.status)).toEqual(['ok','restricted']);expect(v.next_cursor).toContain('|');
  expect(()=>consumerHistory({consumer_id:id('a'),conversations:1,next_cursor:null,items:[item(1,'b',{direction:'sideways'})]})).toThrow(WorkbenchError);
  expect(()=>consumerHistory({consumer_id:id('a'),conversations:1,next_cursor:null,items:[item(1,'b',{content:{status:'guess'}})]})).toThrow(WorkbenchError);
});

test('Beijing time whatever the machine is set to',()=>{
  expect(beijing('2026-10-17 16:30:00+00')).toMatch(new RegExp('^'+new Date('2026-10-17T16:30:00Z').toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})+'（\\d+ (分钟|小时|天)前）$'));
  expect(beijing('2026-10-17 16:30:00+00')).toContain('2026/10/18');   // past midnight in Beijing
});

test('collapsed it shows the latest exchange with its conversation and time; restricted bodies are labelled',()=>{
  const cache=new QueryClient();
  const page={consumer_id:id('a'),conversations:2,next_cursor:'x|'+id('1'),items:[item(3,'b'),item(2,'c',{content:{status:'restricted'}})]};
  cache.setQueryData(['workbench','consumer-history',id('a')],{pages:[consumerHistory(page)],pageParams:['']});
  const html=renderToStaticMarkup(h(QueryClientProvider,{client:cache},h(CustomerHistory,{consumerId:id('a'),currentConversationId:id('b')})));
  expect(html).toContain('客户近期往来');expect(html).toContain('2 个会话');expect(html).toContain('北京时间');
  expect(html).toContain('本会话');expect(html).toContain('消息 3');expect(html).not.toContain('消息 2');   // only the latest while collapsed
  expect(html).toContain('展开最近 10 条');expect(html).not.toContain(RESTRICTED_TEXT);
  expect(html).toContain(beijing('2026-10-18 03:00:00.000000+00'));
});
