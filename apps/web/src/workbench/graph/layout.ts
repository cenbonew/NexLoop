/**
 * Enterprise world model W1 (design §4, D2: hand-written SVG, no library): a deterministic layout of the schema graph. The same
 * types and relations always give the same picture (stable screenshots, stable tests). Pure: no DOM, no randomness, no clock.
 *  - up to 8 nodes: one ring (2 or fewer: one row);
 *  - more: start from a ring in name order, then a fixed number of force iterations (repulsion between all nodes, springs along
 *    the edges, a weak pull to the centre), node rectangles kept apart; the result is translated into a positive box.
 */
export type GraphNode={id:string;label:string;sub?:string};
export type GraphEdge={id:string;from:string;to:string;label?:string};
export type Placed={id:string;x:number;y:number};
export type Layout={nodes:Placed[];width:number;height:number};

export const NODE_W=168,NODE_H=48,MARGIN=48;
const ITERATIONS=200;

function ring(ids:string[],radius:number):Map<string,{x:number;y:number}>{
  const out=new Map<string,{x:number;y:number}>();
  ids.forEach((id,i)=>{const a=-Math.PI/2+2*Math.PI*i/ids.length;out.set(id,{x:radius*Math.cos(a),y:radius*Math.sin(a)});});
  return out;
}

export function layout(nodes:GraphNode[],edges:GraphEdge[]):Layout{
  const ids=[...new Set(nodes.map(n=>n.id))].sort();
  if(!ids.length)return {nodes:[],width:2*MARGIN,height:2*MARGIN};
  let pos:Map<string,{x:number;y:number}>;
  if(ids.length<=2)pos=new Map(ids.map((id,i)=>[id,{x:i*(NODE_W+80),y:0}]));
  else{
    const radius=Math.max(140,ids.length*(NODE_W+24)/(2*Math.PI));
    pos=ring(ids,radius);
    if(ids.length>8){
      const links=edges.filter(e=>e.from!==e.to&&pos.has(e.from)&&pos.has(e.to));
      const ideal=NODE_W+60;
      for(let step=0;step<ITERATIONS;step++){
        const cool=1-step/ITERATIONS;
        const force=new Map(ids.map(id=>[id,{x:0,y:0}]));
        for(let i=0;i<ids.length;i++)for(let j=i+1;j<ids.length;j++){
          const a=pos.get(ids[i])!,b=pos.get(ids[j])!;let dx=a.x-b.x,dy=a.y-b.y;
          if(dx===0&&dy===0){dx=1;dy=0;}  // deterministic split of coincident points
          const d2=Math.max(dx*dx+dy*dy,1),f=ideal*ideal/d2;
          force.get(ids[i])!.x+=dx*f;force.get(ids[i])!.y+=dy*f;force.get(ids[j])!.x-=dx*f;force.get(ids[j])!.y-=dy*f;
        }
        for(const e of links){
          const a=pos.get(e.from)!,b=pos.get(e.to)!;const dx=b.x-a.x,dy=b.y-a.y,d=Math.max(Math.hypot(dx,dy),1),f=(d-ideal)/d*0.1;
          force.get(e.from)!.x+=dx*f;force.get(e.from)!.y+=dy*f;force.get(e.to)!.x-=dx*f;force.get(e.to)!.y-=dy*f;
        }
        for(const id of ids){
          const p=pos.get(id)!,f=force.get(id)!;f.x-=p.x*0.01;f.y-=p.y*0.01;
          const len=Math.hypot(f.x,f.y),max=30*cool+1;const k=len>max?max/len:1;
          pos.set(id,{x:p.x+f.x*k,y:p.y+f.y*k});
        }
      }
    }
    // Rectangles never overlap: push apart any pair closer than a node box (a few deterministic passes).
    for(let pass=0;pass<50;pass++){let moved=false;
        for(let i=0;i<ids.length;i++)for(let j=i+1;j<ids.length;j++){
          const a=pos.get(ids[i])!,b=pos.get(ids[j])!;const ox=NODE_W+16-Math.abs(a.x-b.x),oy=NODE_H+16-Math.abs(a.y-b.y);
          if(ox>0&&oy>0){moved=true;if(ox<oy){const s=(a.x<=b.x?-1:1)*ox/2;a.x+=s;b.x-=s;}else{const s=(a.y<=b.y?-1:1)*oy/2;a.y+=s;b.y-=s;}}
        }
        if(!moved)break;}
  }
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
