// @vitest-environment jsdom
// Owner report from stage (2026-10-28): the persona page read as "only one persona" - with none it showed "暂无人设" and an editor,
// and a second persona meant picking "新人设" in "从哪个人设开始" and changing the id. Now every persona is one row (current version,
// state, where it is assigned), "新建人设" and "发布新版本" are separate entries into the editor, and the empty state says a tenant
// can have several personas assigned to different customers.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {me,OPERATION_ACTION,personaOverview} from '../src/workbench/api';
import {Personas} from '../src/workbench/pages/Personas';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const doc=(name:string)=>({display_name:name,tone:'亲切',expression:{max_chars:200,emoji:'few'}});
const version=(persona_id:string,v:number,status:'published'|'retired',name:string)=>({persona_id,version:v,status,display_name:name,created_at:'2026-10-19T02:00:00+00:00',document:doc(name)});
const raw=(personas:unknown[],rules:unknown=null)=>({personas,rules,ai_disclosure:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:0},consumer:null});
const overview=(personas:unknown[],rules:unknown=null)=>personaOverview(raw(personas,rules));
let server:unknown=raw([]);  // what a re-read after a write returns
const ownerRaw={principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',OPERATION_ACTION.publish_operator_persona,OPERATION_ACTION.publish_persona_rules]};
const owner=me(ownerRaw);
const operator=me({principal_id:'synthetic-operator',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const sent:{path:string;body:unknown}[]=[];
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{sent.length=0;
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    if(init?.method==='POST'){sent.push({path,body:JSON.parse(String(init.body))});return new Response(JSON.stringify({status:'ok'}),{status:200});}
    if(path==='/api/v1/workbench/operator-personas')return new Response(JSON.stringify(server),{status:200});
    if(path==='/api/v1/workbench/me')return new Response(JSON.stringify(ownerRaw),{status:200});
    return new Response('{}',{status:503});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text);
function mount(view:unknown,member=owner){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','operator-personas'],view);c.setQueryData(['workbench','me'],member);
  act(()=>root.render(h(QueryClientProvider,{client:c},h(Personas,{}))));
}
const fill=(id:string,value:string)=>act(()=>{const el=box.querySelector<HTMLInputElement|HTMLTextAreaElement>('#'+id)!;
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')!.set!.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});

test('no persona yet: the empty state says several can be made; 新建人设 opens the editor at version 1 with focus on its title',async()=>{
  server=raw([version('lan',1,'published','小蓝')]);
  mount(overview([]));
  expect(box.textContent).toContain('还没有人设');expect(box.textContent).toContain('可以创建多个人设，按分配规则给不同客户使用');
  expect(box.querySelector('form.persona-editor')).toBeNull();  // the editor is an explicit step, not the page
  const create=button('新建人设')!;expect(create.getAttribute('aria-expanded')).toBe('false');
  act(()=>create.click());
  const form=box.querySelector<HTMLFormElement>('form.persona-editor')!;
  expect(form.querySelector('h2')!.textContent).toBe('新建人设');expect(document.activeElement).toBe(form.querySelector('h2'));
  expect(box.querySelector('#persona-base')).toBeNull();  // no "从哪个人设开始"
  const submit=form.querySelector<HTMLButtonElement>('button[type=submit]')!;
  expect(submit.disabled).toBe(true);expect(form.querySelector('.disabled-reason')?.textContent).toBe('先填写人设标识（小写字母开头）。');
  fill('persona-persona_id','lan');fill('persona-display_name','小蓝');fill('persona-tone','简洁');fill('persona-reason','第二个人设');
  expect(submit.disabled).toBe(false);
  await act(async()=>{submit.click();});await settle();
  expect(sent).toEqual([{path:'/api/v1/workbench/actions/publish_operator_persona',
    body:{persona_id:'lan',version:1,status:'published',document:{display_name:'小蓝',tone:'简洁',expression:{max_chars:200,emoji:'none'}},reason:'第二个人设'}}]);
  // Done: the editor closes, the list says what was published and how customers get it; focus is back on 新建人设.
  expect(box.querySelector('form.persona-editor')).toBeNull();
  expect(box.querySelector('.notice-success')?.textContent).toContain('已发布 lan@1');expect(box.querySelector('.notice-success')?.getAttribute('role')).toBe('status');
  expect(box.querySelector('table[aria-label="人设"]')?.textContent).toContain('lan@1');expect(document.activeElement).toBe(button('新建人设'));
});

test('several personas: one row each with its current version, state and assignment; a new one cannot take a used id',()=>{
  mount(overview([version('xiaohe',1,'retired','小禾'),version('xiaohe',2,'published','小禾'),version('lan',1,'published','小蓝'),version('old',1,'retired','旧人设')],
    {version:1,default_persona_id:'xiaohe',rules:[{when:{property:'tier',equals:'gold'},persona_id:'lan'},{when:{property:'tier',equals:'vip'},persona_id:'lan'}],published_at:'2026-10-19T02:00:00+00:00'}));
  expect(box.querySelector('#persona-list-title')!.textContent).toBe('人设（3 个）');
  const rows=[...box.querySelectorAll('table[aria-label="人设"] tbody tr')].map(r=>[...r.querySelectorAll('td,th')].map(c=>c.textContent));
  expect(rows.map(r=>r.slice(0,4))).toEqual([
    ['小蓝 lan','lan@1','✓已发布','分群规则第 1、2 条'],['旧人设 old','没有在用的版本','–已停用','未分配'],['小禾 xiaohe','xiaohe@2','✓已发布','租户默认']]);
  expect(box.querySelectorAll('table[aria-label="人设版本"] tbody tr')).toHaveLength(4);  // every version still listed for retiring / re-enabling
  act(()=>button('新建人设')!.click());
  fill('persona-persona_id','xiaohe');fill('persona-display_name','重复');fill('persona-tone','x');fill('persona-reason','x');
  expect(box.querySelector('.disabled-reason')?.textContent).toBe('标识 xiaohe 已经有人设在用；要修改它，请在列表里选“发布新版本”。');
});

test('发布新版本 edits one persona: its id fixed, its content prefilled, the next version; 关闭 returns focus to the row',async()=>{
  mount(overview([version('xiaohe',1,'retired','小禾'),version('xiaohe',2,'published','小禾'),version('lan',1,'published','小蓝')]));
  const edit=[...box.querySelectorAll<HTMLButtonElement>('table[aria-label="人设"] button')].filter(b=>b.textContent==='发布新版本…')[1];  // xiaohe's row
  act(()=>edit.click());
  const form=box.querySelector<HTMLFormElement>('form.persona-editor')!;
  expect(form.querySelector('h2')!.textContent).toBe('为 小禾 发布新版本 xiaohe@3');expect(form.querySelector('#persona-persona_id')).toBeNull();
  expect(box.querySelector<HTMLInputElement>('#persona-display_name')!.value).toBe('小禾');expect(edit.getAttribute('aria-expanded')).toBe('true');
  act(()=>button('关闭')!.click());await settle();
  expect(box.querySelector('form.persona-editor')).toBeNull();expect(document.activeElement).toBe(edit);
});

test('a member without the publish Action sees the list but no entry into the editor',()=>{
  mount(overview([version('xiaohe',2,'published','小禾')]),operator);
  expect(box.querySelector('table[aria-label="人设"]')).not.toBeNull();
  expect(button('新建人设')).toBeUndefined();expect(button('发布新版本…')).toBeUndefined();
});
