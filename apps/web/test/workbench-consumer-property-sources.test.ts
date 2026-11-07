// @vitest-environment jsdom
// Claim to Consumer (docs/implementation/claim-consumer-delivery-windows-design.md §4.3) on the contract example: the profile card's
// "来自对话" rows. delivery_windows as 周五 白天、周六 晚上; 来自 X 月 X 日的对话 from stated_at, effective_at secondary; corrected shows
// its state and no old value; the quote follows K5 (from the evidence graph, reviewers only, otherwise 原文受限); empty states said as such.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {WorkbenchError,consumer,propertySources} from '../src/workbench/api';
import {ProfileCard,QUOTE_ELSEWHERE,SOURCE_EMPTY,monthDay,windowsText} from '../src/workbench/pages/ConsumerEvidence';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const json=(name:string)=>JSON.parse(readFileSync(resolve(__dirname,`../../../packages/contracts/examples/${name}.valid.json`),'utf8'));
const ex=json('workbench-consumer-property-sources'),graphEx=json('workbench-consumer-evidence');
const cid=ex.consumer_id as string,B='b'.repeat(64);
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
/** The evidence graph's first claim made the source claim, with the given quote status. */
function graphWith(status:'ok'|'reviewer_only'|null){
  const g=structuredClone(graphEx);g.consumer_id=cid;
  if(status){const c=g.groups[0].items[0];c.claim_id=B;c.source.quote_status=status;c.source.quote=status==='ok'?'周五白天我在家':null;}
  g.edges=[];return g;
}
async function mount(sources:unknown,graph:unknown=graphWith('reviewer_only')){
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>{
    if(url.includes('/evidence-graph'))return graph?Response.json(graph):Response.json({error:'real_world_only'},{status:404});
    if(url.includes('/commitments?'))return Response.json({commitments:[],exceptions:[]});
    return Response.json({},{status:404});}));
  const c=new QueryClient({defaultOptions:{queries:{retry:false}}});
  const data=consumer({consumer_id:cid,paused:false,restriction:null,conversations:[],commitments:[],properties:{status:'ok',values:{},withheld:[]},
    ...(sources===undefined?{}:{property_sources:sources})});
  await act(async()=>{root.render(h(QueryClientProvider,{client:c},h(ProfileCard,{data,fallback:'无',groups:null})));});
  for(let i=0;i<3;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});
}
const row=()=>box.querySelector('.property-source')!;

test('parser: the example, a missing block is null, an unknown status is refused; windows read as 周五 白天、周六 晚上',()=>{
  const p=propertySources(ex);expect(p.items[0].status).toBe('applied');
  expect(windowsText(p.items[0])).toBe('周五 白天、周六 晚上');
  expect(windowsText({...p.items[0],value:[{weekday:'any',part:'any'},{weekday:'sat',part:'any'}]})).toBe('随时、周六 全天');
  expect(windowsText({...p.items[0],value:[{weekday:'holiday',part:'any'}]})).toBe(p.items[0].display);
  expect(consumer({consumer_id:cid,paused:false,restriction:null,conversations:[],commitments:[],properties:{status:'ok',values:{},withheld:[]}}).property_sources).toBeNull();
  // Asia/Shanghai, not the browser: 16:00Z is midnight there, 15:59:59Z the day before.
  expect(monthDay('2026-11-01T16:00:00Z')).toBe('11 月 2 日');expect(monthDay('2026-11-01T15:59:59Z')).toBe('11 月 1 日');
  expect(monthDay('2026-12-31T16:30:00+00:00')).toBe('1 月 1 日');
  const bad=structuredClone(ex);bad.items[0].status='stale';expect(()=>propertySources(bad)).toThrow(WorkbenchError);
});

test('an applied value: 送货时间, 来自 the stated day, 记入 secondary, the quote restricted for a non-reviewer',async()=>{
  await mount(ex);
  expect([...box.querySelectorAll('h3')].map(x=>x.textContent)).toContain('来自对话');
  expect(box.textContent).toContain('送货时间');
  expect(row().querySelector('.property-source-value')!.textContent).toBe('周五 白天、周六 晚上');
  expect(row().querySelector('.property-source-when')!.textContent).toBe('来自 11 月 2 日的对话');
  expect(row().querySelector('.property-source-effective')!.textContent).toBe('11 月 2 日记入');
  expect(row().querySelector('.source-quote')!.textContent).toBe('原文受限');
});

test('the quote for a reviewer comes from the evidence graph; a graph that is not available is 原文受限, a claim it does not list 原文见证据视图',async()=>{
  await mount(ex,graphWith('ok'));expect(row().querySelector('q.source-quote')!.textContent).toBe('周五白天我在家');
  act(()=>root.unmount());root=createRoot(box);
  await mount(ex,null);expect(row().querySelector('.source-quote')!.textContent).toBe('原文受限');
  act(()=>root.unmount());root=createRoot(box);
  await mount(ex,graphWith(null));expect(row().querySelector('.source-quote')!.textContent).toBe(QUOTE_ELSEWHERE);
});

test('corrected shows its state and never the old value; unknown, restricted and no items are said as such',async()=>{
  const corrected=structuredClone(ex);corrected.items[0].status='corrected';
  await mount(corrected);expect(box.textContent).toContain('已更正');expect(box.textContent).toContain(SOURCE_EMPTY.corrected);
  expect(box.textContent).not.toContain('周五');expect(box.querySelector('.property-source')).toBeNull();
  for(const [sources,text] of [[{...ex,items:[{...ex.items[0],value:null,display:null,status:'unknown',source:null,stated_at:null,effective_at:null}]},SOURCE_EMPTY.unknown],
    [{...ex,status:'restricted',items:[]},SOURCE_EMPTY.restricted],[{...ex,items:[]},SOURCE_EMPTY.none]] as const){
    act(()=>root.unmount());root=createRoot(box);await mount(sources);expect(box.textContent).toContain(text);expect(box.textContent).not.toContain('周五');
  }
  act(()=>root.unmount());root=createRoot(box);await mount(undefined);
  expect([...box.querySelectorAll('h3')].map(x=>x.textContent)).not.toContain('来自对话');
});
