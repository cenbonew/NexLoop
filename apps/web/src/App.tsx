import {useQuery,useQueryClient} from '@tanstack/react-query';
import {readSession,errorMessage} from './api';
import {Login} from './Login';import {Account} from './Account';
export function App(){
  const cache=useQueryClient();const query=useQuery({queryKey:['session'],queryFn:readSession,retry:false,refetchOnWindowFocus:false});
  return <div className="shell"><header>NexLoop</header><main>
    {query.isPending?<p role="status">正在检查会话…</p>:query.isError?<section><h1>暂时无法连接</h1><p role="alert">{errorMessage(query.error)}</p><button onClick={()=>void query.refetch()}>重试</button></section>:query.data?<Account session={query.data} onLogout={()=>cache.setQueryData(['session'],null)}/>:<Login onLogin={session=>cache.setQueryData(['session'],session)}/>}
  </main><footer>NexLoop · 持续运营</footer></div>;
}
