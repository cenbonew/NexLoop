/** Workbench routes live in the URL (/workbench/<page>[/<id>][?filters]) so a page, its object and its list filters can be linked,
 * reloaded and restored on Back (walkthrough F09). Only the query keys a page declares are kept, each with its own check. */
export const PAGES={overview:'总览',goals:'目标与对齐',consumers:'消费者',plans:'计划与运行',actions:'Action / 异常',approvals:'审批',commitments:'承诺 / 交付',contact:'联系限制',
  knowledge:'知识工作台',alerts:'告警',operations:'运行状态',audit:'审计',settings:'设置与治理',personas:'运营人设',capabilities:'能力目录',ontology:'企业世界模型',data_assets:'数据资产',data_health:'数据健康',experiments:'实验空间'} as const;
export type PageKey=keyof typeof PAGES;
export type Query=Record<string,string>;
/** Enterprise world model sections (EIOS navigation: Object Type, Object, Property, Link, Function, Action), one page each under
 * /workbench/ontology/<path>; the group's own page (/workbench/ontology) is the summary. Object Set (ADR-036) follows Action; Object
 * View and Interface have no NexLoop data and no page (capability gap D-G2/P2). */
export const WORLD_SECTIONS=[{key:'object_types',path:'object-types',label:'Object Type'},{key:'objects',path:'objects',label:'Object'},
  {key:'properties',path:'properties',label:'Property'},{key:'links',path:'links',label:'Link'},{key:'functions',path:'functions',label:'Function'},
  {key:'actions',path:'actions',label:'Action'},{key:'object_sets',path:'object-sets',label:'Object Set'}] as const;
export type WorldSection=typeof WORLD_SECTIONS[number]['key'];
export type Route={page:PageKey;id?:string;sub?:'conversation';section?:WorldSection;query?:Query};
const hex64=/^[a-f0-9]{64}$/;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const day=/^\d{4}-\d{2}-\d{2}$/;
const offset=/^(0|[1-9]\d{0,5})$/;
const words=(v:string)=>v.length>0&&v.length<=80&&!/[\u0000-\u001f]/.test(v);
/** List filters per page (the list pages only; detail pages carry none). */
const QUERY:Partial<Record<PageKey,Record<string,(v:string)=>boolean>>>={
  commitments:{status:v=>['open','ended','all'].includes(v),consumer:v=>hex64.test(v),late:v=>v==='true'||v==='false',due_from:v=>day.test(v),due_to:v=>day.test(v),
    sort:v=>['due_at','registered_at','status'].includes(v),order:v=>v==='asc'||v==='desc',page:v=>offset.test(v),q:words},
  capabilities:{tab:v=>['capabilities','actions','channels','dispatch'].includes(v)},
  consumers:{restricted:v=>v==='true'||v==='false',open:v=>v==='true'||v==='false',sort:v=>['id','open_commitments','conversations','active_plans'].includes(v),
    order:v=>v==='asc'||v==='desc',page:v=>offset.test(v),q:words},
};
const typeName=(v:string)=>/^[A-Za-z][A-Za-z0-9_.]{0,159}$/.test(v),ref=(v:string)=>/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(v);
/** World model sections: Object Type keeps the Table / Graph view and the focused type; Object the type, page cursor and open
 * object (W2). Deep links and Back walk them. */
const WORLD_QUERY:Partial<Record<WorldSection,Record<string,(v:string)=>boolean>>>={
  object_types:{view:v=>v==='graph',focus:typeName},
  objects:{type:v=>/^[A-Z][A-Za-z0-9_]{0,63}$/.test(v),after:ref,object:ref},
  object_sets:{set:v=>/^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$/.test(v),offset:v=>/^(0|[1-9]\d{0,3})$/.test(v)&&Number(v)<=1000}};
/** Links of the earlier single page (?tab=...) open the same content in its section. */
const LEGACY_TAB:Record<string,WorldSection>={object_types:'object_types',properties:'properties',relations:'links',actions:'actions',functions:'functions',instances:'objects'};
function allowedFor(route:{page:PageKey;section?:WorldSection}){return route.page==='ontology'?(route.section?WORLD_QUERY[route.section]:undefined):QUERY[route.page];}
function parseQuery(page:PageKey,search:string,section?:WorldSection):Query|undefined{
  const allowed=allowedFor({page,section});if(!allowed)return undefined;
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
  if(page==='ontology'){
    let section=WORLD_SECTIONS.find(s=>s.path===parts[1])?.key;
    const legacy=new URLSearchParams(search).get('tab');
    if(!section&&!parts[1]&&legacy&&LEGACY_TAB[legacy])section=LEGACY_TAB[legacy];
    if(!section&&!parts[1]&&new URLSearchParams(search).get('view')==='graph')section='object_types';
    if(!section)return {page};
    const query=parseQuery(page,search,section);
    return query?{page,section,query}:{page,section};
  }
  const query=parseQuery(page,search);
  return query?{page,query}:{page};
}
export function routePath(route:Route):string{
  const section=route.page==='ontology'&&route.section?'/'+WORLD_SECTIONS.find(s=>s.key===route.section)!.path:'';
  const path='/workbench/'+route.page+(route.sub==='conversation'?'/conversations/'+route.id:route.id?'/'+route.id:'')+section;
  const allowed=allowedFor(route);
  const query=!route.id&&allowed&&route.query?Object.entries(route.query).filter(([k,v])=>allowed[k]?.(v)):[];
  return query.length?path+'?'+new URLSearchParams(query).toString():path;
}
