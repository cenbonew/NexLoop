import {Brand} from '../Brand';
import {useEffect,useRef,useState,type FormEvent,type KeyboardEvent} from 'react';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,offers,readMe,readWorkbenchSession,workbenchLogin,workbenchLogout} from './api';
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
import {Glossary} from './pages/Glossary';
import {Capabilities} from './pages/Capabilities';
import {DataAssets} from './pages/DataAssets';
import {DataHealth} from './pages/DataHealth';
import {Approvals} from './pages/Approvals';
import {Members} from './pages/Members';
import {ACCOUNT_PATHS,AccountSecurity,InvitationPage,ResetNotice,ResetPage} from './AccountPages';
import {GOVERNED_SLOTS,type Slots} from './actions';
import {PageErrorBoundary} from './kit';
import {WorkbenchSso} from './SsoLink';
import {SectionTerm,sectionText,useSections,type Sections} from './Gloss';

/** Governed Action controls per page (slices 2/3, ./actions); a slot may depend on the route (the object shown). */
export type ActionSlots=Slots;

export function WorkbenchLogin({onLogin}:{onLogin:()=>void}){
  const [username,setUsername]=useState('');const [password,setPassword]=useState('');
  const mutation=useMutation({mutationFn:()=>workbenchLogin(username,password),onSuccess:()=>{setPassword('');onLogin();}});
  function submit(event:FormEvent){event.preventDefault();mutation.mutate();}
  return <section className="workbench-login" aria-labelledby="workbench-login-title"><h1 id="workbench-login-title">登录负责人工作台</h1><p className="subtitle">企业成员账号；客户账号不能进入工作台</p>
    <form onSubmit={submit}><div className="field"><label htmlFor="wb-username">账号</label><input id="wb-username" autoComplete="username" value={username} onChange={e=>setUsername(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    <div className="field"><label htmlFor="wb-password">密码</label><input id="wb-password" type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required maxLength={1024} disabled={mutation.isPending}/></div>
    {mutation.isError?<p className="error" role="alert">{kindOf(mutation.error)==='unauthenticated'?'账号或密码不正确。':STATE_TEXT[kindOf(mutation.error)]}</p>:null}
    <button type="submit" disabled={mutation.isPending}>{mutation.isPending?'正在登录…':'登录'}</button></form>
    {/* NX-058 slice F: shown only when a provider is configured. */}
    <WorkbenchSso/>
    <p className="note login-help">忘记密码？请负责人为你签发重置码，然后<a className="text-link" href="/workbench/reset">用重置码设置新密码</a>。</p></section>;
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
    case 'personas':return <Personas actions={a} onConsumers={()=>go({page:'consumers'})}/>;
    case 'settings':return <Settings actions={a} onMembers={()=>go({page:'members'})}/>;
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
    case 'glossary':return <Glossary/>;
    case 'experiments':return <Experiments title={PAGES.experiments}/>;
  }
}

/** Navigation in four groups (walkthrough F08; world model design §5: 模型与资产 next to 运营); the next-stage page last, marked "未启用".
 * 企业世界模型 is an expandable entry (EIOS navigation, owner review 2026-10-20): its summary page and one page per concept. */
export const NAV_GROUPS:{title:string;pages:PageKey[]}[]=[
  {title:'运营',pages:['overview','consumers','commitments','actions','approvals','plans','goals','contact']},
  {title:'模型与资产',pages:['ontology','data_assets','data_health','knowledge']},
  {title:'监控',pages:['alerts','operations','audit']},
  {title:'管理',pages:['settings','members','account','personas','capabilities','glossary','experiments']}];
/** Pages offered only to a member whose role carries the operation (R57 W5: the glossary page, the owner's). */
const NAV_NEEDS:Partial<Record<PageKey,Parameters<typeof offers>[1]>>={glossary:'world_glossary_set'};
export const navOffered=(page:PageKey,member:Parameters<typeof offers>[0])=>{const need=NAV_NEEDS[page];return !need||offers(member,need);};
const NOT_ENABLED:PageKey[]=[];
/** Pages with sub-entries in the sidebar: the world model's concept pages (WORLD_SECTIONS, in EIOS order). */
export const NAV_CHILDREN:Partial<Record<PageKey,{label:string;route:Route}[]>>={
  ontology:WORLD_SECTIONS.map(s=>({label:s.label,route:{page:'ontology',section:s.key}}))};
const sameRoute=(a:Route,b:Route)=>a.page===b.page&&a.section===b.section;

/** Detail pages: where this page sits (F08); every step but the last goes back. With the customer known (F10) a conversation reads
 * 消费者 › <客户> › 会话 and a commitment 承诺 / 交付 › <客户的承诺> › 承诺详情. */
export function crumbs(route:Route,context:Context=null,sections?:Sections):{label:string;to?:Route}[]{
  if(route.page==='ontology'&&route.section)return [{label:PAGES.ontology,to:{page:'ontology'}},{label:sectionText(WORLD_SECTIONS.find(s=>s.key===route.section)!.label,sections)}];
  if(!route.id)return [];
  const list={page:route.page} as Route;
  if(route.page==='consumers'&&route.sub==='conversation')return [{label:PAGES.consumers,to:list},
    ...(context?[{label:context.label,to:{page:'consumers',id:context.consumerId} as Route}]:[]),{label:'会话'}];
  if(route.page==='commitments'&&context)return [{label:PAGES.commitments,to:list},
    {label:context.label+' 的承诺',to:{page:'commitments',query:{consumer:context.consumerId,status:'all'}}},{label:'承诺详情'}];
  const leaf={consumers:'客户详情',commitments:'承诺详情',knowledge:'审核证据'}[route.page as string]??'详情';
  return [{label:PAGES[route.page],to:list},{label:leaf}];
}

/** The browser tab's title for a route (UX audit B7, WCAG 2.4.2): the page, the detail it shows, then the application. */
export function pageTitle(route:Route|null,context:Context=null,sections?:Sections):string{
  const app='负责人工作台 · NexLoop';
  if(!route)return '登录 · '+app;
  const trail=crumbs(route,context,sections);
  const page=trail.length?trail.slice(1).map(c=>c.label).reverse().concat(PAGES[route.page]).join(' · '):PAGES[route.page];
  return page+' · '+app;
}
/** After an in-app navigation the reader is moved to the new page's title (WCAG 2.4.3): a screen reader announces it and the next
 * Tab starts in the content, not back in the sidebar. Waits a few frames for the page to mount its heading. */
function focusPageTitle(){
  let tries=0;
  const step=()=>{const h=document.querySelector<HTMLElement>('.workbench-main h1');
    if(h){if(!h.hasAttribute('tabindex'))h.setAttribute('tabindex','-1');h.focus({preventScroll:true});return;}
    if(++tries<30)requestAnimationFrame(step);};
  requestAnimationFrame(step);
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
  const toggle=useRef<HTMLButtonElement>(null);
  useEffect(()=>{const back=(event:PopStateEvent)=>{setContext(null);setRoute(here());restoreScroll(Number(event.state?.scroll)||0);focusPageTitle();};
    window.addEventListener('popstate',back);return ()=>window.removeEventListener('popstate',back);},[]);
  const signedIn=!!session.data;
  const sections=useSections();
  const me=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false,enabled:signedIn});
  const shown=(page:PageKey)=>navOffered(page,me.data);  // the server's section names once the world model is read (S4)
  useEffect(()=>{const path=window.location.pathname;
    document.title=path===ACCOUNT_PATHS.invitation?'接受工作台邀请 · 负责人工作台 · NexLoop':path===ACCOUNT_PATHS.reset?'用重置码设置新密码 · 负责人工作台 · NexLoop'
      :pageTitle(signedIn?route:null,context,sections);},[route,context,signedIn,sections]);
  // The narrow-screen menu (UX audit C9): opening it moves focus to its first link, Escape closes it and returns to the button.
  useEffect(()=>{if(menu)document.querySelector<HTMLElement>('#workbench-nav a')?.focus();},[menu]);
  function navKey(event:KeyboardEvent){if(event.key==='Escape'&&menu){event.preventDefault();setMenu(false);toggle.current?.focus();}}
  function go(next:Route,options:{replace?:boolean}={}){
    if(options.replace){window.history.replaceState({...window.history.state,scroll:window.scrollY},'',routePath(next));setRoute(next);return;}
    window.history.replaceState({...window.history.state,scroll:window.scrollY},'');  // where to come back to
    window.history.pushState({scroll:0},'',routePath(next));setContext(null);setRoute(next);window.scrollTo(0,0);focusPageTitle();
  }
  const logout=useMutation({mutationFn:workbenchLogout,onSettled:()=>{cache.removeQueries({queryKey:['workbench']});void session.refetch();}});
  return <div className="shell workbench"><a className="skip-link" href="#workbench-content"
    onClick={event=>{event.preventDefault();const h=document.querySelector<HTMLElement>('.workbench-main h1');
      if(h){if(!h.hasAttribute('tabindex'))h.setAttribute('tabindex','-1');h.focus();}else document.getElementById('workbench-content')?.focus();}}>跳到主要内容</a>
    <header className="brand-header"><Brand/><span className="brand-context">负责人工作台</span><span className="brand-tz">时间均为北京时间</span></header><main id="workbench-content" tabIndex={-1}>
    {/* NX-058: the public account pages need no session (the invitation token or reset code is the credential). */}
    {window.location.pathname===ACCOUNT_PATHS.invitation?<InvitationPage/>:window.location.pathname===ACCOUNT_PATHS.reset?<ResetPage/>
      // C13: while the session is checked, the shape of the workbench (navigation and page placeholders), not a line on white.
      :session.isPending?<div className="workbench-layout workbench-skeleton" role="status" aria-busy="true"><span className="sr-only">{STATE_TEXT.loading}</span>
        <div className="skeleton-nav" aria-hidden="true">{Array.from({length:8},(_,i)=><span key={i} className="skeleton-line"/>)}</div>
        <div className="workbench-main" aria-hidden="true"><span className="skeleton-line skeleton-title"/><span className="skeleton-line"/><span className="skeleton-line"/><span className="skeleton-line short"/></div></div>
      :session.isError?<section><h1>暂时无法连接</h1><p role="alert">{STATE_TEXT[kindOf(session.error)]}</p><button type="button" onClick={()=>void session.refetch()}>重试</button></section>
      :!session.data?<WorkbenchLogin onLogin={()=>void session.refetch()}/>
      :<div className="workbench-layout">
        {/* md/sm (<1024): the navigation sits behind this visible menu button instead of a horizontal strip (web layout batch 1). */}
        <button type="button" ref={toggle} className="workbench-menu-toggle" aria-expanded={menu} aria-controls="workbench-nav" onClick={()=>setMenu(!menu)}>
          <span>{menu?'收起菜单':'菜单'}</span><span className="menu-current">{PAGES[route.page]}{route.section?' · '+sectionText(WORLD_SECTIONS.find(s=>s.key===route.section)!.label,sections):''}</span></button>
        <nav id="workbench-nav" className={menu?'open':undefined} aria-label="工作台导航" onKeyDown={navKey}>{NAV_GROUPS.map(g=><div key={g.title} className="nav-group">
          <h2 className="nav-group-title" id={'nav-'+g.title}>{g.title}</h2><ul aria-labelledby={'nav-'+g.title}>{g.pages.filter(shown).map(page=>{
          const children=NAV_CHILDREN[page];const open=children?(opened[page]??route.page===page):false;
          const link=<a href={routePath({page})} aria-current={route.page===page&&!route.section?'page':undefined} className={NOT_ENABLED.includes(page)?'nav-off':undefined}
            onClick={event=>{event.preventDefault();setMenu(false);go({page});}}>{PAGES[page]}{NOT_ENABLED.includes(page)?<span className="nav-badge">未启用</span>:null}</a>;
          return <li key={page}>{children?<><div className="nav-entry">{link}
            <button type="button" className="nav-expand" aria-expanded={open} aria-controls={'nav-sub-'+page} aria-label={(open?'收起':'展开')+PAGES[page]}
              onClick={()=>setOpened({...opened,[page]:!open})}>{open?'▾':'▸'}</button></div>
            {open?<ul id={'nav-sub-'+page} className="nav-sub">{children.map(c=><li key={c.label}><a href={routePath(c.route)}
              aria-current={sameRoute(route,c.route)?'page':undefined} onClick={event=>{event.preventDefault();setMenu(false);go(c.route);}}><SectionTerm name={c.label}/></a></li>)}</ul>:null}</>
            :link}</li>;})}</ul></div>)}
          <button type="button" onClick={()=>logout.mutate()} disabled={logout.isPending}>退出</button></nav>
        <div className="workbench-main">
          <ResetNotice notices={session.data.reset_notices}/>
          {crumbs(route,context,sections).length?<nav className="breadcrumbs" aria-label="面包屑"><ol>{crumbs(route,context,sections).map((c,i)=><li key={i}>{c.to?<a href={routePath(c.to)}
            onClick={event=>{event.preventDefault();go(c.to!);}}>{c.label}</a>:<span aria-current="page">{c.label}</span>}</li>)}</ol></nav>:null}
          <PageErrorBoundary key={routePath(route)}><WorkbenchPage route={route} go={go} slots={slots} onContext={setContext}/></PageErrorBoundary></div>
      </div>}
  </main><footer>NexLoop · 持续运营<span className="footer-tz"> · 时间均为北京时间</span></footer></div>;
}
