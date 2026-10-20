/**
 * Enterprise world model W1: the schema relation graph (design §4; D2 hand-written SVG). Object Types are nodes, Links are directed
 * edges labelled with their name and cardinality. Keyboard complete: every node is a focusable button (Tab / Shift+Tab), Enter or
 * Space selects it (the page shows its Properties and Links beside the graph), +/-/0 zoom and arrow keys pan when the graph itself
 * has focus. The same data is always available as a table (the page's Table view); the SVG has an accessible name and a text summary.
 *
 * Fit and legibility (owner review of release 23, 2026-10-20): the picture opens fitted to the canvas (as large as it fits, never
 * smaller than the label floor), and no zoom makes a node label smaller than MIN_LABEL_PX on screen: past the floor the canvas pans
 * instead. Without any Link the nodes are a grid filling the canvas and the graph says so.
 */
import {useEffect,useMemo,useRef,useState,type KeyboardEvent} from 'react';
import {NODE_H,NODE_W,border,hue,layout,selfLoop,type GraphEdge,type GraphNode} from './layout';

export const CARDINALITY_TEXT:Record<string,string>={one_to_one:'1:1',one_to_many:'1:N',many_to_one:'N:1',many_to_many:'N:N'};
const HUES=['#2563eb','#0f766e','#7c3aed','#b45309','#be185d','#15803d','#4338ca','#0369a1'];
const short=(s:string,n=18)=>s.length>n?s.slice(0,n-1)+'…':s;

/** Label sizes in layout units; on screen they are multiplied by the scale, which never goes below MIN_SCALE. */
export const LABEL_PX=14,SUB_PX=12,EDGE_PX=12,MIN_LABEL_PX=12;
export const MIN_SCALE=MIN_LABEL_PX/LABEL_PX,MAX_SCALE=2.5;
/** The canvas: the measured width (a server render assumes DEFAULT_W) and at most MAX_H tall. */
export const DEFAULT_W=960,MAX_H=560,MIN_H=240;

const clamp=(v:number,lo:number,hi:number)=>Math.min(hi,Math.max(lo,v));
/** The scale that fits the whole layout into the canvas, kept between the label floor and MAX_SCALE. */
export function fitScale(layoutW:number,layoutH:number,canvasW:number,canvasH=MAX_H):number{
  return clamp(Math.min(canvasW/layoutW,canvasH/layoutH),MIN_SCALE,MAX_SCALE);
}

export function SchemaGraph({nodes,edges,focus,onFocus,label,emptyEdgesText}:{nodes:GraphNode[];edges:GraphEdge[];focus:string|null;
  onFocus:(id:string|null)=>void;label:string;emptyEdgesText?:string}){
  const box=useRef<HTMLDivElement>(null);
  const [canvasW,setCanvasW]=useState(DEFAULT_W);
  useEffect(()=>{const el=box.current;if(!el||typeof ResizeObserver==='undefined')return;
    const seen=new ResizeObserver(([e])=>{const w=Math.round(e.contentRect.width);if(w>0)setCanvasW(w);});seen.observe(el);return ()=>seen.disconnect();},[]);
  const placed=useMemo(()=>layout(nodes,edges,canvasW/MAX_H),[nodes,edges,canvasW]);
  const at=useMemo(()=>new Map(placed.nodes.map(n=>[n.id,n])),[placed]);
  const drawn=edges.filter(e=>at.has(e.from)&&at.has(e.to));
  const fit=fitScale(placed.width,placed.height,canvasW);
  const [zoom,setZoom]=useState<number|null>(null);  // null: fitted (follows the canvas width)
  const [pan,setPan]=useState({x:0,y:0});
  const scale=zoom??fit;
  const canvasH=Math.round(clamp(placed.height*scale,MIN_H,MAX_H));
  const viewW=canvasW/scale,viewH=canvasH/scale;
  const x0=(placed.width-viewW)/2+pan.x,y0=(placed.height-viewH)/2+pan.y;
  const near=focus?new Set([focus,...edges.filter(e=>e.from===focus||e.to===focus).flatMap(e=>[e.from,e.to])]):null;
  const zoomBy=(k:number)=>setZoom(z=>clamp((z??fit)*k,MIN_SCALE,MAX_SCALE));
  const reset=()=>{setZoom(null);setPan({x:0,y:0});};
  function keys(event:KeyboardEvent<SVGSVGElement>){
    if(event.target!==event.currentTarget)return;
    const step=60/scale;
    const move:Record<string,()=>void>={'+':()=>zoomBy(1.25),'=':()=>zoomBy(1.25),'-':()=>zoomBy(1/1.25),'0':reset,
      ArrowLeft:()=>setPan(p=>({...p,x:p.x-step})),ArrowRight:()=>setPan(p=>({...p,x:p.x+step})),
      ArrowUp:()=>setPan(p=>({...p,y:p.y-step})),ArrowDown:()=>setPan(p=>({...p,y:p.y+step})),Escape:()=>onFocus(null)};
    if(move[event.key]){event.preventDefault();move[event.key]();}
  }
  const grid=drawn.length===0;
  return <div className="schema-graph" ref={box} style={{position:'relative'}}>
    <div className="schema-graph-tools" role="group" aria-label="缩放" style={{display:'flex',gap:8,marginBottom:8,flexWrap:'wrap',alignItems:'center'}}>
      <button type="button" onClick={()=>zoomBy(1.25)} aria-label="放大">＋</button>
      <button type="button" onClick={()=>zoomBy(1/1.25)} aria-label="缩小" disabled={scale<=MIN_SCALE+1e-9}>－</button>
      <button type="button" onClick={reset}>适应画布</button>
      <span className="note">点击或用 Tab 选中，Enter 查看；图获得焦点时方向键平移、+/− 缩放、0 适应画布。</span>
    </div>
    {grid&&emptyEdgesText?<p className="note" role="status" data-empty-edges="">{emptyEdgesText}</p>:null}
    <svg role="group" aria-label={label} tabIndex={0} onKeyDown={keys} width="100%" height={canvasH}
      style={{border:'1px solid var(--border, #d0d7de)',borderRadius:8,display:'block'}}
      viewBox={`${x0} ${y0} ${viewW} ${viewH}`} preserveAspectRatio="xMidYMid meet" data-nodes={placed.nodes.length} data-edges={drawn.length}
      data-layout={grid?'grid':'graph'} data-scale={scale.toFixed(3)} data-label-px={(LABEL_PX*scale).toFixed(2)}>
      <defs><marker id="schema-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
      <g className="schema-edges" style={{color:'var(--text, #1f2328)'}}>
        {drawn.map(e=>{const a=at.get(e.from)!,b=at.get(e.to)!;
          const dim=near&&!(near.has(e.from)&&near.has(e.to)&&(e.from===focus||e.to===focus));
          if(e.from===e.to){const loop=selfLoop(a.x,a.y);return <g key={e.id} opacity={dim?0.25:0.85} data-self-loop={e.id}>
            <path d={loop.d} fill="none" stroke="currentColor" strokeWidth={2} markerEnd="url(#schema-arrow)"/>
            {e.label?<text x={loop.label.x} y={loop.label.y} fontSize={EDGE_PX} fill="currentColor" style={{paintOrder:'stroke',stroke:'var(--surface, #fff)',strokeWidth:4}}>{e.label}</text>:null}</g>;}
          const s=border(a.x,a.y,b.x,b.y),t=border(b.x,b.y,a.x,a.y);
          return <g key={e.id} opacity={dim?0.25:0.85} data-edge={e.id}><line x1={s.x} y1={s.y} x2={t.x} y2={t.y} stroke="currentColor" strokeWidth={2} markerEnd="url(#schema-arrow)"/>
            {e.label?<text x={(s.x+t.x)/2} y={(s.y+t.y)/2-6} fontSize={EDGE_PX} textAnchor="middle" fill="currentColor" style={{paintOrder:'stroke',stroke:'var(--surface, #fff)',strokeWidth:4}}>{e.label}</text>:null}</g>;})}
      </g>
      <g className="schema-nodes">
        {nodes.map(n=>{const p=at.get(n.id);if(!p)return null;const selected=focus===n.id;const color=HUES[hue(n.id)];
          return <g key={n.id} role="button" tabIndex={0} aria-label={`${n.label}${n.sub?'，'+n.sub:''}${selected?'，已选中':''}`} aria-pressed={selected}
            data-node={n.id} opacity={near&&!near.has(n.id)?0.35:1} style={{cursor:'pointer',outline:'none'}}
            onClick={()=>onFocus(selected?null:n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onFocus(selected?null:n.id);}}}>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={NODE_W} height={NODE_H} rx={12} fill="var(--surface, #fff)" stroke={color} strokeWidth={selected?3:1.5}/>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={6} height={NODE_H} rx={3} fill={color}/>
            <text x={p.x} y={p.y-(n.sub?3:-5)} fontSize={LABEL_PX} fontWeight={600} textAnchor="middle" fill="var(--text, #1f2328)">{short(n.label)}</text>
            {n.sub?<text x={p.x} y={p.y+15} fontSize={SUB_PX} textAnchor="middle" fill="var(--muted, #57606a)">{short(n.sub,24)}</text>:null}
            <title>{n.label}{n.sub?` · ${n.sub}`:''}</title></g>;})}
      </g>
    </svg>
  </div>;
}
