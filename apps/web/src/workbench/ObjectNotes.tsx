// NX-062 M5c (design §11; L6 V-1, V-4): the notes of one Commitment or CommercialRecord on its page. A note is an unverified statement
// by a member or a machine client: shown as plain text only (a text node - never HTML, Markdown or a link), each marked with who wrote
// it and that it is unverified. The input is offered when the member's role carries the Action; the governed entry still decides.
import {useState} from 'react';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {NOTE_INPUT_HINT,newRequestKey,noteAuthorText,offers,readMe,readObjectNotes,workbenchAct,writeKindOf,type NoteObjectType} from './api';
import {Time} from './ui';

const MAX=2000;

export function ObjectNotes({type,objectId}:{type:NoteObjectType;objectId:string}){
  const client=useQueryClient();
  const key=['workbench','object-notes',type,objectId];
  const notes=useQuery({queryKey:key,queryFn:()=>readObjectNotes(type,objectId)});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const [draft,setDraft]=useState('');
  const [requestKey,setRequestKey]=useState(newRequestKey);
  const add=useMutation({
    mutationFn:(text:string)=>workbenchAct('object_note_add',{type_name:type,object_id:objectId,text},requestKey),
    onSuccess:()=>{setDraft('');setRequestKey(newRequestKey());void client.invalidateQueries({queryKey:key});},
  });
  const trimmed=draft.trim();
  return <section className="object-notes" aria-label="备注">
    <h2>备注</h2>
    {notes.isError?<p role="status">备注暂时读不到。</p>:null}
    {notes.data&&notes.data.length===0?<p className="workbench-summary">还没有备注。</p>:null}
    {notes.data&&notes.data.length?<ul className="note-list">{notes.data.map(n=><li key={n.note_id} data-author-kind={n.author_kind}>
      <p className="note-author">{noteAuthorText(n)} · <Time value={n.created_at}/></p>
      <p className="note-text">{n.text}</p>
    </li>)}</ul>:null}
    {offers(member.data,'object_note_add')?<form onSubmit={e=>{e.preventDefault();if(trimmed&&!add.isPending)add.mutate(trimmed);}}>
      <label htmlFor={`note-${objectId}`}>添加备注</label>
      <p className="field-hint" id={`note-hint-${objectId}`}>{NOTE_INPUT_HINT}</p>
      <textarea id={`note-${objectId}`} aria-describedby={`note-hint-${objectId}`} maxLength={MAX} value={draft} onChange={e=>setDraft(e.target.value)}/>
      <button type="submit" disabled={!trimmed||add.isPending}>{add.isPending?'正在保存…':'保存备注'}</button>
      {add.isError?<p role="alert">没有保存（{writeKindOf(add.error)}）。</p>:null}
    </form>:null}
  </section>;
}
