import {useMutation} from '@tanstack/react-query';
import {WebChat} from './WebChat';
import {refreshCsrf,logout,errorMessage,type Session} from './api';
/** The customer's home: WebChat only. Staff features (review decisions, operations status) live in the separate workbench
 * application (D1: a customer never holds workbench authority). */
export function Account({session,onLogout}:{session:Session;onLogout:()=>void}) {
  const mutation=useMutation({mutationFn:async()=>{const {csrf}=await refreshCsrf();await logout(csrf);},retry:false,onSuccess:onLogout});
  // One compact header row (web layout batch 1): the conversation starts on the first screen.
  return <section><div className="account-head"><div><h1>已登录 NexLoop</h1><p className="workspace">当前工作空间 {session.tenant_id}</p></div>
    <button type="button" onClick={()=>mutation.mutate()} disabled={mutation.isPending}>{mutation.isPending?'正在退出…':'退出登录'}</button></div>
    {session.restricted && <p role="status">此账户的操作受到限制。</p>}
    {mutation.isError && <p className="error" role="alert">{errorMessage(mutation.error)}</p>}
    <WebChat/></section>;
}
