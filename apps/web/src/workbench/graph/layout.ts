/**
 * Enterprise world model W1 (design §4, D2: hand-written SVG, no library): a deterministic layout of the schema graph. The same
 * types and relations always give the same picture (stable screenshots, stable tests). Pure: no DOM, no randomness, no clock.
 *
 * Bounded width (owner report from stage, 2026-10-28): the picture is never wider than `maxWidth` - the width the canvas shows at
 * the label floor - and grows downwards instead, so "fit" can always frame every node with legible labels. (The earlier force
 * layout did not settle on a star: with Consumer pointed to by a dozen types it spread over thousands of units, and the canvas
 * showed Consumer alone with arrows coming from outside.)
 *  - no edges: a grid in name order whose columns follow the canvas aspect, at most as many as the width allows;
 *  - with edges, each connected part on a grid of cells: its most connected type first, near the middle, then breadth first,
 *    every type in the free cell nearest its placed neighbours, avoiding cells where an edge would run through another box or a
 *    box would sit on an edge; parts one under another; types without any edge in a grid below.
 */
export type GraphNode={id:string;label:string;sub?:string};
export type GraphEdge={id:string;from:string;to:string;label?:string};
export type Placed={id:string;x:number;y:number};
export type Layout={nodes:Placed[];width:number;height:number;
  /** The connected layout was skipped for size (more than MAX_GRAPH_TYPES linked types): a plain grid in name order. */capped?:boolean};
/** Above this many linked types the placement (about O(n^2 x cells)) is skipped for a plain grid: measured on this machine, 60 types
 * take 0.4-0.5 s and 80 take 0.9-1.2 s on the main thread, so the cap keeps a layout under about a second. A Web Worker would
 * move the cost off the main thread but not shorten the wait, and a schema of hundreds of types reads better as the table anyway. */
export const MAX_GRAPH_TYPES=64;

export const NODE_W=168,NODE_H=48,MARGIN=80;  // the margin keeps a self-loop (reach 56) and its label inside the box
export const GAP_X=48,GAP_Y=40;
/** The widest picture by default: a 960 px canvas at the label floor (12 / 14). */
export const DEFAULT_MAX_WIDTH=1120;
/** Cell pitch of the connected layout: room between boxes for edge labels. */
const PITCH_Y=NODE_H+72,MAX_PITCH_X=NODE_W+120,CROSS=100;

/** Columns of a grid of n node boxes whose overall shape is closest to the canvas aspect (width / height). */
export function gridColumns(n:number,aspect:number):number{
  let best=1,score=Infinity;
  for(let c=1;c<=n;c++){const r=Math.ceil(n/c);const w=c*(NODE_W+GAP_X),h=r*(NODE_H+GAP_Y);const d=Math.abs(Math.log((w/h)/aspect));if(d<score-1e-9){score=d;best=c;}}
  return best;
}
/** How many columns of boxes `pitch` apart fit in `maxWidth` with the margins (at least one). */
function columnsIn(maxWidth:number,pitch:number):number{return Math.max(1,Math.floor((maxWidth-2*MARGIN-NODE_W)/pitch)+1);}

function grid(ids:string[],cols:number,top=0):Map<string,{x:number;y:number}>{
  return new Map(ids.map((id,i)=>[id,{x:(i%cols)*(NODE_W+GAP_X),y:top+Math.floor(i/cols)*(NODE_H+GAP_Y)}]));
}

type Pt={x:number;y:number};
/** Whether the segment a-b passes through the box centred at c (shrunk by a pixel, so touching is not crossing). */
export function segmentHitsBox(a:Pt,b:Pt,c:Pt,w=NODE_W,h=NODE_H):boolean{
  const x0=c.x-w/2+1,x1=c.x+w/2-1,y0=c.y-h/2+1,y1=c.y+h/2-1;
  let t0=0,t1=1;const dx=b.x-a.x,dy=b.y-a.y;
  for(const [p,q] of [[-dx,a.x-x0],[dx,x1-a.x],[-dy,a.y-y0],[dy,y1-a.y]] as const){
    if(p===0){if(q<0)return false;continue;}
    const r=q/p;if(p<0){if(r>t1)return false;if(r>t0)t0=r;}else{if(r<t0)return false;if(r<t1)t1=r;}
  }
  return t0<=t1;
}

/** One connected part on a grid of `cols` columns (rows unbounded): cell coordinates per type. */
function placePart(ids:string[],adj:Map<string,Set<string>>,cols:number,pitchX:number):Map<string,Pt>{
  const degree=(id:string)=>adj.get(id)!.size;
  const byWeight=(a:string,b:string)=>degree(b)-degree(a)||(a<b?-1:a>b?1:0);
  const hub=[...ids].sort(byWeight)[0];
  // Breadth first from the hub; neighbours with more links first, so the busy types get the near cells.
  const order=[hub],seen=new Set([hub]);
  for(let i=0;i<order.length;i++)for(const n of [...adj.get(order[i])!].sort(byWeight))if(!seen.has(n)){seen.add(n);order.push(n);}
  const rows=Math.ceil(ids.length/cols)+2;
  const centre=Math.floor((cols-1)/2);
  const cells:Pt[]=[];
  for(let r=-rows;r<=rows;r++)for(let c=0;c<cols;c++)cells.push({x:(c-centre)*pitchX,y:r*PITCH_Y});
  // Nearest the middle first, so equal costs resolve towards a compact, centred picture.
  cells.sort((a,b)=>Math.hypot(a.x/pitchX,a.y/PITCH_Y)-Math.hypot(b.x/pitchX,b.y/PITCH_Y)||a.y-b.y||a.x-b.x);
  const pos=new Map<string,Pt>(),taken=new Set<string>();
  const key=(p:Pt)=>p.x+','+p.y;
  const drawn:[Pt,Pt][]=[];
  for(const id of order){
    let best:Pt|null=null,cost=Infinity;
    const placedNeighbours=[...adj.get(id)!].filter(n=>pos.has(n)).map(n=>pos.get(n)!);
    for(const cell of cells){
      if(taken.has(key(cell)))continue;
      let c=0;
      for(const n of placedNeighbours){
        c+=Math.hypot((cell.x-n.x)/pitchX,(cell.y-n.y)/PITCH_Y);
        for(const [other,p] of pos)if(p!==n&&other!==id&&segmentHitsBox(cell,n,p))c+=CROSS;  // the new edge runs through a box
      }
      for(const [a,b] of drawn)if(segmentHitsBox(a,b,cell))c+=CROSS;  // the new box would sit on an edge
      if(c<cost-1e-9){cost=c;best=cell;}
    }
    pos.set(id,best!);taken.add(key(best!));
    for(const n of placedNeighbours)drawn.push([best!,n]);
  }
  // Then a few passes of single moves to a free cell or swaps of two types, kept only when the picture gets better (an edge through
  // a box weighs as much as CROSS cells of length): the greedy order can leave a late long edge across the hub. Each try is costed
  // by what it touches - the edges of the moved types and the edges that run through their boxes.
  const pairs=[...new Set(ids.flatMap(a=>[...adj.get(a)!].filter(b=>a<b).map(b=>a+'\u0000'+b)))].map(k=>k.split('\u0000') as [string,string]);
  const edgeCost=(a:string,b:string)=>{const p=pos.get(a)!,q=pos.get(b)!;let c=Math.hypot((p.x-q.x)/pitchX,(p.y-q.y)/PITCH_Y);
    for(const [o,r] of pos)if(o!==a&&o!==b&&segmentHitsBox(p,q,r))c+=CROSS;return c;};
  const local=(moved:string[])=>{let c=0;
    for(const [a,b] of pairs){if(moved.includes(a)||moved.includes(b))c+=edgeCost(a,b);
      else for(const m of moved)if(segmentHitsBox(pos.get(a)!,pos.get(b)!,pos.get(m)!))c+=CROSS;}
    return c;};
  const owner=new Map(order.map(id=>[key(pos.get(id)!),id]));
  for(let pass=0;pass<6;pass++){let better=false;
    for(const id of order){
      // Only cells near where it is or near a neighbour (three cells around): far cells only lengthen its edges.
      const anchors=[pos.get(id)!,...[...adj.get(id)!].map(n=>pos.get(n)!)];
      for(const cell of cells){
        if(!anchors.some(p=>Math.abs(p.x-cell.x)<=3*pitchX+1&&Math.abs(p.y-cell.y)<=3*PITCH_Y+1))continue;
        const k=key(cell),from=pos.get(id)!,fk=key(from);if(k===fk)continue;
        const other=owner.get(k)??null;const moved=other?[id,other]:[id];
        const before=local(moved);
        pos.set(id,cell);if(other)pos.set(other,from);
        if(local(moved)<before-1e-9){better=true;owner.set(k,id);if(other)owner.set(fk,other);else owner.delete(fk);}
        else{pos.set(id,from);if(other)pos.set(other,cell);}
      }
    }
    if(!better)break;}
  // Close empty rows and columns (moves can leave them), keeping the order of the rest.
  for(const axis of ['y','x'] as const){
    const used=[...new Set([...pos.values()].map(p=>p[axis]))].sort((a,b)=>a-b);const step=axis==='y'?PITCH_Y:pitchX;
    const to=new Map(used.map((v,i)=>[v,used[0]+i*step]));
    for(const [id,p] of pos)pos.set(id,{...p,[axis]:to.get(p[axis])!});
  }
  return pos;
}

/** aspect: the canvas width / height the picture should fill (the no-edge grid follows it; the default is a wide canvas).
 * maxWidth: the widest the picture may be (the canvas width at the label floor); it grows downwards instead. */
export function layout(nodes:GraphNode[],edges:GraphEdge[],aspect=16/9,maxWidth=DEFAULT_MAX_WIDTH):Layout{
  const all=[...new Set(nodes.map(n=>n.id))].sort();
  if(!all.length)return {nodes:[],width:2*MARGIN,height:2*MARGIN};
  const known=new Set(all);
  const gridCols=columnsIn(maxWidth,NODE_W+GAP_X);
  const links=edges.filter(e=>e.from!==e.to&&known.has(e.from)&&known.has(e.to));
  if(!links.length)return place(grid(all,Math.min(gridColumns(all.length,aspect),gridCols)),all);
  const adj=new Map(all.map(id=>[id,new Set<string>()]));
  for(const e of links){adj.get(e.from)!.add(e.to);adj.get(e.to)!.add(e.from);}
  const linked=all.filter(id=>adj.get(id)!.size),alone=all.filter(id=>!adj.get(id)!.size);
  if(linked.length>MAX_GRAPH_TYPES)return {...place(grid(all,Math.min(gridColumns(all.length,aspect),gridCols)),all),capped:true};
  // Connected parts, largest first (then by first name), one under another.
  const parts:string[][]=[];const inPart=new Set<string>();
  for(const id of linked){if(inPart.has(id))continue;const part=[id];inPart.add(id);
    for(let i=0;i<part.length;i++)for(const n of adj.get(part[i])!)if(!inPart.has(n)){inPart.add(n);part.push(n);}
    parts.push(part.sort());}
  parts.sort((a,b)=>b.length-a.length||(a[0]<b[0]?-1:1));
  const cols=columnsIn(maxWidth,NODE_W+GAP_X);
  const pitchX=cols>1?Math.min(MAX_PITCH_X,(maxWidth-2*MARGIN-NODE_W)/(cols-1)):NODE_W+GAP_X;
  const pos=new Map<string,Pt>();let top=0;
  for(const part of parts){
    // Two types: side by side (one above the other when only one column fits).
    const local=part.length===2?new Map(part.map((id,i)=>[id,cols>1?{x:i*(NODE_W+80),y:0}:{x:0,y:i*PITCH_Y}]))
      :placePart(part,adj,Math.min(cols,part.length),pitchX);
    const ys=[...local.values()].map(p=>p.y),xs=[...local.values()].map(p=>p.x);
    const minY=Math.min(...ys),minX=Math.min(...xs);
    for(const [id,p] of local)pos.set(id,{x:p.x-minX,y:p.y-minY+top});
    top+=Math.max(...ys)-minY+PITCH_Y+GAP_Y;
  }
  if(alone.length){
    // Types without an edge: a grid under the connected parts, as many columns as the width allows.
    for(const [id,p] of grid(alone,Math.min(alone.length,gridCols),top))pos.set(id,p);
  }
  return place(pos,all);
}

/** Translates the positions into a positive box with the margin. */
function place(pos:Map<string,{x:number;y:number}>,ids:string[]):Layout{
  const xs=ids.map(id=>pos.get(id)!.x),ys=ids.map(id=>pos.get(id)!.y);
  const minX=Math.min(...xs),minY=Math.min(...ys);
  const placed=ids.map(id=>({id,x:Math.round(pos.get(id)!.x-minX+MARGIN+NODE_W/2),y:Math.round(pos.get(id)!.y-minY+MARGIN+NODE_H/2)}));
  return {nodes:placed,width:Math.round(Math.max(...xs)-minX+NODE_W+2*MARGIN),height:Math.round(Math.max(...ys)-minY+NODE_H+2*MARGIN)};
}

/** The point where the segment from the centre of a node box towards (tx,ty) leaves the box (edges start and end at borders). */
export function border(x:number,y:number,tx:number,ty:number):{x:number;y:number}{
  const dx=tx-x,dy=ty-y;if(dx===0&&dy===0)return {x,y};
  const s=Math.min(dx===0?Infinity:(NODE_W/2)/Math.abs(dx),dy===0?Infinity:(NODE_H/2)/Math.abs(dy));
  return {x:x+dx*s,y:y+dy*s};
}

/** One of 8 hues per type name (stable across sessions): a small string hash. */
export function hue(name:string):number{let h=0;for(const c of name)h=(h*31+c.charCodeAt(0))>>>0;return h%8;}

/** A relation from a type to itself: a loop out of one side of the box and back into it, both ends on the border (outside the
 * rectangle by a pixel), drawn with the same arrowhead as any other edge (PR #11 review). Centre (x,y). The side is the right one
 * unless another box or label is there (edgePaths chooses); the label sits beyond the loop, anchored away from the box. */
export type LoopSide='right'|'left'|'top'|'bottom';
export function selfLoop(x:number,y:number,side:LoopSide='right'):{d:string;start:Pt;end:Pt;label:Pt;anchor:'start'|'end'|'middle'}{
  if(side==='right'||side==='left'){
    const k=side==='right'?1:-1,edge=x+k*(NODE_W/2+1),start={x:edge,y:y-NODE_H/4},end={x:edge,y:y+NODE_H/4},reach=56*k;
    return {d:`M${start.x},${start.y} C${edge+reach},${start.y-28} ${edge+reach},${end.y+28} ${end.x},${end.y}`,start,end,
      label:{x:edge+reach-8*k,y},anchor:side==='right'?'start':'end'};
  }
  const k=side==='bottom'?1:-1,edge=y+k*(NODE_H/2+1),start={x:x-NODE_W/8,y:edge},end={x:x+NODE_W/8,y:edge},reach=36*k;
  return {d:`M${start.x},${start.y} C${start.x-28},${edge+reach} ${end.x+28},${edge+reach} ${end.x},${end.y}`,start,end,
    label:{x,y:edge+reach+k*10},anchor:'middle'};
}

/** The drawn width of a label at `px` (CJK characters a full em, others about 0.6 em). */
export function textWidth(text:string,px:number):number{let w=0;for(const c of text)w+=c.charCodeAt(0)>0x2e7f?px:px*0.6;return w;}
/** clear: the drawn edge runs through no other node box. crowded: no point along it leaves the label clear of the boxes and the
 * labels placed before it - the label is then drawn only while one of its types is selected. */
export type EdgePath={id:string;d:string;label:{x:number;y:number}|null;self:boolean;clear:boolean;crowded:boolean;anchor:'start'|'end'|'middle'};
const LABEL_H=16;
const overlap=(a:{x0:number;y0:number;x1:number;y1:number},b:{x0:number;y0:number;x1:number;y1:number})=>
  Math.max(0,Math.min(a.x1,b.x1)-Math.max(a.x0,b.x0))*Math.max(0,Math.min(a.y1,b.y1)-Math.max(a.y0,b.y0));
/** The box of an edge label drawn at `px` from the point (centred, or starting / ending there). */
export function labelBox(at:{x:number;y:number},text:string,px:number,anchor:'start'|'end'|'middle'='middle'){
  const w=textWidth(text,px)+6,x0=anchor==='start'?at.x:anchor==='end'?at.x-w:at.x-w/2;return {x0,y0:at.y-LABEL_H/2,x1:x0+w,y1:at.y+LABEL_H/2};}

/** Where every edge is drawn (owner report 2026-10-28: edges and labels must not pile up):
 *  - edges between the same two types are curved apart (a straight line when alone), both ends on the box borders;
 *  - each label goes to the first point along its edge, from the middle outwards (then just above or below it), whose box clears
 *    every node box and every label already placed; when none does, the label is `crowded` and drawn only on focus;
 *  - a self-loop leaves on the first side (right, left, top, bottom) where it and its label are clear and inside `bounds`. */
export function edgePaths(placed:Placed[],edges:GraphEdge[],px:number,bounds?:{width:number;height:number}):EdgePath[]{
  const inside=(b:{x0:number;y0:number;x1:number;y1:number})=>!bounds||(b.x0>=0&&b.y0>=0&&b.x1<=bounds.width&&b.y1<=bounds.height);
  const at=new Map(placed.map(n=>[n.id,n]));
  const drawn=edges.filter(e=>at.has(e.from)&&at.has(e.to));
  const pairKey=(e:GraphEdge)=>e.from<e.to?e.from+'\u0000'+e.to:e.to+'\u0000'+e.from;
  const groups=new Map<string,GraphEdge[]>();
  for(const e of drawn)if(e.from!==e.to)groups.set(pairKey(e),[...(groups.get(pairKey(e))??[]),e]);
  const boxes=placed.map(n=>({x0:n.x-NODE_W/2,y0:n.y-NODE_H/2,x1:n.x+NODE_W/2,y1:n.y+NODE_H/2}));
  const labels:{x0:number;y0:number;x1:number;y1:number}[]=[];
  const out:EdgePath[]=[];
  for(const e of drawn){
    const a=at.get(e.from)!,b=at.get(e.to)!;
    if(e.from===e.to){
      // The first side whose loop and label clear the other boxes and the labels already placed.
      const fits=(side:LoopSide)=>{const loop=selfLoop(a.x,a.y,side);
        const xs=[loop.start.x,loop.end.x,loop.start.x+(side==='right'?56:side==='left'?-56:-28),loop.end.x+(side==='top'||side==='bottom'?28:0)];
        const ys=[loop.start.y,loop.end.y,a.y+(side==='top'?-NODE_H/2-36:side==='bottom'?NODE_H/2+36:0)];
        const area={x0:Math.min(...xs),y0:Math.min(...ys),x1:Math.max(...xs),y1:Math.max(...ys)};
        const tag=e.label?labelBox(loop.label,e.label,px,loop.anchor):null;
        return inside(area)&&(!tag||inside(tag))&&boxes.every((n,i)=>placed[i]===a||(overlap(area,n)===0&&(!tag||overlap(tag,n)===0)))&&(!tag||labels.every(l=>overlap(tag,l)===0));};
      const side=(['right','left','top','bottom'] as const).find(fits)??'right';
      const loop=selfLoop(a.x,a.y,side);
      const free=fits(side);
      if(e.label&&free)labels.push(labelBox(loop.label,e.label,px,loop.anchor));
      out.push({id:e.id,d:loop.d,label:e.label?loop.label:null,self:true,clear:true,crowded:!!e.label&&!free,anchor:loop.anchor});continue;}
    const group=groups.get(pairKey(e))!,k=group.indexOf(e),m=group.length;
    // The normal of the pair in its name order, so A->B and B->A bend to opposite sides.
    const [p,q]=e.from<e.to?[a,b]:[b,a];const len=Math.max(Math.hypot(q.x-p.x,q.y-p.y),1);
    const nx=-(q.y-p.y)/len,ny=(q.x-p.x)/len;
    let off=(k-(m-1)/2)*32;
    // A lone straight edge that would run through another box bends round it (the narrow one-column picture, a rare leftover).
    const others=placed.filter(n=>n!==a&&n!==b);
    const through=(o:number)=>{const c={x:(a.x+b.x)/2+nx*o*2,y:(a.y+b.y)/2+ny*o*2};
      for(let i=0;i<16;i++){const u=i/16,v=(i+1)/16;
        const at=(w:number)=>({x:(1-w)*(1-w)*a.x+2*w*(1-w)*c.x+w*w*b.x,y:(1-w)*(1-w)*a.y+2*w*(1-w)*c.y+w*w*b.y});
        if(others.some(n=>segmentHitsBox(at(u),at(v),n)))return true;}
      return false;};
    if(m===1&&through(0))off=[40,-40,80,-80,NODE_W/2+24,-(NODE_W/2+24)].find(o=>!through(o))??0;
    const mid={x:(a.x+b.x)/2+nx*off*2,y:(a.y+b.y)/2+ny*off*2};  // the control point: the curve passes `off` from the chord
    const s=off?border(a.x,a.y,mid.x,mid.y):border(a.x,a.y,b.x,b.y),t=off?border(b.x,b.y,mid.x,mid.y):border(b.x,b.y,a.x,a.y);
    const d=off?`M${s.x},${s.y} Q${mid.x},${mid.y} ${t.x},${t.y}`:`M${s.x},${s.y} L${t.x},${t.y}`;
    const point=(u:number)=>off?{x:(1-u)*(1-u)*s.x+2*u*(1-u)*mid.x+u*u*t.x,y:(1-u)*(1-u)*s.y+2*u*(1-u)*mid.y+u*u*t.y}
      :{x:s.x+(t.x-s.x)*u,y:s.y+(t.y-s.y)*u};
    let label:{x:number;y:number}|null=null,crowded=false;
    if(e.label){
      let best=Infinity;
      // Along the edge from the middle outwards, on it first, then just above or below it (a short edge between two boxes).
      const lift=NODE_H/2+LABEL_H/2+2;
      for(const [u,dy] of [0,-lift,lift].flatMap(dy=>[0.5,0.4,0.6,0.3,0.7,0.2,0.8].map(u=>[u,dy] as const))){
        const c0=point(u),c={x:c0.x,y:c0.y+dy},box=labelBox(c,e.label,px);
        const cover=(inside(box)?0:1e6)+boxes.reduce((sum,n)=>sum+overlap(box,n),0)+labels.reduce((sum,l)=>sum+overlap(box,l),0);
        if(cover<best-1e-9){best=cover;label=c;}
        if(cover===0)break;
      }
      // A crowded label does not take space from the labels after it (it is only drawn while its edge is in focus).
      crowded=best>0;if(!crowded)labels.push(labelBox(label!,e.label,px));
    }
    out.push({id:e.id,d,label,self:false,clear:!through(off),crowded,anchor:'middle'});
  }
  return out;
}
