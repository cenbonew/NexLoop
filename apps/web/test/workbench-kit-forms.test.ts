// Workbench UI kit, batch 2 (docs/design/workbench-ui-spec.md §9 and appendix B): form fields, choice groups, the view switch and
// the tabs ↔ tabpanel link; and the pages that had the layout problems (AI disclosure, outreach controls).
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {CheckboxGroup,ChoiceGroup,FormField,Segmented,describedBy} from '../src/workbench/kit';
import {TabPanel,Tabs} from '../src/workbench/ui';
import {me} from '../src/workbench/api';

const r=(e:Parameters<typeof renderToStaticMarkup>[0])=>renderToStaticMarkup(e);

test('FormField: the label above its control, the hint and the error under it with their ids; width caps the control',()=>{
  const html=r(h(FormField,{id:'f-reason',label:'原因',hint:'最多 500 字',error:'必填',width:'sm'},h('input',{id:'f-reason','aria-describedby':describedBy('f-reason',{hint:1,error:1})})));
  expect(html).toBe('<div class="field form-field field-sm"><label for="f-reason">原因</label><input id="f-reason" aria-describedby="f-reason-hint f-reason-error"/>'
    +'<p class="field-hint" id="f-reason-hint">最多 500 字</p><p class="field-error" id="f-reason-error" role="alert">必填</p></div>');
  expect(describedBy('x')).toBeUndefined();
  expect(r(h(FormField,{id:'a',label:'A'},h('input',{id:'a'})))).toContain('class="field form-field field-md"');
});

test('ChoiceGroup / CheckboxGroup: a fieldset with a legend, one option per line, hints under their text; cards for long options',()=>{
  const radio=r(h(ChoiceGroup<'deflect'|'handoff'>,{legend:'被直接询问是否 AI 时',name:'ai-asked',value:'handoff',onChange:()=>{},
    options:[{value:'deflect',label:'用回避话术回答',hint:'话术为空时转人工'},{value:'handoff',label:'转人工'}]}));
  expect(radio).toMatch(/^<fieldset class="choice-group choice-stack"><legend>被直接询问是否 AI 时<\/legend><label class="choice-option"><input type="radio" name="ai-asked" value="deflect"/);
  expect(radio).toContain('<span class="choice-hint">话术为空时转人工</span>');expect(radio).toContain('class="choice-option choice-checked"><input type="radio" name="ai-asked" checked="" value="handoff"');
  expect(radio.match(/<label class="choice-option/g)?.length).toBe(2);
  expect(r(h(ChoiceGroup<'a'>,{legend:'L',name:'n',value:'a',onChange:()=>{},layout:'cards',options:[{value:'a',label:'A'}]}))).toContain('class="choice-group choice-cards"');
  const boxes=r(h(CheckboxGroup,{legend:'附加处理',items:[{key:'a',label:'拦截',checked:true,onChange:()=>{}},{key:'b',label:'标识',hint:'渠道支持时',checked:false,onChange:()=>{}}]}));
  expect(boxes).toMatch(/^<fieldset class="choice-group choice-stack"><legend>附加处理<\/legend>/);
  expect(boxes.match(/type="checkbox"/g)?.length).toBe(2);expect(boxes).toContain('<span class="choice-hint">渠道支持时</span>');
});

test('Segmented: one group, aria-pressed on the current value; Tabs with an id link each tab to the one TabPanel',()=>{
  const seg=r(h(Segmented<'table'|'graph'>,{label:'查看方式',value:'graph',onChange:()=>{},options:[{value:'table',label:'Table'},{value:'graph',label:'Graph'}]}));
  expect(seg).toBe('<div class="segmented" role="group" aria-label="查看方式"><button type="button" aria-pressed="false">Table</button><button type="button" aria-pressed="true">Graph</button></div>');
  const tabs=r(h(Tabs<'a'|'b'>,{id:'t',label:'设置',value:'b',onChange:()=>{},tabs:[{key:'a',label:'A'},{key:'b',label:'B'}]}));
  expect(tabs).toContain('id="t-tab-a" aria-controls="t-panel"');expect(tabs).toContain('aria-selected="true" tabindex="0" id="t-tab-b" aria-controls="t-panel"');
  expect(r(h(TabPanel,{id:'t',value:'b'},'内容'))).toBe('<div role="tabpanel" id="t-panel" aria-labelledby="t-tab-b" tabindex="0" class="tab-panel">内容</div>');
  expect(r(h(Tabs<'a'>,{label:'x',value:'a',onChange:()=>{},tabs:[{key:'a',label:'A'}]}))).not.toContain('aria-controls');  // without id: unchanged
});

test('AI disclosure: the select, then the radio group, then the checkboxes in their own fieldset - never on one line; read side as key-value',async()=>{
  const {Personas}=await import('../src/workbench/pages/Personas');
  const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.ai_disclosure.set:1']});
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','operator-personas'],{personas:[],rules:null,consumer:null,ai_disclosure:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:3}});
  c.setQueryData(['workbench','me'],owner);
  const page=renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Personas,{})));
  expect(page).toContain('role="tabpanel" id="persona-tabs-panel" aria-labelledby="persona-tabs-tab-personas"');
  const {Disclosure}=await import('../src/workbench/pages/Personas');
  const html=renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Disclosure,{setting:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:3}})));
  expect(html).toContain('<dt>修订号</dt><dd>第 3 版</dd>');expect(html).toContain('class="key-value facts"');
  const form=html.slice(html.indexOf('<form'));
  const order=['id="ai-mode"','<legend>被直接询问是否 AI 时</legend>','<legend>附加处理</legend>','id="ai-reason"'].map(x=>form.indexOf(x));
  expect(order.every(i=>i>0)).toBe(true);expect([...order].sort((a,b)=>a-b)).toEqual(order);  // one block after another
  const radios=form.slice(form.indexOf('<legend>被直接询问是否 AI 时</legend>'),form.indexOf('</fieldset>'));
  expect(radios).not.toContain('type="checkbox"');  // the checkboxes are not on the radio group's line
  expect(form.match(/type="checkbox"/g)?.length).toBe(2);expect(form).not.toContain('class="choices"');
});
