/**
 * World model glossary, the owner's correction (docs/design/world-model-glossary.md §3.3/§4): nexloop.world_glossary.set takes
 * {kind, identifier, gloss}; the gloss is 1-24 characters with Chinese in it, or "" to clear it (English only from then on; a machine
 * translation no longer replaces it). Offered only to a role that carries the Action (the owner); the server decides.
 */
import {useId,useState} from 'react';
import {Form,WithMember} from './actions';
import {offers} from './api';
import {FormField} from './kit';
import {glossText} from './Gloss';

export type GlossKind='section'|'object_type'|'property'|'relation'|'relation_inverse'|'event_type'|'function'|'action';
export type GlossTarget={identifier:string;gloss:string|null};
const HAN=/\p{Script=Han}/u;
/** Why a gloss cannot be saved, or null. Empty means clear, which `clear` must say explicitly. */
export function glossProblem(gloss:string,clear:boolean):string|null{
  if(clear)return null;
  const g=gloss.trim();
  if(!g)return '填写中文译名，或勾选“清空中文”';
  if([...g].length>24)return '译名最多 24 个字';
  if(!HAN.test(g))return '译名需要包含中文';
  return null;
}

/** One kind's correction: pick the identifier, write its Chinese or clear it. */
export function GlossEditor({kind,label,targets}:{kind:GlossKind;label:string;targets:GlossTarget[]}){
  return <WithMember>{member=>offers(member,'world_glossary_set')&&targets.length?<GlossEditorForm kind={kind} label={label} targets={targets} member={member}/>:null}</WithMember>;
}

function GlossEditorForm({kind,label,targets,member}:{kind:GlossKind;label:string;targets:GlossTarget[];member:Parameters<typeof Form>[0]['member']}){
  const id=useId();
  const [open,setOpen]=useState(false);const [identifier,setIdentifier]=useState(targets[0].identifier);
  const current=targets.find(t=>t.identifier===identifier)??targets[0];
  const [gloss,setGloss]=useState(current.gloss??'');const [clear,setClear]=useState(false);
  if(!open)return <button type="button" className="btn-secondary gloss-edit-open" onClick={()=>setOpen(true)}>改正{label}的中文译名</button>;
  const problem=glossProblem(gloss,clear);
  return <div className="gloss-edit">
    <Form operation="world_glossary_set" member={member} title={`改正${label}的中文译名`} level="h3" disabled={!!problem} disabledReason={problem??undefined}
      body={()=>problem?null:{kind,identifier:current.identifier,gloss:clear?'':gloss.trim()}}>
      <FormField id={id+'-identifier'} label="标识">
        <select id={id+'-identifier'} value={current.identifier} onChange={e=>{const t=targets.find(x=>x.identifier===e.target.value)!;setIdentifier(t.identifier);setGloss(t.gloss??'');setClear(false);}}>
          {targets.map(t=><option key={t.identifier} value={t.identifier}>{glossText(t.identifier,t.gloss)}</option>)}</select></FormField>
      <FormField id={id+'-gloss'} label="中文译名" hint="1–24 个字，需含中文；保存后作为人工译名，机翻不会再覆盖。">
        <input id={id+'-gloss'} value={gloss} maxLength={24} disabled={clear} onChange={e=>setGloss(e.target.value)}/></FormField>
      <label className="choice"><input type="checkbox" checked={clear} onChange={e=>setClear(e.target.checked)}/>清空中文（只显示英文）</label>
    </Form>
    <button type="button" className="btn-secondary" onClick={()=>setOpen(false)}>收起</button>
  </div>;
}
