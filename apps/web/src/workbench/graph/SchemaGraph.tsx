/**
 * Enterprise world model W1: the schema relation graph (design §4; D2 hand-written SVG). Object Types are nodes, Links are directed
 * edges labelled with their name and cardinality. Keyboard complete: every node is a focusable button (Tab / Shift+Tab), Enter or
 * Space selects it (the page shows its Properties and Links beside the graph), +/-/0 zoom and arrow keys pan when the graph itself
 * has focus. The same data is always available as a table (the page's Table view); the SVG has an accessible name and a text summary.
 *
 * Fit and legibility (owner review of release 23, 2026-10-20): the picture opens fitted to the canvas (as large as it fits, never
 * smaller than the label floor), and no zoom makes a node label smaller than MIN_LABEL_PX on screen: past the floor the canvas pans
 * instead. Without any Link the nodes are a grid filling the canvas and the graph says so.
 * Every node in view (owner report from stage, 2026-10-28): the layout is never wider than the canvas at the label floor, and when
 * the fitted picture is taller than MAX_H the canvas grows to it, so the default view and "适应画布" frame every node.
 */
import {useEffect,useLayoutEffect,useMemo,useRef,useState,type KeyboardEvent,type ReactNode} from 'react';
import {MAX_GRAPH_TYPES,NODE_H,NODE_W,edgePaths,hue,layout,type GraphEdge,type GraphNode} from './layout';

export const CARDINALITY_TEXT:Record<string,string>={one_to_one:'1:1',one_to_many:'1:N',many_to_one:'N:1',many_to_many:'N:N'};
const HUES=Array.from({length:8},(_,i)=>`var(--hue-${i+1})`);  // UI spec §1: the graph hues are tokens (light and dark)
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
/** The canvas height at a scale: the picture's height, at least MIN_H; at most MAX_H, or the fitted picture's height when that is
 * taller (the layout's width fits the canvas at the floor, so only the height can be short). */
export function canvasHeight(layoutH:number,scale:number,fit:number):number{
  return Math.round(clamp(layoutH*scale,MIN_H,Math.max(MAX_H,layoutH*fit)));
}

export function SchemaGraph({nodes,edges,focus,onFocus,label,emptyEdgesText,aside}:{nodes:GraphNode[];edges:GraphEdge[];focus:string|null;
  onFocus:(id:string|null)=>void;label:string;emptyEdgesText?:string;aside?:ReactNode}){
  const box=useRef<SVGSVGElement>(null);
  const [canvasW,setCanvasW]=useState(DEFAULT_W);
  // L6 I24: when the detail closes (its 关闭 unmounts with it), focus goes back to the node it was opened from, or the canvas.
  const was=useRef<string|null>(focus);
  useEffect(()=>{const before=was.current;was.current=focus;if(focus!==null||before===null)return;
    const svg=box.current;if(!svg)return;const a=document.activeElement;if(a&&a!==document.body&&svg.contains(a))return;
    (svg.querySelector<SVGGElement>(`[data-node="${CSS.escape(before)}"]`)??svg).focus();},[focus]);
  // The width is read once before the first paint (the picture is laid out for the real canvas, not DEFAULT_W and then shrunk),
  // then followed with a ResizeObserver.
  useLayoutEffect(()=>{const el=box.current;if(!el)return;const w=Math.round(el.getBoundingClientRect().width);if(w>0)setCanvasW(w);
    if(typeof ResizeObserver==='undefined')return;
    const seen=new ResizeObserver(([e])=>{const w=Math.round(e.contentRect.width);if(w>0)setCanvasW(w);});seen.observe(el);return ()=>seen.disconnect();},[]);
  // The widest picture the canvas shows at the label floor, in 40-unit steps (a resize does not re-run the layout every pixel).
  const maxWidth=Math.floor(canvasW/MIN_SCALE/40)*40;
  const placed=useMemo(()=>layout(nodes,edges,canvasW/MAX_H,maxWidth),[nodes,edges,canvasW,maxWidth]);
  const at=useMemo(()=>new Map(placed.nodes.map(n=>[n.id,n])),[placed]);
  const paths=useMemo(()=>edgePaths(placed.nodes,edges,EDGE_PX,placed),[placed,edges]);
  const drawn=edges.filter(e=>at.has(e.from)&&at.has(e.to));
  const edgeOf=new Map(drawn.map(e=>[e.id,e]));
  const fit=fitScale(placed.width,placed.height,canvasW);
  const [zoom,setZoom]=useState<number|null>(null);  // null: fitted (follows the canvas width)
  const [pan,setPan]=useState({x:0,y:0});
  const scale=zoom??fit;
  const canvasH=canvasHeight(placed.height,scale,fit);
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
  return <div className="schema-graph">
    <div className="schema-graph-tools" role="group" aria-label="缩放">
      <button type="button" onClick={()=>zoomBy(1.25)} aria-label="放大">＋</button>
      <button type="button" onClick={()=>zoomBy(1/1.25)} aria-label="缩小" disabled={scale<=MIN_SCALE+1e-9}>－</button>
      <button type="button" onClick={reset}>适应画布</button>
      <span className="note">点击或用 Tab 选中，Enter 查看；图获得焦点时方向键平移、+/− 缩放、0 适应画布。{paths.some(p=>p.crowded)?'放不下的关系名在选中一端类型时显示。':''}{placed.capped?`有关系的类型超过 ${MAX_GRAPH_TYPES} 个，按名称排成网格；关系的走向请看表格视图。`:''}</span>
    </div>
    {grid&&emptyEdgesText?<p className="note" role="status" data-empty-edges="">{emptyEdgesText}</p>:null}
    {/* Owner walkthrough R57 (W2): a detail panel (aside) sits beside the canvas in one row, as tall as the canvas, top and bottom aligned. */}
    <div className={'schema-graph-body'+(aside?' with-aside':'')}>
    <svg ref={box} role="group" aria-label={label} tabIndex={0} onKeyDown={keys} width="100%" height={canvasH}
      className="schema-graph-canvas"
      viewBox={`${x0} ${y0} ${viewW} ${viewH}`} preserveAspectRatio="xMidYMid meet" data-nodes={placed.nodes.length} data-edges={drawn.length}
      data-layout={grid?'grid':placed.capped?'capped':'graph'} data-scale={scale.toFixed(3)} data-label-px={(LABEL_PX*scale).toFixed(2)}>
      <defs><marker id="schema-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
      <g className="schema-edges">
        {paths.map(p=>{const e=edgeOf.get(p.id)!;
          const touches=e.from===focus||e.to===focus;
          const dim=near&&!(near.has(e.from)&&near.has(e.to)&&touches);
          // A crowded label (no clear spot) is drawn only while one of its types is selected; its name is always the edge's title.
          const shown=e.label&&p.label&&(!p.crowded||touches);
          return <g key={p.id} opacity={dim?0.25:0.85} {...(p.self?{'data-self-loop':p.id}:{'data-edge':p.id})} {...(p.crowded?{'data-crowded':''}:{})}>
            <path d={p.d} fill="none" stroke="currentColor" strokeWidth={2} markerEnd="url(#schema-arrow)"/>
            {e.label?<title>{`${e.from} → ${e.to}：${e.label}`}</title>:null}
            {shown?<text x={p.label!.x} y={p.label!.y} fontSize={EDGE_PX} textAnchor={p.anchor} dominantBaseline="middle" fill="currentColor"
              className="schema-edge-label">{e.label}</text>:null}</g>;})}
      </g>
      <g className="schema-nodes">
        {nodes.map(n=>{const p=at.get(n.id);if(!p)return null;const selected=focus===n.id;const color=HUES[hue(n.id)];
          return <g key={n.id} role="button" tabIndex={0} aria-label={`${n.gloss?`${n.label}（${n.gloss}）`:n.label}${n.sub?'，'+n.sub:''}${selected?'，已选中':''}`} aria-pressed={selected}
            data-node={n.id} opacity={near&&!near.has(n.id)?0.35:1} className="schema-node"
            onClick={()=>onFocus(selected?null:n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onFocus(selected?null:n.id);}}}>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={NODE_W} height={NODE_H} rx={12} fill="var(--surface, #fff)" stroke={color} strokeWidth={selected?3:1.5}/>
            <rect x={p.x-NODE_W/2} y={p.y-NODE_H/2} width={6} height={NODE_H} rx={3} fill={color}/>
            <text x={p.x} y={p.y-(n.sub||n.gloss?3:-5)} fontSize={LABEL_PX} fontWeight={600} textAnchor="middle" fill="var(--text)" className="schema-node-id">{short(n.label)}</text>
            {n.gloss?<text x={p.x} y={p.y+15} fontSize={SUB_PX} textAnchor="middle" fill="var(--muted)" className="schema-node-gloss">{short(`（${n.gloss}）`,12)}</text>
              :n.sub?<text x={p.x} y={p.y+15} fontSize={SUB_PX} textAnchor="middle" fill="var(--muted)">{short(n.sub,24)}</text>:null}
            <title>{`${n.gloss?`${n.label}（${n.gloss}）`:n.label}${n.sub?` · ${n.sub}`:''}`}</title></g>;})}
      </g>
    </svg>{aside}</div>
  </div>;
}
