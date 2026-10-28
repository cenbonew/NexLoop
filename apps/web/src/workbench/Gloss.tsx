/**
 * Owner 2026-10-29: every English identifier of the enterprise world model carries a Chinese gloss in full-width parentheses, e.g.
 * Consumer（客户）. The identifier stays monospace and never breaks; the parentheses with the Chinese are secondary, in the body font,
 * and may wrap. No gloss: the identifier alone, never empty parentheses. A machine gloss carries a small 机翻 mark.
 */
import {useContext,useMemo,type ReactNode} from 'react';
import {QueryClientContext} from '@tanstack/react-query';
import type {WorldModel} from './api';

export type GlossSource='manual'|'machine';
export type Glossed={gloss:string|null;gloss_source:GlossSource|null};
export const NO_GLOSS:Glossed={gloss:null,gloss_source:null};
export const MACHINE_HINT='由模型翻译，负责人可改正';

/** The workbench's own section and view names (glossary v1, manual). */
export const SECTION_GLOSS:Record<string,string>={'Object Type':'对象类型',Object:'对象',Property:'属性',Link:'关系',Function:'函数',Action:'动作',
  'Object Set':'对象集','Event Type':'事件类型','Payload Property':'载荷属性',Table:'表格',Graph:'关系图','Object View':'对象视图',Interface:'接口',
  'Shared Property':'共享属性',Scope:'权限范围'};

/** As plain text, for titles, accessible names and search: Consumer（客户）, or the identifier alone. */
export const glossText=(id:string,gloss:string|null|undefined)=>gloss?`${id}（${gloss}）`:id;
export const sectionText=(name:string)=>glossText(name,SECTION_GLOSS[name]);
/** Whether a keyword finds an identifier by its English or its Chinese. */
export const glossMatches=(keyword:string,id:string,gloss:string|null|undefined)=>{
  const k=keyword.trim().toLowerCase();return !k||id.toLowerCase().includes(k)||(!!gloss&&gloss.toLowerCase().includes(k));};

/** An identifier with its gloss. `mono`: identifiers are code (names); section names are words. */
export function Term({id,gloss,source=null,mono=true}:{id:string;gloss:string|null|undefined;source?:GlossSource|null;mono?:boolean}):ReactNode{
  const name=mono?<code className="term-id">{id}</code>:<span className="term-id term-word">{id}</span>;
  if(!gloss)return <span className="term">{name}</span>;
  return <span className="term">{name}<span className="term-gloss">（{gloss}）{source==='machine'?<small className="term-machine" title={MACHINE_HINT}>机翻</small>:null}</span></span>;
}
/** A section or view name: the server's glossary sections when the model is on this page, else glossary v1. */
export function SectionTerm({name}:{name:string}){
  const sections=useContext(QueryClientContext)?.getQueryData<WorldModel>(['workbench','world-model'])?.glossary.sections;
  return <Term id={name} gloss={sections?.[name]??SECTION_GLOSS[name]} mono={false}/>;
}

/** Glosses of the world model already read on this page (the cached model; nothing is fetched here): by type, by a type's
 * Property, by Link or inverse name. Missing: no gloss (the identifier alone). */
export function useWorldGlosses(){
  const m=useContext(QueryClientContext)?.getQueryData<WorldModel>(['workbench','world-model']);
  return useMemo(()=>{
    const types=new Map((m?.object_types??[]).map(t=>[t.name,t]));
    const links=new Map<string,Glossed>();
    for(const r of m?.relation_types??[]){links.set(r.name,r);if(r.inverse_name)links.set(r.inverse_name,{gloss:r.inverse_gloss,gloss_source:r.inverse_gloss_source});}
    return {type:(name:string):Glossed=>types.get(name)??NO_GLOSS,
      property:(type:string,name:string):Glossed=>types.get(type)?.properties.find(p=>p.name===name)??NO_GLOSS,
      link:(name:string):Glossed=>links.get(name)??NO_GLOSS};
  },[m]);
}
