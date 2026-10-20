import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {contactPreferences,me,personaOverview} from '../src/workbench/api';
import {ConsumerPersona,Personas,documentOf,usableVersions} from '../src/workbench/pages/Personas';

const doc={display_name:'小禾',tone:'亲切、简洁',expression:{max_chars:200,emoji:'few'},ai_disclosure:{deflect_statement:'我是本店的服务助手。'}};
const raw={personas:[{persona_id:'xiaohe',version:1,status:'retired',display_name:'小禾',created_at:'2026-10-19T01:00:00+00:00',document:doc},
  {persona_id:'xiaohe',version:2,status:'published',display_name:'小禾',created_at:'2026-10-19T02:00:00+00:00',document:{...doc,tone:'专业'}}],
  rules:{version:1,default_persona_id:'xiaohe',rules:[{when:{property:'tier',equals:'gold'},persona_id:'xiaohe'}],published_at:'2026-10-19T02:00:00+00:00'},
  ai_disclosure:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:0},consumer:null};
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.operator_persona.publish:1',
  'eios:action:nexloop.operator_persona.assign:1','eios:action:nexloop.operator_persona.rules_publish:1','eios:action:nexloop.ai_disclosure.set:1','eios:action:nexloop.contact_preference.dismiss:1']});
const operator=me({principal_id:'synthetic-operator',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
function client(seed:[unknown[],unknown][]){const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});for(const [k,v] of seed)c.setQueryData(k,v);return c;}
const render=(c:QueryClient,node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},node));

test('the overview parses strictly; a persona resolves to its highest published version', ()=>{
  const view=personaOverview(raw);
  expect([...usableVersions(view.personas).values()].map(p=>p.version)).toEqual([2]);
  for(const bad of [{...raw,personas:[{...raw.personas[0],status:'draft'}]},{...raw,personas:[{...raw.personas[0],document:{tone:'x'}}]},
    {...raw,ai_disclosure:{...raw.ai_disclosure,when_asked:'truthful'}},{...raw,personas:[{...raw.personas[0],persona_id:'Bad'}]}])
    expect(()=>personaOverview(bad)).toThrow();
  expect(()=>contactPreferences({preferences:[{claim_id:'a'.repeat(64),dimension:'price',polarity:'affirmed',value:'x',dismissed:false}]})).toThrow();
});

test('a draft publishes only the fields filled in, as the contract allows', ()=>{
  const draft={persona_id:'lan',display_name:' 小蓝 ',signature:'',self_intro:'',tone:'亲切',values:'先解决问题\n\n不确定就说不确定',max_chars:'150',emoji:'none' as const,
    vocabulary:'',forbidden:'亲爱的',language:'',scenarios:'',not_for:'',statement:'',deflect:'我是服务助手',reason:'首发'};
  expect(documentOf(draft)).toEqual({display_name:'小蓝',tone:'亲切',values:['先解决问题','不确定就说不确定'],
    expression:{max_chars:150,emoji:'none',forbidden_phrases:['亲爱的']},ai_disclosure:{deflect_statement:'我是服务助手'}});
});

test('the owner gets the editor, rules and disclosure forms; another member reads only', ()=>{
  const view=personaOverview(raw);
  const asOwner=render(client([[['workbench','operator-personas'],view],[['workbench','me'],owner]]),h(Personas,{}));
  expect(asOwner).toContain('xiaohe@2');expect(asOwner).toContain('发布为 xiaohe@3');expect(asOwner).toContain('停用');
  const asOperator=render(client([[['workbench','operator-personas'],view],[['workbench','me'],operator]]),h(Personas,{}));
  expect(asOperator).toContain('xiaohe@2');expect(asOperator).not.toContain('发布为');expect(asOperator).not.toContain('<form');
});

test('the consumer block shows persona@version with its source and the customer\'s preferences', ()=>{
  const view=personaOverview({...raw,consumer:{consumer_id:'c'.repeat(64),desired:{persona_id:'xiaohe',source:'rule',rules_version:1},
    settled:{persona_id:'xiaohe',source:'default',rule_version:null,event_id:1,updated_at:'2026-10-19T02:00:00+00:00'},events:[]}});
  const prefs=contactPreferences({preferences:[{claim_id:'a'.repeat(64),dimension:'channel',polarity:'affirmed',value:'wechat',dismissed:false},
    {claim_id:'b'.repeat(64),dimension:'tone',polarity:'affirmed',value:'说简单点',dismissed:true}]});
  const html=render(client([[['workbench','operator-personas','c'.repeat(64)],view],[['workbench','contact-preferences','c'.repeat(64)],prefs],[['workbench','me'],owner]]),
    h(ConsumerPersona,{consumerId:'c'.repeat(64)}));
  expect(html).toContain('xiaohe@2');expect(html).toContain('租户默认');expect(html).toContain('下次对话起');expect(html).toContain('分群规则');
  expect(html).toContain('只用：wechat');expect(html).toContain('不采纳');expect(html).toContain('恢复采纳');
});
