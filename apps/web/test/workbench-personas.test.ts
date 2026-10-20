import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {contactPreferences,me,outreachApprovals,outreachControls,outreachPurposes,personaOverview} from '../src/workbench/api';
import {ConsumerPersona,OutreachApprovals,OutreachControlsTab,OutreachPurposes,Personas,documentOf,outreachReady,outreachSettingsOf,usableVersions} from '../src/workbench/pages/Personas';

const doc={display_name:'小禾',tone:'亲切、简洁',expression:{max_chars:200,emoji:'few'},ai_disclosure:{deflect_statement:'我是本店的服务助手。'}};
const raw={personas:[{persona_id:'xiaohe',version:1,status:'retired',display_name:'小禾',created_at:'2026-10-19T01:00:00+00:00',document:doc},
  {persona_id:'xiaohe',version:2,status:'published',display_name:'小禾',created_at:'2026-10-19T02:00:00+00:00',document:{...doc,tone:'专业'}}],
  rules:{version:1,default_persona_id:'xiaohe',rules:[{when:{property:'tier',equals:'gold'},persona_id:'xiaohe'}],published_at:'2026-10-19T02:00:00+00:00'},
  ai_disclosure:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:0},consumer:null};
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.outreach_controls.set:1','eios:action:nexloop.outreach_purposes.set:1','eios:action:nexloop.operator_persona.publish:1',
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


const controls={settings:{time_zone:'Asia/Shanghai',quiet_hours:'21:00-09:00',per_24h:1,per_7d:2,revision:0,source:'default',
  marketing:{enabled:false,consent_mode:'opt_out',unsubscribe_notice:'不想再收到这类消息，回复“退订”即可。',per_7d:1,sender_label:''}},events:[]};

test('0232: the outreach controls tab is the owner\'s only', ()=>{
  const view=personaOverview(raw);
  const asOwner=render(client([[['workbench','operator-personas'],view],[['workbench','me'],owner]]),h(Personas,{}));
  expect(asOwner).toContain('外联控制');
  const asOperator=render(client([[['workbench','operator-personas'],view],[['workbench','me'],operator]]),h(Personas,{}));
  expect(asOperator).not.toContain('外联控制');
});

test('0232: the controls parse strictly and show their revision; the form maps to the Action\'s settings', ()=>{
  const parsed=outreachControls(controls);
  for(const bad of [{...controls,settings:{...controls.settings,quiet_hours:'25:00-08:00'}},{...controls,settings:{...controls.settings,source:'x'}},
    {...controls,settings:{...controls.settings,marketing:{...controls.settings.marketing,consent_mode:'implied'}}}])expect(()=>outreachControls(bad)).toThrow();
  const html=render(client([[['workbench','outreach-controls'],parsed],[['workbench','me'],owner]]),h(OutreachControlsTab,{}));
  expect(html).toContain('未设置（使用默认值）');expect(html).toContain('21:00-09:00');expect(html).toContain('24 小时 1 条');expect(html).toContain('已关闭');
  expect(html).toContain('保存外联控制');expect(html).not.toContain('我确认营销同意模式');  // marketing off: no confirmation asked
  const tenant=outreachControls({...controls,settings:{...controls.settings,revision:3,source:'tenant'},events:[{revision:3,consent_mode_confirmed:'opt_out',marketing_enabled:true,recorded_at:'2026-10-20T06:00:00+00:00'}]});
  expect(render(client([[['workbench','outreach-controls'],tenant],[['workbench','me'],owner]]),h(OutreachControlsTab,{}))).toContain('r3');
  const form={time_zone:'Asia/Shanghai',quiet:false,quiet_from:'21:00',quiet_to:'09:00',per_24h:'1',per_7d:'2',enabled:true,consent:'opt_out' as const,notice:'回复退订即可',marketing_7d:'1',sender:''};
  expect(outreachSettingsOf(form)).toEqual({time_zone:'Asia/Shanghai',quiet_hours:null,per_24h:1,per_7d:2,marketing:{enabled:true,consent_mode:'opt_out',unsubscribe_notice:'回复退订即可',per_7d:1,sender_label:''}});
  // O5: switching marketing on is not submittable until the consent mode is confirmed.
  expect(outreachReady(form,parsed.settings,false,'开通营销')).toBe(false);
  expect(outreachReady(form,parsed.settings,true,'开通营销')).toBe(true);
  expect(outreachReady({...form,enabled:false},parsed.settings,false,'调整')).toBe(true);
  expect(outreachReady({...form,marketing_7d:'3'},parsed.settings,true,'开通营销')).toBe(false);
  expect(outreachReady(form,parsed.settings,true,'')).toBe(false);
});


test('outreach slice 3: the purpose list parses strictly and the owner switches listed purposes', ()=>{
  const def=(id:string,kind:string,trigger:string,topic:string,fc:string,enabled:boolean,unsub:boolean)=>({purpose_id:id,listed:true,enabled,list_version:1,revision:0,
    definition:{purpose_id:id,kind,trigger,topic,frequency_class:fc,requires_consent:false,requires_approval:false,default_enabled:enabled,content_rules:{max_chars:300,unsubscribe_notice_required:unsub}}});
  const raw={list_version:1,purposes:[def('commitment_followup','commitment_followup','commitment_due','commitment_followup','service',true,false),
    def('marketing_general','marketing','owner_request','marketing','marketing',false,true)]};
  const parsed=outreachPurposes(raw);
  expect(parsed.purposes.map(p=>[p.purpose_id,p.enabled])).toEqual([['commitment_followup',true],['marketing_general',false]]);
  expect(()=>outreachPurposes({...raw,purposes:[{...raw.purposes[0],listed:false}]})).toThrow();
  expect(()=>outreachPurposes({...raw,purposes:[{...raw.purposes[0],definition:{...raw.purposes[0].definition,kind:'flash'}}]})).toThrow();
  const html=render(client([[['workbench','outreach-purposes'],parsed],[['workbench','me'],owner]]),h(OutreachPurposes,{}));
  expect(html).toContain('目的清单 v1');expect(html).toContain('承诺跟进');expect(html).toContain('已启用');expect(html).toContain('已停用');
  expect(html).toContain('末尾附退订话术');expect(html).toContain('跟随租户同意模式');expect(html).toContain('停用');expect(html).toContain('启用');
  const asOperator=render(client([[['workbench','outreach-purposes'],parsed],[['workbench','me'],operator]]),h(OutreachPurposes,{}));
  expect(asOperator).not.toContain('<form');
});


test('outreach slice 6: pending approvals parse strictly (ids, purpose, times, requester; never a body) and offer approve and reject', ()=>{
  const id='outreach:'+'a'.repeat(64);
  const raw={window_seconds:86400,pending:[{intent_id:id,purpose_id:'approved_followup',requested_at:'2026-10-20T01:00:00+00:00',expires_at:'2026-10-21T01:00:00+00:00',requested_by:'synthetic-owner'}]};
  const parsed=outreachApprovals(raw);
  expect(parsed.pending.map(p=>p.intent_id)).toEqual([id]);
  expect(()=>outreachApprovals({...raw,pending:[{...raw.pending[0],body:'您好'}]})).toThrow();
  expect(()=>outreachApprovals({...raw,pending:[{...raw.pending[0],intent_id:'x'}]})).toThrow();
  const approver=me({principal_id:'synthetic-owner-2',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.outreach.approve:1']});
  const html=render(client([[['workbench','outreach-approvals'],parsed],[['workbench','me'],approver]]),h(OutreachApprovals,{}));
  expect(html).toContain('待审批外联');expect(html).toContain('审批时限 24 小时');expect(html).toContain('approved_followup');expect(html).toContain('synthetic-owner');
  expect(html).toContain('批准');expect(html).toContain('拒绝');expect(html).toContain('<form');
  const empty=render(client([[['workbench','outreach-approvals'],{window_seconds:86400,pending:[]}],[['workbench','me'],approver]]),h(OutreachApprovals,{}));
  expect(empty).toContain('没有待审批的外联');
  const asOperator=render(client([[['workbench','outreach-approvals'],parsed],[['workbench','me'],operator]]),h(OutreachApprovals,{}));
  expect(asOperator).not.toContain('<form');
});
