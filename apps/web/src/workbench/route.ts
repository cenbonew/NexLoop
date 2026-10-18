/** Workbench routes live in the URL (/workbench/<page>[/<id>][?filters]) so a page, its object and its list filters can be linked,
 * reloaded and restored on Back (walkthrough F09). Only the query keys a page declares are kept, each with its own check. */
export const PAGES={overview:'总览',goals:'目标与对齐',consumers:'消费者',plans:'计划与运行',actions:'Action / 异常',commitments:'承诺 / 交付',contact:'联系限制',
  knowledge:'知识工作台',alerts:'告警',operations:'运行状态',audit:'审计',settings:'设置与治理',ontology:'企业世界模型',experiments:'实验空间'} as const;
export type PageKey=keyof typeof PAGES;
export type Query=Record<string,string>;
export type Route={page:PageKey;id?:string;sub?:'conversation';query?:Query};
const hex64=/^[a-f0-9]{64}$/;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const day=/^\d{4}-\d{2}-\d{2}$/;
const offset=/^(0|[1-9]\d{0,5})$/;
const words=(v:string)=>v.length>0&&v.length<=80&&!/[\u0000-\u001f]/.test(v);
/** List filters per page (the list pages only; detail pages carry none). */
const QUERY:Partial<Record<PageKey,Record<string,(v:string)=>boolean>>>={
  commitments:{status:v=>['open','ended','all'].includes(v),consumer:v=>hex64.test(v),late:v=>v==='true'||v==='false',due_from:v=>day.test(v),due_to:v=>day.test(v),
    sort:v=>['due_at','registered_at','status'].includes(v),order:v=>v==='asc'||v==='desc',page:v=>offset.test(v),q:words},
  // Enterprise world model (W1): the tab, table or relation graph, and the focused type (deep links and Back walk them).
  ontology:{tab:v=>['object_types','properties','relations','events','actions','functions'].includes(v),view:v=>v==='graph',focus:v=>/^[A-Za-z][A-Za-z0-9_.]{0,159}$/.test(v)},
  consumers:{restricted:v=>v==='true'||v==='false',open:v=>v==='true'||v==='false',sort:v=>['id','open_commitments','conversations','active_plans'].includes(v),
    order:v=>v==='asc'||v==='desc',page:v=>offset.test(v),q:words},
};
function parseQuery(page:PageKey,search:string):Query|undefined{
  const allowed=QUERY[page];if(!allowed)return undefined;
  const out:Query={};
  for(const [k,v] of new URLSearchParams(search))if(allowed[k]?.(v))out[k]=v;
  return Object.keys(out).length?out:undefined;
}
export function parseRoute(pathname:string,search=''):Route{
  const parts=pathname.replace(/^\/workbench\/?/,'').split('/').filter(Boolean);
  const page=(parts[0]??'overview') as PageKey;
  if(!(page in PAGES))return {page:'overview'};
  if(page==='consumers'&&parts[1]==='conversations'&&hex64.test(parts[2]??''))return {page,id:parts[2],sub:'conversation'};
  if((page==='consumers'||page==='commitments')&&hex64.test(parts[1]??''))return {page,id:parts[1]};
  if(page==='knowledge'&&uuid.test(parts[1]??''))return {page,id:parts[1]};
  const query=parseQuery(page,search);
  return query?{page,query}:{page};
}
export function routePath(route:Route):string{
  const path='/workbench/'+route.page+(route.sub==='conversation'?'/conversations/'+route.id:route.id?'/'+route.id:'');
  const allowed=QUERY[route.page];
  const query=!route.id&&allowed&&route.query?Object.entries(route.query).filter(([k,v])=>allowed[k]?.(v)):[];
  return query.length?path+'?'+new URLSearchParams(query).toString():path;
}
