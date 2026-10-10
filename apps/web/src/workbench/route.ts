/** Workbench routes live in the URL (/workbench/<page>[/<id>]) so a page and its object can be linked and reloaded. */
export const PAGES={overview:'总览',goals:'目标与对齐',consumers:'消费者',plans:'计划与运行',actions:'Action / 异常',commitments:'承诺 / 交付',contact:'联系限制',
  knowledge:'知识工作台',settings:'设置与治理',ontology:'本体 / 演进',experiments:'实验空间'} as const;
export type PageKey=keyof typeof PAGES;
export type Route={page:PageKey;id?:string;sub?:'conversation'};
const hex64=/^[a-f0-9]{64}$/;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function parseRoute(pathname:string):Route{
  const parts=pathname.replace(/^\/workbench\/?/,'').split('/').filter(Boolean);
  const page=(parts[0]??'overview') as PageKey;
  if(!(page in PAGES))return {page:'overview'};
  if(page==='consumers'&&parts[1]==='conversations'&&hex64.test(parts[2]??''))return {page,id:parts[2],sub:'conversation'};
  if((page==='consumers'||page==='commitments')&&hex64.test(parts[1]??''))return {page,id:parts[1]};
  if(page==='knowledge'&&uuid.test(parts[1]??''))return {page,id:parts[1]};
  return {page};
}
export function routePath(route:Route):string{
  return '/workbench/'+route.page+(route.sub==='conversation'?'/conversations/'+route.id:route.id?'/'+route.id:'');
}
