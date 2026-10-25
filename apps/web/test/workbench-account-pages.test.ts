// NX-058 workbench UI, part 3: the public account pages (invitation, reset), the 7-day reset notice and the account security page.
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {acceptInvitation,completeReset,resetNotices} from '../src/workbench/api';
import {AccountSecurity,InvitationPage,ResetNotice,ResetPage,takeFragmentToken} from '../src/workbench/AccountPages';

afterEach(()=>{vi.unstubAllGlobals();});
const token='A'.repeat(43);

test('NX-058 UI: the invitation token comes from the fragment only, in its own shape', ()=>{
  expect(takeFragmentToken('#token='+token)).toBe(token);
  expect(takeFragmentToken('#token=short')).toBeNull();
  expect(takeFragmentToken('#token=<script>'+'a'.repeat(30))).toBeNull();
  expect(takeFragmentToken('')).toBeNull();
});

test('NX-058 UI: the invitation page asks for a password only (no username) and says when the link is incomplete', ()=>{
  const withToken=renderToStaticMarkup(h(InvitationPage,{initialHash:'#token='+token}));
  expect(withToken).toContain('接受邀请并设置口令');expect(withToken).toContain('type="password"');expect(withToken).not.toContain('autoComplete="username"');
  expect(withToken).not.toContain(token);  // the token is never rendered
  expect(renderToStaticMarkup(h(InvitationPage,{initialHash:''}))).toContain('邀请链接不完整或已失效');
});

test('NX-058 UI: the reset page takes the code and a new password, no username', ()=>{
  const html=renderToStaticMarkup(h(ResetPage,{}));
  expect(html).toContain('重置码');expect(html).toContain('设置新口令');expect(html).not.toMatch(/用户名<\/label>|id="reset-username"/);
});

test('NX-058 UI: the reset notice has no close button; the account page lists the 90 days', ()=>{
  const notices=resetNotices([{reset_id:'r-1',issued_by:'synthetic-owner',completed_at:'2026-10-25T01:00:00+00:00'}]);
  const html=renderToStaticMarkup(h(ResetNotice,{notices}));
  expect(html).toContain('7 天内每次登录都会显示');expect(html).not.toContain('<button');
  expect(renderToStaticMarkup(h(ResetNotice,{notices:[]}))).toBe('');
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});c.setQueryData(['workbench','reset-history'],notices);
  const page=renderToStaticMarkup(h(QueryClientProvider,{client:c},h(AccountSecurity,{})) as ReactNode);
  expect(page).toContain('账号安全');expect(page).toContain('你不能删除');
});

test('NX-058 UI: account posts map the server codes; every refusal is the same answer; no referrer is sent', async ()=>{
  const calls:RequestInit[]=[];
  const reply=(status:number,body:unknown)=>vi.fn(async (_url:string,init:RequestInit)=>{calls.push(init);return new Response(JSON.stringify(body),{status});});
  vi.stubGlobal('fetch',reply(200,{code:'invitation_accepted'}));expect(await acceptInvitation(token,'a-good-password-1')).toBe('done');
  vi.stubGlobal('fetch',reply(400,{code:'invitation_invalid'}));expect(await acceptInvitation(token,'a-good-password-1')).toBe('invalid');
  vi.stubGlobal('fetch',reply(429,{code:'rate_limited'}));expect(await completeReset('c'.repeat(22),'a-good-password-1')).toBe('rate_limited');
  vi.stubGlobal('fetch',reply(200,{code:'password_reset'}));expect(await completeReset('c'.repeat(22),'a-good-password-1')).toBe('done');
  vi.stubGlobal('fetch',reply(503,{code:'dependency_unavailable'}));expect(await completeReset('c'.repeat(22),'a-good-password-1')).toBe('unavailable');
  expect(calls.every(c=>c.referrerPolicy==='no-referrer'&&c.credentials==='same-origin')).toBe(true);
  expect(JSON.parse(String(calls[0].body))).toEqual({token,password:'a-good-password-1'});
});
