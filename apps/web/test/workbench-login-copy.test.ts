// UX audit X5/C14 and B12: the workbench login card - the reset hint in 负责人/密码 words with the shared text link, the SSO entry only with a
// configured provider and then as the shared secondary button - and the customer pages: 客户 wording, no internal workspace ID.
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {Account} from '../src/Account';
import {Login} from '../src/Login';
import {SsoButtons} from '../src/workbench/SsoLink';
import {WorkbenchLogin} from '../src/workbench/WorkbenchApp';

const render=(node:ReactNode,data:Record<string,unknown>={})=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  for(const [k,v] of Object.entries(data))c.setQueryData(JSON.parse(k),v);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},node) as ReactNode);};
const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
const OLD_TERMS=/口令|owner|工作账户|工作空间|此账户/;

test('X5/C14: the login card says 忘记密码 and 负责人, its link is the shared text link; no SSO button while SSO is not configured',()=>{
  const card=render(h(WorkbenchLogin,{onLogin:()=>{}}),{'["workbench","sso-providers"]':[]});
  expect(card).toContain('忘记密码？请负责人为你签发重置码');
  expect(card).toMatch(/<a class="text-link" href="\/workbench\/reset">用重置码设置新密码<\/a>/);
  expect(card).not.toMatch(OLD_TERMS);
  expect(card).not.toContain('sso-login');expect(card).not.toContain('SSO');
});

test('X5/C14: with a configured provider the SSO entry is the shared secondary button, styled only with tokens',()=>{
  const card=render(h(WorkbenchLogin,{onLogin:()=>{}}),{'["workbench","sso-providers"]':[{provider_id:'corp',display_name:'Corp IdP'}]});
  expect(card).toMatch(/<a class="button btn-secondary" href="[^"]+corp[^"]*">用 SSO 登录（Corp IdP）<\/a>/);
  expect(renderToStaticMarkup(h(SsoButtons,{providers:[],failed:true}))).toBe('');
  const rules=css.match(/a\.button\.btn-secondary[^{]*\{[^}]*\}|a\.text-link[^{]*\{[^}]*\}/g)??[];
  expect(rules.length).toBeGreaterThanOrEqual(3);
  for(const rule of rules)expect(rule.replace(/var\(--[a-z0-9-]+\)/g,'')).not.toMatch(/#[0-9a-f]{3,6}\b|rgb/i);
});

test('B12: the customer home shows no internal workspace ID; the customer login speaks of a 客户 account',()=>{
  const tenant='00000000-0000-4000-8000-00000000000a';
  const home=render(h(Account,{session:{authenticated:true,tenant_id:tenant,principal_id:'synthetic-customer',restricted:true},onLogout:()=>{}}));
  expect(home).not.toContain(tenant.slice(0,8));expect(home).not.toContain('工作空间');
  expect(home).toContain('此账号的操作受到限制');expect(home).not.toMatch(OLD_TERMS);
  const login=render(h(Login,{onLogin:()=>{}}));
  expect(login).toContain('使用客户账号继续');expect(login).not.toMatch(OLD_TERMS);
});

test('the account pages use 账号 / 密码 / 负责人, not 用户名 or 口令',()=>{
  const source=readFileSync(new URL('../src/workbench/AccountPages.tsx',import.meta.url),'utf8');
  const text=(source.match(/[一-鿿][^<>{}'"`]*/g)??[]).join('\n');
  expect(text).not.toMatch(/用户名|口令|管理员|工作空间/);
  expect(text).toContain('账号以邀请为准');
});
