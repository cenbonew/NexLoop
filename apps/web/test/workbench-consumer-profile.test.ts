// @vitest-environment jsdom
// K5 §2.3 (docs/design/k5-consumer-evidence-views.md): the profile card on the consumer detail, on existing reads only. Upcoming
// commitments first (open, due from now, soonest first, at most 3); then the Object read's properties with the object revision; withheld
// keeps its badge and is never taken for empty; missing ones are counted, not dashed; without the Object read the consumer read's words.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {consumer} from '../src/workbench/api';
import {CONSUMER_NO_VALUES,ProfileCard,UPCOMING} from '../src/workbench/pages/ConsumerEvidence';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const cid='c'.repeat(64);
const values=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-object-values.valid.json'),'utf8'));
const view=(n:string,due:string)=>({commitment_id:n.repeat(64),consumer_id:cid,revision:1,
  properties:{status:'open',due_at:due,due_precision:'exact',late:false,made_by:{sender_kind:'agent'}},quote:`承诺 ${n} 的原话`,
  quote_status:'ok',source_available:true,origin:'claim',registered_at:'2026-10-18T01:00:00+00:00',
  evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[],consumer_display:{status:'ok',name:'合成客户'}});
let root:Root;let box:HTMLDivElement;let urls:string[];
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);urls=[];});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
function stub(object:unknown,commitments:unknown[]=[]){
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>{urls.push(url);
    if(url.startsWith('/api/v1/workbench/objects/'))return object===403?Response.json({error:'forbidden'},{status:403}):Response.json(object);
    if(url.startsWith('/api/v1/workbench/commitments?'))return Response.json({commitments,exceptions:[]});
    return Response.json({},{status:404});}));
}
async function mount(fallback='当前账号没有 Property 读取权限，字段与证据不显示',groups:string|null=null){
  const c=new QueryClient({defaultOptions:{queries:{retry:false}}});
  const data=consumer({consumer_id:cid,paused:false,restriction:null,conversations:[],commitments:[],properties:{status:'ok',values:{},withheld:[]}});
  await act(async()=>{root.render(h(QueryClientProvider,{client:c},h(ProfileCard,{data,fallback,groups})));});
  await act(async()=>{await new Promise(r=>setTimeout(r,0));});
}

test('upcoming commitments come first, asked open from now, soonest first, at most 3',async()=>{
  stub(values,[view('1','2026-11-05T00:00:00+00:00'),view('2','2026-11-06T00:00:00+00:00'),view('3','2026-11-07T00:00:00+00:00')]);
  await mount();
  const q=new URL(urls.find(u=>u.includes('/commitments?'))!,'http://x').searchParams;
  expect(Object.fromEntries(q)).toMatchObject({status_group:'open',consumer_id:cid,sort:'due_at',order:'asc',limit:String(UPCOMING)});
  expect(Date.parse(q.get('due_after')!)).toBeLessThanOrEqual(Date.now());
  expect([...box.querySelectorAll('h3')].map(x=>x.textContent)).toEqual(['即将发生的承诺','属性']);
  expect([...box.querySelectorAll('.profile-upcoming li')].map(li=>li.textContent!.includes('承诺 1'))).toEqual([true,false,false]);
});

test('properties: the object revision, values, withheld with its badge, missing counted and not dashed',async()=>{
  stub(values);await mount();
  expect(box.querySelector('.profile-revision')!.textContent).toBe('修订 r3 · Schema v2');
  expect(box.textContent).toContain('示例客户');expect(box.querySelectorAll('[data-code="withheld"]')).toHaveLength(1);
  expect(box.textContent).toContain('另有 1 个属性还没有值，未列出');expect(box.textContent).not.toContain('—');
  expect(box.textContent).toContain('没有即将到期的承诺');
});

test('no value at all says where values come from; all withheld is a permission, not empty',async()=>{
  stub({...values,properties:[{name:'nickname',status:'missing'},{name:'locale',status:'missing'}]});await mount();
  expect(box.textContent).toContain(CONSUMER_NO_VALUES);
  act(()=>root.unmount());root=createRoot(box);
  stub({...values,properties:[{name:'monthly_income',status:'withheld',reason:'restricted'}]});await mount();
  expect(box.textContent).not.toContain(CONSUMER_NO_VALUES);expect(box.querySelectorAll('[data-code="withheld"]')).toHaveLength(1);
});

test('without the Object read the consumer read\'s words stay, with the restricted groups named',async()=>{
  stub(403);await mount('没有你能读取的属性值','负责人标为受限的属性组不显示：finance');
  expect(box.textContent).toContain('没有你能读取的属性值');expect(box.textContent).toContain('finance');
  expect(box.querySelector('.profile-revision')).toBeNull();
});
