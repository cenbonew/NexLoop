/**
 * Owner 2026-10-29: every English identifier of the enterprise world model carries a Chinese gloss in full-width parentheses, e.g.
 * Consumer（客户）. The identifier stays monospace and never breaks; the parentheses with the Chinese are secondary, in the body font,
 * and never split (see Term for the two layouts). No gloss: the identifier alone, never empty parentheses. A machine gloss carries a small 机翻 mark.
 */
import {useContext,useMemo,useSyncExternalStore,type ReactNode} from 'react';
import {QueryClientContext} from '@tanstack/react-query';
import {WORLD_SECTION_NAMES,type WorldModel} from './api';

export type GlossSource='manual'|'machine';
export type Glossed={gloss:string|null;gloss_source:GlossSource|null};
export const NO_GLOSS:Glossed={gloss:null,gloss_source:null};
export const MACHINE_HINT='由模型翻译，负责人可改正';

/** Section and view names glossed by the server (v2 glossary.sections, the enum WORLD_SECTION_NAMES); none until the model is read. */
export type Sections=Partial<Record<string,string>>;
export {WORLD_SECTION_NAMES};

/** As plain text, for titles, accessible names and search: Consumer（客户）, or the identifier alone. */
export const glossText=(id:string,gloss:string|null|undefined)=>gloss?`${id}（${gloss}）`:id;
/** A section name as text with the server's gloss (English alone without one, e.g. before the model is read). */
export const sectionText=(name:string,sections:Sections|undefined)=>glossText(name,sections?.[name]);
/** Whether a keyword finds an identifier by its English or its Chinese. */
export const glossMatches=(keyword:string,id:string,gloss:string|null|undefined)=>{
  const k=keyword.trim().toLowerCase();return !k||id.toLowerCase().includes(k)||(!!gloss&&gloss.toLowerCase().includes(k));};

/** An identifier with its gloss. `mono`: identifiers are code (names); section names are words.
 * Owner 2026-10-30 (stage walkthrough), one rule per place:
 * - default (table cells, detail panels, lists): two lines in every case - the identifier, then the gloss left-aligned below it,
 *   never broken inside; too narrow, the gloss ends in an ellipsis and its title carries the full text.
 * - `inline` (titles, crumbs, sidebar, switches, a sentence): English（中文） on one line; the gloss moves as a whole, never split. */
export function Term({id,gloss,source=null,mono=true,inline=false}:{id:string;gloss:string|null|undefined;source?:GlossSource|null;mono?:boolean;inline?:boolean}):ReactNode{
  const name=mono?<code className="term-id">{id}</code>:<span className="term-id term-word">{id}</span>;
  const layout=inline?'term term-inline':'term term-stack';
  if(!gloss)return <span className={layout}>{name}</span>;
  const machine=source==='machine'?<small className="term-machine" title={MACHINE_HINT}>机翻</small>:null;
  return <span className={layout}>{name}<span className="term-gloss" title={inline?undefined:gloss}><span className="term-gloss-text">（{gloss}）</span>{machine}</span></span>;
}
const MODEL_KEY=['workbench','world-model'];
const NOTHING=()=>()=>{};
/** The cached world model, followed as it changes (nothing is fetched here); undefined until some page has read it. */
export function useCachedWorldModel():WorldModel|undefined{
  const client=useContext(QueryClientContext);
  return useSyncExternalStore(client?(notify=>client.getQueryCache().subscribe(notify)):NOTHING,
    ()=>client?.getQueryData<WorldModel>(MODEL_KEY),()=>client?.getQueryData<WorldModel>(MODEL_KEY));
}
/** The server's section names (S4: never a copy in the frontend, so an owner's correction shows everywhere). */
export const useSections=():Sections|undefined=>useCachedWorldModel()?.glossary.sections;
/** A section or view name with the server's gloss. */
export function SectionTerm({name}:{name:string}){return <Term id={name} gloss={useSections()?.[name]} mono={false} inline/>;}

/** Glosses of the world model already read on this page (the cached model; nothing is fetched here): by type, by a type's
 * Property, by Link or inverse name. Missing: no gloss (the identifier alone). */
export function useWorldGlosses(){
  const m=useCachedWorldModel();
  return useMemo(()=>{
    const types=new Map((m?.object_types??[]).map(t=>[t.name,t]));
    const links=new Map<string,Glossed>();
    for(const r of m?.relation_types??[]){links.set(r.name,r);if(r.inverse_name)links.set(r.inverse_name,{gloss:r.inverse_gloss,gloss_source:r.inverse_gloss_source});}
    return {type:(name:string):Glossed=>types.get(name)??NO_GLOSS,
      property:(type:string,name:string):Glossed=>types.get(type)?.properties.find(p=>p.name===name)??NO_GLOSS,
      link:(name:string):Glossed=>links.get(name)??NO_GLOSS};
  },[m]);
}
