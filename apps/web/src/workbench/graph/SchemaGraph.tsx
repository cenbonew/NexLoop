/**
 * Enterprise world model W1: the schema relation graph (design §4; D2 hand-written SVG). Object types are nodes, relation types are
 * directed edges labelled with their name and cardinality. Keyboard complete: every node is a focusable button (Tab / Shift+Tab),
 * Enter or Space selects it (the page shows its properties and relations beside the graph), +/-/0 zoom and arrow keys pan when the
 * graph itself has focus. The same data is always available as a table (the page's "表格" view); the SVG has an accessible name and
 * a text summary.
 */
import {useMemo,useState,type KeyboardEvent} from 'react';
import {NODE_H,NODE_W,border,hue,layout,type GraphEdge,type GraphNode} from './layout';

export const CARDINALITY_TEXT:Record<string,string>={one_to_one:'1:1',one_to_many:'1:N',many_to_one:'N:1',many_to_many:'N:N'};
const HUES=['#2563eb','#0f766e','#7c3aed','#b45309','#be185d','#15803d','#4338ca','#0369a1'];
const short=(s:string,n=18)=>s.length>n?s.slice(0,n-1)+'…':s;

export function SchemaGraph({nodes,edges,focus,onFocus,label}:{nodes:GraphNode[];edges:GraphEdge[];focus:string|null;onFocus:(id:string|null)=>void;label:string}){
  const placed=useMemo(()=>layout(nodes,edges),[nodes,edges]);
  const at=useMemo(()=>new Map(placed.nodes.map(n=>[n.id,n])),[placed]);
  const [zoom,setZoom]=useState(1);const [pan,setPan]=useState({x:0,y:0});
  const w=placed.width/zoom,h=placed.height/zoom;
  const cx=placed.width/2+pan.x,cy=placed.height/2+pan.y;
  const near=focus?new Set([focus,...edges.filter(e=>e.from===focus||e.to===focus).flatMap(e=>[e.from,e.to])]):null;
  function keys(event:KeyboardEvent<SVGSVGElement>){
    if(event.target!==event.currentTarget)return;
    const step=60/zoom;
    const move:Record<string,()=>void>={'+':()=>setZoom(z=>Math.min(3,z*1.25)),'=':()=>setZoom(z=>Math.min(3,z*1.25)),'-':()=>setZoom(z=>Math.max(0.4,z/1.25)),
      '0':()=>{setZoom(1);setPan({x:0,y:0});},ArrowLeft:()=>setPan(p=>({...p,x:p.x-step})),ArrowRight:()=>setPan(p=>({...p,x:p.x+step})),
      ArrowUp:()=>setPan(p=>({...p,y:p.y-step})),ArrowDown:()=>setPan(p=>({...p,y:p.y+step})),Escape:()=>onFocus(null)};
    if(move[event.key]){event.preventDefault();move[event.key]();}
  }
  return <div className="schema-graph" style={{position:'relative'}}>
    <div className="schema-graph-tools" role="group" aria-label="缩放" style={{display:'flex',gap:8,marginBottom:8}}>
      <button type="button" onClick={()=>setZoom(z=>Math.min(3,z*1.25))} aria-label="放大">＋</button>
      <button type="button" onClick={()=>setZoom(z=>Math.max(0.4,z/1.25))} aria-label="缩小">－</button>
      <button type="button" onClick={()=>{setZoom(1);setPan({x:0,y:0});}}>复位</button>
      <span className="note">点击或用 Tab 选中类型，Enter 查看；图获得焦点时方向键平移、+/− 缩放。</span>
    </div>
    <svg role="group" aria-label={label} tabIndex={0} onKeyDown={keys} width="100%" style={{maxHeight:'70vh',border:'1px solid var(--border, #d0d7de)',borderRadius:8,display:'block'}}
      viewBox={`${cx-w/2} ${cy-h/2} ${w} ${h}`} preserveAspectRatio="xMidYMid meet" data-nodes={placed.nodes.length} data-edges={edges.length}>
      <defs><marker id="schema-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
      <g className="schema-edges" style={{color:'var(--muted, #57606a)'}}>
        {edges.map(e=>{const a=at.get(e.from),b=at.get(e.to);if(!a||!b)return null;
          const dim=near&&!(near.has(e.from)&&near.has(e.to)&&(e.from===focus||e.to===focus));
          if(e.from===e.to){const x=a.x+NODE_W/2,y=a.y;return <g key={e.id} opacity={dim?0.25:1}><path d={`M${x},${y-8} c40,-40 40,56 0,16`} fill="none" stroke="currentColor"/>
            {e.label?<text x={x+36} y={y} fontSize={11} fill="currentColor">{e.label}</text>:null}</g>;}
          const s=border(a.x,a.y,b.x,b.y),t=border(b.x,b.y,a.x,a.y);
          return <g key={e.id} opacity={dim?0.25:1}><line x1={s.x} y1={s.y} x2={t.x} y2={t.y} stroke="currentColor" strokeWidth={1.4} markerEnd="url(#schema-arrow)"/>
            {e.label?<text x={(s.x+t.x)/2} y={(s.y+t.y)/2-4} fontSize={11} textAnchor="middle" fill="currentColor" style={{paintOrder:'stroke',stroke:'var(--surface, #fff)',strokeWidth:3}}>{e.label}</text>:null}</g>;})}
      </g>
      <g className="schema-nodes">
        {nodes.map(n=>{const p=at.get(n.id);if(!p)return null;const selected=focus===n.id;const color=HUES[hue(n.id)];
          return <g key={n.id} role="button" tabIndex={0} aria-label={`${n.label}${n.sub?'，'+n.sub:''}${selected?'，已选中':''}`} aria-pressed={selected}
            data-node={n.id} opacity={near&&!near.has(n.id)?0.35:1} style={{cursor:'pointer',outline:'none'}}
            onClick={()=>onFocus(selected?null:n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onFocus(selected?null:n.id);}}}>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={NODE_W} height={NODE_H} rx={12} fill="var(--surface, #fff)" stroke={color} strokeWidth={selected?3:1.5}/>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={6} height={NODE_H} rx={3} fill={color}/>
            <text x={p.x} y={p.y-(n.sub?3:-4)} fontSize={13} fontWeight={600} textAnchor="middle" fill="var(--text, #1f2328)">{short(n.label)}</text>
            {n.sub?<text x={p.x} y={p.y+14} fontSize={11} textAnchor="middle" fill="var(--muted, #57606a)">{short(n.sub,24)}</text>:null}
            <title>{n.label}{n.sub?` · ${n.sub}`:''}</title></g>;})}
      </g>
    </svg>
  </div>;
}
