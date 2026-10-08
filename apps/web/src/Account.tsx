import {useMutation,useQuery} from '@tanstack/react-query';
import {WebChat} from './WebChat';
import {refreshCsrf,logout,errorMessage,readReadiness,type Session} from './api';
export function Account({session,onLogout}:{session:Session;onLogout:()=>void}) {
  const readiness=useQuery({queryKey:['readiness'],queryFn:readReadiness,retry:false});
  const mutation=useMutation({mutationFn:async()=>{const {csrf}=await refreshCsrf();await logout(csrf);},retry:false,onSuccess:onLogout});
  return <section><h1>已登录 NexLoop</h1><p className="subtitle">当前工作空间</p><p className="workspace">{session.tenant_id}</p>
    {session.restricted && <p role="status">此账户的操作受到限制。</p>}
    <p className="note">{readiness.isPending?'正在检查运营服务…':readiness.isError?'运营服务状态暂不可用。':readiness.data?'运营服务已就绪。':'运营服务暂不可用，请稍后重试。'}</p>
    {mutation.isError && <p className="error" role="alert">{errorMessage(mutation.error)}</p>}
    <button onClick={()=>mutation.mutate()} disabled={mutation.isPending}>{mutation.isPending?'正在退出…':'退出登录'}</button><WebChat/></section>;
}
