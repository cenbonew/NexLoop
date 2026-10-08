import {useState,type FormEvent} from 'react';
import {useMutation} from '@tanstack/react-query';
import {login,errorMessage,type Session} from './api';
export function Login({onLogin}:{onLogin:(session:Session)=>void}) {
  const [username,setUsername]=useState('');const [password,setPassword]=useState('');
  const mutation=useMutation({mutationFn:()=>login(username,password),retry:false,onSuccess:({session})=>{setPassword('');onLogin(session);}});
  function submit(event:FormEvent){event.preventDefault();mutation.mutate();}
  return <section aria-labelledby="login-title"><h1 id="login-title">登录 NexLoop</h1><p className="subtitle">使用工作账户继续</p>
    <form onSubmit={submit}><div className="field"><label htmlFor="username">账号</label><input id="username" name="username" autoComplete="username" placeholder="输入账号" value={username} onChange={e=>setUsername(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    <div className="field"><label htmlFor="password">密码</label><input id="password" name="password" type="password" autoComplete="current-password" placeholder="输入密码" value={password} onChange={e=>setPassword(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    {mutation.isError && <p className="error" role="alert">{errorMessage(mutation.error)}</p>}
    <button disabled={mutation.isPending} type="submit">{mutation.isPending?'正在登录…':'登录'}</button><p className="note">会话将在本设备保持，退出后立即失效。</p></form></section>;
}
