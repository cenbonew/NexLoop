// NX-058 slice F: the workbench SSO entry - the login card's buttons only with a configured provider, the provider list read strictly,
// the start as a plain link, and the members page's link and unlink forms for active operators and reviewers only (owner only).
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {acceptSsoInvitation,me,members,oneTimeSecret,OPERATION_ACTION,readSsoProviders,ssoProviders,ssoStartPath} from '../src/workbench/api';
import {OneTimeSecret} from '../src/workbench/actions';
import {InvitationPage,PasswordAcceptRefusal} from '../src/workbench/AccountPages';
import {SsoButtons,SsoInviteChoiceView,SsoLinkForms,ssoControls} from '../src/workbench/SsoLink';
import {Members} from '../src/workbench/pages/Members';

afterEach(()=>{vi.unstubAllGlobals();});
const html=(node:ReactNode,data:Record<string,unknown>={})=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  for(const [k,v] of Object.entries(data))c.setQueryData(JSON.parse(k),v);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},node) as ReactNode);};
const row=(principal:string,role:string,status:string)=>({principal_id:principal,label:'p-0123abcd',username:principal.replace('synthetic-',''),role,member_status:status,
  account_status:'active',vote_eligible_from:role==='owner'?'2026-10-20T00:00:00+00:00':null,erased:false,person_all_revoked:false,
  person:'s-'+principal.length.toString(16).padStart(8,'0'),human_active:true});
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',...Object.values(OPERATION_ACTION).filter(a=>a.includes('workbench_member'))]});
const corp=[{provider_id:'corp',display_name:'Corp IdP'}];

test('NX-058 F: the provider list is read strictly; anything else, or an unreadable list, means no SSO button', async()=>{
  expect(ssoProviders({providers:corp})).toEqual(corp);
  expect(()=>ssoProviders({providers:[{...corp[0],client_id:'x'}]})).toThrow();
  expect(()=>ssoProviders({providers:[{provider_id:'Corp?x=1',display_name:'x'}]})).toThrow();
  expect(()=>ssoProviders({providers:corp,secret:'x'})).toThrow();
  vi.stubGlobal('fetch',vi.fn(async()=>new Response('{"code":"dependency_unavailable"}',{status:503})));
  expect(await readSsoProviders()).toEqual([]);
  vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({providers:corp}),{status:200})));
  expect(await readSsoProviders()).toEqual(corp);
});

test('NX-058 F: the login card shows SSO buttons only for configured providers, as plain links to the start', ()=>{
  expect(html(h(SsoButtons,{providers:[],failed:false}))).toBe('');
  const shown=html(h(SsoButtons,{providers:corp,failed:false}));
  expect(shown).toContain('用 SSO 登录（Corp IdP）');
  expect(shown).toContain(`href="${ssoStartPath('corp').replace('&','&amp;')}"`);
  expect(ssoStartPath('corp')).toBe('/api/v1/workbench/auth/oidc/start?provider=corp');
  // a refused callback: one message, never the reason
  const failed=html(h(SsoButtons,{providers:corp,failed:true}));
  expect(failed).toContain('role="alert"');expect(failed).toContain('单点登录没有完成');
});

test('NX-058 F: link controls only for active operators and reviewers other than the owner itself', ()=>{
  const m=members({members:[row('synthetic-owner','owner','active'),row('synthetic-second','owner','active'),row('synthetic-op','operator','active'),
    row('synthetic-rv','reviewer','suspended')],invitations:[],resets:[]}).members;
  expect(m.map(r=>ssoControls(r,'synthetic-owner'))).toEqual([false,false,true,false]);
});

test('NX-058 F: the owner gets link and unlink forms; without a provider only unlink; an operator gets none', ()=>{
  const r=members({members:[row('synthetic-op','operator','active')],invitations:[],resets:[]}).members[0];
  const both=html(h(SsoLinkForms,{member:owner,row:r,providers:corp}));
  for(const t of ['绑定单点登录','IdP 用户标识（sub）','SSO 或密码','仅 SSO','解绑单点登录','Corp IdP'])expect(both).toContain(t);
  const none=html(h(SsoLinkForms,{member:owner,row:r,providers:[]}));
  expect(none).not.toContain('aria-label="绑定单点登录"');expect(none).toContain('解绑单点登录');
  const operator=me({principal_id:'synthetic-op2',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
  expect(html(h(SsoLinkForms,{member:operator,row:r,providers:corp}))).not.toContain('<form');
  // On the members page, inside the row's controls.
  const page=html(h(Members,{}),{'["workbench","members"]':members({members:[row('synthetic-owner','owner','active'),row('synthetic-op','operator','active')],invitations:[],resets:[]}),
    '["workbench","me"]':owner,'["workbench","sso-providers"]':corp});
  expect(page).toContain('aria-label="绑定单点登录"');
  expect(OPERATION_ACTION.link_workbench_external).toBe('eios:action:nexloop.workbench_member.link_external:1');
});

test('NX-058 F: the invite form offers SSO only with a configured provider; its link and accept page take no password', ()=>{
  expect(html(h(SsoInviteChoiceView,{providers:[],value:'',onChange:()=>{}}))).toBe('');
  const choice=html(h(SsoInviteChoiceView,{providers:corp,value:'corp',onChange:()=>{}}));
  for(const t of ['登录方式','密码','仅 SSO（Corp IdP）','value="corp"'])expect(choice).toContain(t);
  const token='T'.repeat(43);
  expect(oneTimeSecret({replayed:false,token,sso_provider:'corp'})).toEqual({kind:'token',value:token,sso:true});
  expect(oneTimeSecret({replayed:false,token,sso_provider:null})).toEqual({kind:'token',value:token});
  expect(html(h(OneTimeSecret,{kind:'token',value:token,sso:true}))).toContain('#token='+token+'&amp;sso=1');
  expect(html(h(OneTimeSecret,{kind:'token',value:token}))).not.toContain('sso=1');
  const sso=html(h(InvitationPage,{initialHash:'#token='+token+'&sso=1'}));
  expect(sso).toContain('只能用单点登录');expect(sso).not.toContain('type="password"');
  expect(html(h(InvitationPage,{initialHash:'#token='+token}))).toContain('type="password"');
});

test('NX-058 F: an SSO-only acceptance posts the token alone', async()=>{
  const fetch=vi.fn(async()=>new Response('{"code":"invitation_accepted"}',{status:200}));vi.stubGlobal('fetch',fetch);
  expect(await acceptSsoInvitation('T'.repeat(43))).toBe('done');
  expect(JSON.parse((fetch.mock.calls[0] as unknown as [string,RequestInit])[1].body as string)).toEqual({token:'T'.repeat(43)});
});

test('NX-058 F (L6 S1, S2): a refused password acceptance hints at the full SSO link; the members page marks a member pending SSO', ()=>{
  const refused=html(h(PasswordAcceptRefusal,{result:'invalid'}));
  expect(refused).toContain('需要用单点登录接受');expect(refused).toContain('完整链接');
  expect(html(h(PasswordAcceptRefusal,{result:'rate_limited'}))).not.toContain('单点登录');
  expect(html(h(PasswordAcceptRefusal,{result:null}))).toBe('');
  const parsed=members({members:[{...row('synthetic-op','operator','active'),sso_pending:true},row('synthetic-rv','reviewer','active')],invitations:[],resets:[]}).members;
  expect(parsed.map(m=>m.sso_pending)).toEqual([true,undefined]);
  expect(()=>members({members:[{...row('synthetic-op','operator','active'),sso_pending:'yes'}],invitations:[],resets:[]})).toThrow();
  const page=html(h(Members,{}),{'["workbench","members"]':members({members:[row('synthetic-owner','owner','active'),{...row('synthetic-op','operator','active'),sso_pending:true}],invitations:[],resets:[]}),
    '["workbench","me"]':owner,'["workbench","sso-providers"]':corp});
  expect(page.match(/待绑定 SSO/g)?.length).toBe(1);
});
