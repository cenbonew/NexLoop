/**
 * World model glossary page under 管理 (owner walkthrough R57, W5): the world model pages only show glosses; every identifier and its
 * Chinese is browsed, searched and corrected here, by kind. Offered only to a member whose role carries world_glossary_set (the
 * owner); the sidebar hides the entry otherwise and the server decides every correction (GlossEditor: its checks and the sim rule).
 */
import {useEffect,useMemo,useRef,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {offers,readWorldModel,type WorldModel as Model} from '../api';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {Keyword,StatusBadge} from '../ui';
import {EmptyState} from '../kit';
import {WithMember} from '../actions';
import {SectionTerm,WORLD_SECTION_NAMES,glossMatches,type GlossSource} from '../Gloss';
import {GlossEditor,type GlossKind} from '../GlossEdit';

type Row={identifier:string;gloss:string|null;source:GlossSource|null};
/** The kinds, in the order the owner named them; `section` names come from glossary.sections, which carry no source. */
export const GLOSSARY_KINDS:{kind:GlossKind;label:string}[]=[{kind:'object_type',label:'Object Type'},{kind:'property',label:'Property'},
  {kind:'relation',label:'Link'},{kind:'relation_inverse',label:'反向名'},{kind:'action',label:'Action'},{kind:'function',label:'Function'},
  {kind:'event_type',label:'Event Type'},{kind:'section',label:'栏目名'}];
const once=(rows:Row[])=>[...new Map(rows.map(r=>[r.identifier,r])).values()];  // a Property name shared by several types is glossed once

export function glossaryRows(m:Model,kind:GlossKind):Row[]{
  const g=(identifier:string,x:{gloss:string|null;gloss_source:GlossSource|null}):Row=>({identifier,gloss:x.gloss,source:x.gloss_source});
  switch(kind){
    case 'object_type':return m.object_types.map(t=>g(t.name,t));
    case 'property':return once(m.object_types.flatMap(t=>t.properties.map(p=>g(p.name,p))));
    case 'relation':return m.relation_types.map(r=>g(r.name,r));
    case 'relation_inverse':return once(m.relation_types.filter(r=>r.inverse_name).map(r=>({identifier:r.inverse_name,gloss:r.inverse_gloss,source:r.inverse_gloss_source})));
    case 'action':return once(m.actions.map(a=>g(a.name,a)));
    case 'function':return once(m.functions.map(f=>g(f.name,f)));
    case 'event_type':return m.event_types.map(e=>g(e.name,e));
    case 'section':return WORLD_SECTION_NAMES.map(k=>({identifier:k,gloss:m.glossary.sections[k]??null,source:null}));
  }
}

function SourceBadge({row,kind}:{row:Row;kind:GlossKind}){
  if(row.source==='machine')return <StatusBadge text="机翻" tone="neutral" code="machine"/>;
  if(row.source==='manual')return <StatusBadge text={row.gloss?'人工':'人工清空'} tone="ok" code="manual"/>;
  return <span className="note">{kind==='section'?'—':'未翻译'}</span>;
}

export function Glossary(){
  const result=useQuery({queryKey:['workbench','world-model'],queryFn:readWorldModel});
  return <Page title="世界模型译名" query={result}
    lead="世界模型各标识的中文译名：按种类浏览和查找，标出机翻或人工；改正后作为人工译名，机翻不会再覆盖。">{m=><WithMember>{member=>offers(member,'world_glossary_set')
      ?<GlossaryBody model={m}/>:<EmptyState kind="forbidden" subject="世界模型译名" reason="你的角色没有管理世界模型译名的权限。"/>}</WithMember>}</Page>;
}

function GlossaryBody({model:m}:{model:Model}){
  const [kind,setKind]=useState<GlossKind>('object_type');
  const [keyword,setKeyword]=useState('');const [pick,setPick]=useState<string|null>(null);
  const label=GLOSSARY_KINDS.find(k=>k.kind===kind)!.label;
  const rows=useMemo(()=>glossaryRows(m,kind),[m,kind]);
  const shown=rows.filter(r=>glossMatches(keyword,r.identifier,r.gloss));
  const machine=rows.filter(r=>r.source==='machine').length,manual=rows.filter(r=>r.source==='manual').length;
  const real=m.world==='real';
  const editor=useRef<HTMLDivElement>(null);
  // The form opens above the list: bring it into view and put the cursor in the gloss field.
  useEffect(()=>{if(!pick)return;const el=editor.current;el?.scrollIntoView?.({block:'nearest'});el?.querySelector<HTMLInputElement>('input:not([type=checkbox])')?.focus();},[pick]);
  const targets=rows.map(r=>({identifier:r.identifier,gloss:r.gloss}));
  return <>
    <div className="segmented glossary-kinds" role="group" aria-label="译名种类">
      {GLOSSARY_KINDS.map(k=><button key={k.kind} type="button" aria-pressed={kind===k.kind}
        onClick={()=>{setKind(k.kind);setPick(null);}}>{k.kind==='section'||k.kind==='relation_inverse'?k.label:<SectionTerm name={k.label}/>}</button>)}
    </div>
    <div className="world-list-head"><Keyword value={keyword} onChange={setKeyword} label={`查找 ${label}（英文或中文）`}/></div>
    <p className="note">{rows.length} 个标识{kind==='section'?'':` · 机翻 ${machine} · 人工 ${manual}`}</p>
    {!real&&targets.length?<GlossEditor world={m.world} kind={kind} label={` ${label} `} targets={targets}/>:null}
    {real&&pick?<div ref={editor}><GlossEditor key={kind+'/'+pick} world={m.world} kind={kind} label={` ${label} `} targets={targets} pick={pick} onClose={()=>setPick(null)}/></div>:null}
    {rows.length?<DataTable label={`${label} 译名`} rows={shown} rowKey={r=>r.identifier} empty={{filtered:!!keyword.trim()}} columns={[
      {label:'标识',main:true,cell:r=><code className="term-id">{r.identifier}</code>},
      {label:'中文译名',cell:r=>r.gloss??'—'},
      {label:'来源',cell:r=><SourceBadge row={r} kind={kind}/>},
      ...(real?[{label:'操作',cell:(r:Row)=><button type="button" className="btn-secondary" aria-label={`改正 ${r.identifier} 的中文译名`}
        onClick={()=>setPick(r.identifier)}>改正…</button>}]:[])]}/>
      :<EmptyState kind="empty" subject={label}/>}
  </>;
}
