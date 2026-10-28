/**
 * NX-058 (docs/design/account-self-service.md v7 §1, §2, §4; V3, I3): the workbench's own account pages.
 * - /workbench/invitation: accept an invitation. The token arrives in the URL fragment (never sent to a server, never in a log); the page
 *   takes it and clears the fragment at once, and sets no-referrer, so the token is not left in history or handed to another page.
 * - /workbench/reset: complete an owner-issued reset with the one-time code and a new password; no username (nothing to enumerate).
 * - ResetNotice: within 7 days of a completed owner-issued reset every sign-in shows it; it cannot be dismissed (it only tells).
 * - AccountSecurity: the member's own resets of the last 90 days; the member cannot remove them.
 * Every refusal reads the same (which part was wrong is never said).
 */
import {useEffect,useState,type FormEvent} from 'react';
import {useQuery} from '@tanstack/react-query';
import {acceptInvitation,completeReset,kindOf,readResetHistory,STATE_TEXT,type AccountFormResult,type ResetNotice as Notice} from './api';
import {Time} from './ui';
import {IdText} from './kit';

export const ACCOUNT_PATHS={invitation:'/workbench/invitation',reset:'/workbench/reset'} as const;
const PASSWORD_HINT='新密码至少 12 个字符，不能与最近用过的密码相同。';
const RESULT_TEXT:Record<Exclude<AccountFormResult,'done'>,string>={
  invalid:'未能完成：链接或重置码无效、已过期或已用过，或者新密码不符合要求。请向负责人重新索取。',
  rate_limited:'尝试太频繁，请稍后再试。',unavailable:'暂时无法完成，请稍后再试；如果多次失败，请联系负责人。'};

/** Take the token from the fragment once and remove it from the address bar and history (design §1, no-referrer). */
export function takeFragmentToken(hash:string):string|null{
  const token=new URLSearchParams(hash.replace(/^#/,'')).get('token');
  return token&&/^[A-Za-z0-9_-]{20,128}$/.test(token)?token:null;
}
function useNoReferrer(){
  useEffect(()=>{const meta=document.createElement('meta');meta.name='referrer';meta.content='no-referrer';document.head.appendChild(meta);
    return ()=>{meta.remove();};},[]);
}

function PasswordFields({password,confirm,setPassword,setConfirm}:{password:string;confirm:string;setPassword:(v:string)=>void;setConfirm:(v:string)=>void}){
  return <>
    <div className="field"><label htmlFor="account-password">新密码</label><input id="account-password" type="password" autoComplete="new-password" minLength={12} maxLength={256}
      value={password} onChange={e=>setPassword(e.target.value)} required/></div>
    <div className="field"><label htmlFor="account-confirm">再输入一次</label><input id="account-confirm" type="password" autoComplete="new-password" minLength={12} maxLength={256}
      value={confirm} onChange={e=>setConfirm(e.target.value)} required/></div>
    <p className="note">{PASSWORD_HINT}</p>
    {confirm&&confirm!==password?<p className="error" role="alert">两次输入的密码不一致。</p>:null}
  </>;
}

export function InvitationPage({initialHash}:{initialHash?:string}){
  useNoReferrer();
  const [token]=useState(()=>takeFragmentToken(initialHash??globalThis.location?.hash??''));
  useEffect(()=>{if(globalThis.location?.hash)window.history.replaceState(null,'',ACCOUNT_PATHS.invitation);},[]);
  const [password,setPassword]=useState('');const [confirm,setConfirm]=useState('');
  const [result,setResult]=useState<AccountFormResult|null>(null);const [busy,setBusy]=useState(false);
  async function submit(event:FormEvent){event.preventDefault();if(!token||password.length<12||password!==confirm)return;
    setBusy(true);setResult(await acceptInvitation(token,password));setBusy(false);}
  return <section className="workbench-page" aria-labelledby="invitation-title"><h1 id="invitation-title">接受工作台邀请</h1>
    {result==='done'?<><p role="status">已接受邀请，账号已开通。请用邀请里的用户名和刚设置的密码登录。</p><a href="/workbench">去登录</a></>
      :!token?<p role="alert">邀请链接不完整或已失效。请向邀请你的负责人重新索取（链接只能用一次）。</p>
      :<form onSubmit={submit} aria-label="接受邀请">
        <p className="note">设置密码后即开通账号；用户名以邀请为准。这个链接只能用一次。</p>
        <PasswordFields password={password} confirm={confirm} setPassword={setPassword} setConfirm={setConfirm}/>
        <button type="submit" disabled={busy||password.length<12||password!==confirm}>接受邀请并设置密码</button>
        {result?<p className={result==='unavailable'?'pending':'error'} role="alert">{RESULT_TEXT[result]}</p>:null}
      </form>}</section>;
}

export function ResetPage(){
  useNoReferrer();
  const [code,setCode]=useState('');const [password,setPassword]=useState('');const [confirm,setConfirm]=useState('');
  const [result,setResult]=useState<AccountFormResult|null>(null);const [busy,setBusy]=useState(false);
  const cleaned=code.trim();
  async function submit(event:FormEvent){event.preventDefault();if(!cleaned||password.length<12||password!==confirm)return;
    setBusy(true);setResult(await completeReset(cleaned,password));setBusy(false);}
  return <section className="workbench-page" aria-labelledby="reset-title"><h1 id="reset-title">用重置码设置新密码</h1>
    {result==='done'?<><p role="status">密码已重置，之前的登录都已失效。请用新密码登录。</p><a href="/workbench">去登录</a></>
      :<form onSubmit={submit} aria-label="重置密码">
        <p className="note">重置码由负责人线下交给你，30 分钟内有效，只能用一次。这里不需要用户名。</p>
        <div className="field"><label htmlFor="reset-code">重置码</label><input id="reset-code" autoComplete="one-time-code" spellCheck={false} maxLength={64}
          value={code} onChange={e=>setCode(e.target.value)} required/></div>
        <PasswordFields password={password} confirm={confirm} setPassword={setPassword} setConfirm={setConfirm}/>
        <button type="submit" disabled={busy||!cleaned||password.length<12||password!==confirm}>设置新密码</button>
        {result?<p className={result==='unavailable'?'pending':'error'} role="alert">{RESULT_TEXT[result]}</p>:null}
      </form>}</section>;
}

/** V3: shown on every page while a completed owner-issued reset is within 7 days; no close button. */
export function ResetNotice({notices}:{notices:Notice[]}){
  if(!notices.length)return null;
  const last=notices[0];
  return <p className="impact-summary" role="status" aria-live="polite">你的密码在 <Time value={last.completed_at}/> 通过负责人签发的重置码重置（签发人 <IdText value={last.issued_by} head={8} tail={4} copy={false}/>）。
    如果不是你本人操作，请立即联系其他负责人。这条提示 7 天内每次登录都会显示，详情见“账号安全”。</p>;
}

export function AccountSecurity(){
  const query=useQuery({queryKey:['workbench','reset-history'],queryFn:readResetHistory});
  return <section className="workbench-page" aria-labelledby="account-title"><h1 id="account-title">账号安全</h1>
    <p className="note">最近 90 天通过负责人签发的重置码完成的密码重置。这里的记录你不能删除。</p>
    {query.isPending?<p role="status">{STATE_TEXT.loading}</p>:query.isError?<p role="alert">{STATE_TEXT[kindOf(query.error)]}</p>
      :query.data.length===0?<p>最近 90 天没有通过重置码重置密码。</p>
      :<ul>{query.data.map(n=><li key={n.reset_id}><Time value={n.completed_at}/>，签发人 <IdText value={n.issued_by} head={8} tail={4} copy={false}/></li>)}</ul>}
  </section>;
}
