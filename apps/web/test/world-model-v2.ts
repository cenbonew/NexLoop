// Until L2's v2 contract example lands (docs/design/world-model-glossary.md §4, S2): the v1 contract example in the v2 shape - every
// identifier with gloss/gloss_source null (no translation yet), a Link's inverse likewise, and the top-level glossary. The section
// names are glossary v1's (manual), as a published manifest would give them.
export const GLOSSARY_V1_SECTIONS={'Object Type':'对象类型',Object:'对象',Property:'属性',Link:'关系',Function:'函数',Action:'动作','Object Set':'对象集',
  'Event Type':'事件类型','Payload Property':'载荷属性',Table:'表格',Graph:'关系图','Object View':'对象视图',Interface:'接口','Shared Property':'共享属性'};
type Json=Record<string,unknown>&{[k:string]:any};// eslint-disable-line @typescript-eslint/no-explicit-any
export function asV2<T extends Json>(v1:T,sections:Record<string,string>=GLOSSARY_V1_SECTIONS):T{
  const m=structuredClone(v1) as Json;const none=(o:Json)=>{if(!('gloss' in o))o.gloss=null;if(!('gloss_source' in o))o.gloss_source=null;};
  m.glossary={manifest_version:1,sections};
  for(const t of m.object_types){none(t);for(const p of t.properties)none(p);}
  for(const r of m.relation_types){none(r);if(!('inverse_gloss' in r))r.inverse_gloss=null;if(!('inverse_gloss_source' in r))r.inverse_gloss_source=null;}
  for(const k of ['event_types','actions','functions'])for(const x of m[k])none(x);
  return m as T;
}
