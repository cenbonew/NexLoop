import {Brand} from '../Brand';
import {useEffect,useState,type FormEvent} from 'react';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,readWorkbenchSession,workbenchLogin,workbenchLogout} from './api';
import {PAGES,WORLD_SECTIONS,parseRoute,routePath,type PageKey,type Route} from './route';
import {Overview} from './pages/Overview';
import {Personas} from './pages/Personas';
import {Goals,Plans} from './pages/Goals';
import {ConsumerDetail,Consumers,Conversation} from './pages/Consumers';
import {Actions} from './pages/Actions';
import {CommitmentDetail,Commitments} from './pages/Commitments';
import {Contact} from './pages/Contact';
import {Settings} from './pages/Settings';
import {Experiments} from './pages/Experiments';
import {Knowledge,ReviewEvidencePage} from './pages/Knowledge';
import {Alerts,Audit,Operations} from './pages/Observability';
import {WorldModel} from './pages/WorldModel';
import {Capabilities} from './pages/Capabilities';
import {DataAssets} from './pages/DataAssets';
import {DataHealth} from './pages/DataHealth';
import {Approvals} from './pages/Approvals';
import {Members} from './pages/Members';
import {ACCOUNT_PATHS,AccountSecurity,InvitationPage,ResetNotice,ResetPage} from './AccountPages';
import {GOVERNED_SLOTS,type Slots} from './actions';
import {PageErrorBoundary} from './kit';

/** Governed Action controls per page (slices 2/3, ./actions); a slot may depend on the route (the object shown). */
export type ActionSlots=Slots;

function WorkbenchLogin({onLogin}:{onLogin:()=>void}){
  const [username,setUsername]=useState('');const [password,setPassword]=useState('');
  const mutation=useMutation({mutationFn:()=>workbenchLogin(username,password),onSuccess:()=>{setPassword('');onLogin();}});
  function submit(event:FormEvent){event.preventDefault();mutation.mutate();}
  return <section className="workbench-login" aria-labelledby="workbench-login-title"><h1 id="workbench-login-title">登录负责人工作台</h1><p className="subtitle">企业成员账号；顾客账号不能进入工作台</p>
    <form onSubmit={submit}><div className="field"><label htmlFor="wb-username">账号</label><input id="wb-username" autoComplete="username" value={username} onChange={e=>setUsername(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    <div className="field"><label htmlFor="wb-password">密码</label><input id="wb-password" type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    {mutation.isError?<p className="error" role="alert">{kindOf(mutation.error)==='unauthenticated'?'账号或密码不正确。':STATE_TEXT[kindOf(mutation.error)]}</p>:null}
    <button type="submit" disabled={mutation.isPending}>{mutation.isPending?'正在登录…':'登录'}</button></form>
    <p className="note">忘记口令？请 owner 为你签发重置码，然后<a href="/workbench/reset">用重置码设置新口令</a>。</p></section>;
}

/** What the shown object belongs to (F10): a detail page names its customer so the breadcrumb can lead back to that customer. */
export type Context={consumerId:string;label:string}|null;

export function WorkbenchPage({route,go,slots={},onContext=()=>{}}:{route:Route;go:(route:Route,options?:{replace?:boolean})=>void;slots?:ActionSlots;onContext?:(context:Context)=>void}){
  const slot=slots[route.page];const a=typeof slot==='function'?slot(route):slot;
  const setQuery=(query:Route['query'])=>go({page:route.page,query},{replace:true});
  switch(route.page){
    case 'overview':return <Overview actions={a} onOpen={page=>go({page:page as PageKey})}/>;
    case 'goals':return <Goals actions={a}/>;
    case 'consumers':
      if(route.sub==='conversation'&&route.id)return <Conversation conversationId={route.id} actions={a} onContext={onContext}/>;
      return route.id?<ConsumerDetail consumerId={route.id} actions={a} onConversation={id=>go({page:'consumers',id,sub:'conversation'})}
          onCommitments={consumer=>go({page:'commitments',query:{consumer,status:'open'}})} onContext={onContext}/>
        :<Consumers actions={a} query={route.query??{}} onQuery={setQuery} onOpen={id=>go({page:'consumers',id})}
          onCommitments={consumer=>go({page:'commitments',query:{consumer,status:'open'}})}/>;
    case 'plans':return <Plans actions={a}/>;
    case 'actions':return <Actions actions={a}/>;
    case 'commitments':return route.id?<CommitmentDetail commitmentId={route.id} actions={a} onContext={onContext} onConsumer={id=>go({page:'consumers',id})}/>
      :<Commitments actions={a} query={route.query??{}} onQuery={setQuery} onOpen={id=>go({page:'commitments',id})} onConsumer={id=>go({page:'consumers',id})}/>;
    case 'contact':return <Contact actions={a}/>;
    case 'approvals':return <Approvals/>;
    case 'members':return <Members/>;
    case 'account':return <AccountSecurity/>;
    case 'personas':return <Personas actions={a}/>;
    case 'settings':return <Settings actions={a}/>;
    case 'knowledge':return route.id?<ReviewEvidencePage candidateId={route.id} actions={a}/>:<Knowledge actions={a} onOpen={id=>go({page:'knowledge',id})}/>;
    case 'alerts':return <Alerts actions={a}/>;
    case 'operations':return <Operations actions={a}/>;
    case 'audit':return <Audit actions={a}/>;
    // Pushed: Back walks the sections, the Object Type view and focus, and the Object pages; the Table / Graph switch replaces.
    case 'ontology':return <WorldModel section={route.section} query={route.query}
      go={(section,query,options)=>go({page:'ontology',...(section?{section}:{}),...(query&&Object.keys(query).length?{query}:{})},options)}/>;
    case 'data_assets':return <DataAssets/>;
    case 'data_health':return <DataHealth/>;
    case 'capabilities':return <Capabilities query={route.query} onQuery={setQuery}/>;
    case 'experiments':return <Experiments title={PAGES.experiments}/>;
  }
}

/** Navigation in four groups (walkthrough F08; world model design §5: 模型与资产 next to 运营); the next-stage page last, marked "未启用".
 * 企业世界模型 is an expandable entry (EIOS navigation, owner review 2026-10-20): its summary page and one page per concept. */
export const NAV_GROUPS:{title:string;pages:PageKey[]}[]=[
  {title:'运营',pages:['overview','consumers','commitments','actions','approvals','plans','goals','contact']},
  {title:'模型与资产',pages:['ontology','data_assets','data_health','knowledge']},
  {title:'监控',pages:['alerts','operations','audit']},
  {title:'管理',pages:['settings','members','account','personas','capabilities','experiments']}];
const NOT_ENABLED:PageKey[]=['experiments'];
/** Pages with sub-entries in the sidebar: the world model's concept pages (WORLD_SECTIONS, in EIOS order). */
export const NAV_CHILDREN:Partial<Record<PageKey,{label:string;route:Route}[]>>={
  ontology:WORLD_SECTIONS.map(s=>({label:s.label,route:{page:'ontology',section:s.key}}))};
const sameRoute=(a:Route,b:Route)=>a.page===b.page&&a.section===b.section;

/** Detail pages: where this page sits (F08); every step but the last goes back. With the customer known (F10) a conversation reads
 * 消费者 › <客户> › 会话 and a commitment 承诺 / 交付 › <客户的承诺> › 承诺详情. */
export function crumbs(route:Route,context:Context=null):{label:string;to?:Route}[]{
  if(route.page==='ontology'&&route.section)return [{label:PAGES.ontology,to:{page:'ontology'}},{label:WORLD_SECTIONS.find(s=>s.key===route.section)!.label}];
  if(!route.id)return [];
  const list={page:route.page} as Route;
  if(route.page==='consumers'&&route.sub==='conversation')return [{label:PAGES.consumers,to:list},
    ...(context?[{label:context.label,to:{page:'consumers',id:context.consumerId} as Route}]:[]),{label:'会话'}];
  if(route.page==='commitments'&&context)return [{label:PAGES.commitments,to:list},
    {label:context.label+' 的承诺',to:{page:'commitments',query:{consumer:context.consumerId,status:'all'}}},{label:'承诺详情'}];
  const leaf={consumers:'客户详情',commitments:'承诺详情',knowledge:'审核证据'}[route.page as string]??'详情';
  return [{label:PAGES[route.page],to:list},{label:leaf}];
}

/** F09: the scroll position of each history entry, restored on Back once the page has rendered (data may still be loading). */
function restoreScroll(y:number){
  const until=Date.now()+3000;
  const step=()=>{if(document.documentElement.scrollHeight-innerHeight>=y||Date.now()>until){window.scrollTo(0,y);return;}requestAnimationFrame(step);};
  requestAnimationFrame(step);
}
const here=()=>parseRoute(window.location.pathname,window.location.search);

export function WorkbenchApp({slots=GOVERNED_SLOTS}:{slots?:ActionSlots}){
  const cache=useQueryClient();
  const session=useQuery({queryKey:['workbench','session'],queryFn:readWorkbenchSession,refetchOnWindowFocus:false});
  const [route,setRoute]=useState<Route>(here);
  const [context,setContext]=useState<Context>(null);
  const [menu,setMenu]=useState(false);
  const [opened,setOpened]=useState<Partial<Record<PageKey,boolean>>>({});  // a sub-entry list the member opened or closed
  useEffect(()=>{const back=(event:PopStateEvent)=>{setContext(null);setRoute(here());restoreScroll(Number(event.state?.scroll)||0);};
    window.addEventListener('popstate',back);return ()=>window.removeEventListener('popstate',back);},[]);
  function go(next:Route,options:{replace?:boolean}={}){
    if(options.replace){window.history.replaceState({...window.history.state,scroll:window.scrollY},'',routePath(next));setRoute(next);return;}
    window.history.replaceState({...window.history.state,scroll:window.scrollY},'');  // where to come back to
    window.history.pushState({scroll:0},'',routePath(next));setContext(null);setRoute(next);window.scrollTo(0,0);
  }
  const logout=useMutation({mutationFn:workbenchLogout,onSettled:()=>{cache.removeQueries({queryKey:['workbench']});void session.refetch();}});
  return <div className="shell workbench"><header className="brand-header"><Brand/><span className="brand-context">负责人工作台</span><span className="brand-tz">时间均为北京时间</span></header><main>
    {/* NX-058: the public account pages need no session (the invitation token or reset code is the credential). */}
    {window.location.pathname===ACCOUNT_PATHS.invitation?<InvitationPage/>:window.location.pathname===ACCOUNT_PATHS.reset?<ResetPage/>
      :session.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :session.isError?<section><h1>暂时无法连接</h1><p role="alert">{STATE_TEXT[kindOf(session.error)]}</p><button type="button" onClick={()=>void session.refetch()}>重试</button></section>
      :!session.data?<WorkbenchLogin onLogin={()=>void session.refetch()}/>
      :<div className="workbench-layout">
        {/* md/sm (<1024): the navigation sits behind this visible menu button instead of a horizontal strip (web layout batch 1). */}
        <button type="button" className="workbench-menu-toggle" aria-expanded={menu} aria-controls="workbench-nav" onClick={()=>setMenu(!menu)}>
          <span>{menu?'收起菜单':'菜单'}</span><span className="menu-current">{PAGES[route.page]}{route.section?' · '+WORLD_SECTIONS.find(s=>s.key===route.section)!.label:''}</span></button>
        <nav id="workbench-nav" className={menu?'open':undefined} aria-label="工作台导航">{NAV_GROUPS.map(g=><div key={g.title} className="nav-group">
          <h2 className="nav-group-title" id={'nav-'+g.title}>{g.title}</h2><ul aria-labelledby={'nav-'+g.title}>{g.pages.map(page=>{
          const children=NAV_CHILDREN[page];const open=children?(opened[page]??route.page===page):false;
          const link=<a href={routePath({page})} aria-current={route.page===page&&!route.section?'page':undefined} className={NOT_ENABLED.includes(page)?'nav-off':undefined}
            onClick={event=>{event.preventDefault();setMenu(false);go({page});}}>{PAGES[page]}{NOT_ENABLED.includes(page)?<span className="nav-badge">未启用</span>:null}</a>;
          return <li key={page}>{children?<><div className="nav-entry">{link}
            <button type="button" className="nav-expand" aria-expanded={open} aria-controls={'nav-sub-'+page} aria-label={(open?'收起':'展开')+PAGES[page]}
              onClick={()=>setOpened({...opened,[page]:!open})}>{open?'▾':'▸'}</button></div>
            {open?<ul id={'nav-sub-'+page} className="nav-sub">{children.map(c=><li key={c.label}><a href={routePath(c.route)}
              aria-current={sameRoute(route,c.route)?'page':undefined} onClick={event=>{event.preventDefault();setMenu(false);go(c.route);}}>{c.label}</a></li>)}</ul>:null}</>
            :link}</li>;})}</ul></div>)}
          <button type="button" onClick={()=>logout.mutate()} disabled={logout.isPending}>退出</button></nav>
        <div className="workbench-main">
          <ResetNotice notices={session.data.reset_notices}/>
          {crumbs(route,context).length?<nav className="breadcrumbs" aria-label="面包屑"><ol>{crumbs(route,context).map((c,i)=><li key={i}>{c.to?<a href={routePath(c.to)}
            onClick={event=>{event.preventDefault();go(c.to!);}}>{c.label}</a>:<span aria-current="page">{c.label}</span>}</li>)}</ol></nav>:null}
          <PageErrorBoundary key={routePath(route)}><WorkbenchPage route={route} go={go} slots={slots} onContext={setContext}/></PageErrorBoundary></div>
      </div>}
  </main><footer>NexLoop · 持续运营</footer></div>;
}
