import {useEffect,useState,type FormEvent,type ReactNode} from 'react';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,readWorkbenchSession,workbenchLogin,workbenchLogout} from './api';
import {PAGES,parseRoute,routePath,type PageKey,type Route} from './route';
import {Overview} from './pages/Overview';
import {Goals,Plans} from './pages/Goals';
import {ConsumerDetail,Consumers,Conversation} from './pages/Consumers';
import {Actions} from './pages/Actions';
import {CommitmentDetail,Commitments} from './pages/Commitments';
import {Contact} from './pages/Contact';
import {NotEnabled,Settings} from './pages/Settings';
import {Knowledge,ReviewEvidencePage} from './pages/Knowledge';

/** Slice 2/3 attach governed Action controls per page here (ReactNode slots); slice 1 renders none. */
export type ActionSlots=Partial<Record<PageKey,ReactNode>>;

function WorkbenchLogin({onLogin}:{onLogin:()=>void}){
  const [username,setUsername]=useState('');const [password,setPassword]=useState('');
  const mutation=useMutation({mutationFn:()=>workbenchLogin(username,password),onSuccess:()=>{setPassword('');onLogin();}});
  function submit(event:FormEvent){event.preventDefault();mutation.mutate();}
  return <section aria-labelledby="workbench-login-title"><h1 id="workbench-login-title">登录负责人工作台</h1><p className="subtitle">企业成员账号；顾客账号不能进入工作台</p>
    <form onSubmit={submit}><div className="field"><label htmlFor="wb-username">账号</label><input id="wb-username" autoComplete="username" value={username} onChange={e=>setUsername(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    <div className="field"><label htmlFor="wb-password">密码</label><input id="wb-password" type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    {mutation.isError?<p className="error" role="alert">{kindOf(mutation.error)==='unauthenticated'?'账号或密码不正确。':STATE_TEXT[kindOf(mutation.error)]}</p>:null}
    <button type="submit" disabled={mutation.isPending}>{mutation.isPending?'正在登录…':'登录'}</button></form></section>;
}

export function WorkbenchPage({route,go,slots={}}:{route:Route;go:(route:Route)=>void;slots?:ActionSlots}){
  const a=slots[route.page];
  switch(route.page){
    case 'overview':return <Overview actions={a} onOpen={page=>go({page:page as PageKey})}/>;
    case 'goals':return <Goals actions={a}/>;
    case 'consumers':
      if(route.sub==='conversation'&&route.id)return <Conversation conversationId={route.id} actions={a}/>;
      return route.id?<ConsumerDetail consumerId={route.id} actions={a} onConversation={id=>go({page:'consumers',id,sub:'conversation'})}/>:<Consumers actions={a} onOpen={id=>go({page:'consumers',id})}/>;
    case 'plans':return <Plans actions={a}/>;
    case 'actions':return <Actions actions={a}/>;
    case 'commitments':return route.id?<CommitmentDetail commitmentId={route.id} actions={a}/>:<Commitments actions={a} onOpen={id=>go({page:'commitments',id})}/>;
    case 'contact':return <Contact actions={a}/>;
    case 'settings':return <Settings actions={a}/>;
    case 'knowledge':return route.id?<ReviewEvidencePage candidateId={route.id} actions={a}/>:<Knowledge actions={a} onOpen={id=>go({page:'knowledge',id})}/>;
    case 'ontology':return <NotEnabled title={PAGES.ontology}/>;
    case 'experiments':return <NotEnabled title={PAGES.experiments}/>;
  }
}

export function WorkbenchApp({slots}:{slots?:ActionSlots}){
  const cache=useQueryClient();
  const session=useQuery({queryKey:['workbench','session'],queryFn:readWorkbenchSession,refetchOnWindowFocus:false});
  const [route,setRoute]=useState<Route>(()=>parseRoute(window.location.pathname));
  useEffect(()=>{const back=()=>setRoute(parseRoute(window.location.pathname));window.addEventListener('popstate',back);return ()=>window.removeEventListener('popstate',back);},[]);
  function go(next:Route){window.history.pushState(null,'',routePath(next));setRoute(next);}
  const logout=useMutation({mutationFn:workbenchLogout,onSettled:()=>{cache.removeQueries({queryKey:['workbench']});void session.refetch();}});
  return <div className="shell workbench"><header>NexLoop 负责人工作台</header><main>
    {session.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :session.isError?<section><h1>暂时无法连接</h1><p role="alert">{STATE_TEXT[kindOf(session.error)]}</p><button type="button" onClick={()=>void session.refetch()}>重试</button></section>
      :!session.data?<WorkbenchLogin onLogin={()=>void session.refetch()}/>
      :<div className="workbench-layout">
        <nav aria-label="工作台导航"><ul>{(Object.keys(PAGES) as PageKey[]).map(page=><li key={page}>
          <a href={routePath({page})} aria-current={route.page===page?'page':undefined} onClick={event=>{event.preventDefault();go({page});}}>{PAGES[page]}</a></li>)}</ul>
          <button type="button" onClick={()=>logout.mutate()} disabled={logout.isPending}>退出</button></nav>
        <WorkbenchPage route={route} go={go} slots={slots}/>
      </div>}
  </main><footer>NexLoop · 持续运营</footer></div>;
}
