// K4j (R67 walkthrough): the server does not serve the 商业事件与费用 block yet (NX-027 not wired); the health tab says it is not
// connected, in the neutral style, never "暂不可用" (which reads as a failure). A served block renders as before.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {overview} from '../src/workbench/api';
import {COMMERCIAL_NOT_CONNECTED,HealthCards} from '../src/workbench/pages/Overview';

const off={status:'unavailable',data:null};
const sections=(commercial:unknown)=>overview({goals:off,commitments:off,actions:off,contact:off,takeovers:off,backlog:off,commercial});
const card=(html:string)=>html.slice(html.indexOf('aria-label="商业事件与费用"'));

test('an unavailable commercial block reads 尚未接入 in the neutral not_enabled state, no alert',()=>{
  const html=card(renderToStaticMarkup(h(HealthCards,{data:sections(off),onOpen:()=>{}})));
  expect(COMMERCIAL_NOT_CONNECTED).toBe('商业事件与费用尚未接入');
  expect(html).toContain(COMMERCIAL_NOT_CONNECTED);expect(html).toContain('data-state="not_enabled"');expect(html).toContain('这不是故障');
  expect(html.slice(0,html.indexOf('</article>'))).not.toMatch(/暂不可用|role="alert"|card-unavailable/);
  // The other unavailable blocks keep their warning.
  expect(renderToStaticMarkup(h(HealthCards,{data:sections(off),onOpen:()=>{}}))).toContain('队列积压暂不可用');
});

test('a served commercial block shows its figures',()=>{
  const html=card(renderToStaticMarkup(h(HealthCards,{data:sections({status:'ok',data:{budgets:{},last_24h:[]}}),onOpen:()=>{}})));
  expect(html).not.toContain(COMMERCIAL_NOT_CONNECTED);expect(html).not.toContain('data-state="not_enabled"');
});
