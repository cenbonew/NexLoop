// Owner report from stage (2026-10-28): the Object Type graph showed Consumer alone with a dozen arrows coming from outside the
// canvas, also after "适应画布". Two causes: the force layout did not settle on a star (it spread over thousands of units), and the fit
// stopped at the label floor, so it could never frame the picture. Now the layout is never wider than the canvas at the floor and the
// canvas grows to the fitted height: in the default view every node is inside the viewBox, labels stay legible, no edge runs through
// another box, edges between the same two types are curved apart and the edge labels do not pile up.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {MAX_GRAPH_TYPES,NODE_H,NODE_W,edgePaths,labelBox,layout,segmentHitsBox,type GraphEdge,type GraphNode} from '../src/workbench/graph/layout';
import {DEFAULT_W,EDGE_PX,MAX_H,MIN_LABEL_PX,MIN_SCALE,SchemaGraph,canvasHeight,fitScale} from '../src/workbench/graph/SchemaGraph';

// The relation catalog NX-054 published on stage (25 types; assessment_evidence deferred), plus types without any relation.
const CATALOG:[string,string,string][]=[['goal_of','Goal','Consumer'],['plan_step_of','PlanStep','Consumer'],['control_of','EffectControl','Consumer'],
  ['conversation_of','Conversation','Consumer'],['message_in','Message','Conversation'],['commitment_to','Commitment','Consumer'],['commercial_record_of','CommercialRecord','Consumer'],
  ['assignment_of','MessageAssignment','Consumer'],['owned_by','ConsumerOwnership','Consumer'],['role_link_of','ConsumerRoleLink','Consumer'],
  ['service_offering_of','ConsumerServiceOffering','Consumer'],['incentive_offering_of','ConsumerIncentiveOffering','Consumer'],['scope_for_consumer','RoleAssignmentScope','Consumer'],
  ['step_in_goal','PlanStep','Goal'],['step_under_control','PlanStep','EffectControl'],['commitment_for_goal','Commitment','Goal'],['supersedes','Commitment','Commitment'],
  ['assignment_for_message','MessageAssignment','Message'],['assignment_goal','MessageAssignment','Goal'],['assignment_step','MessageAssignment','PlanStep'],
  ['assignment_control','MessageAssignment','EffectControl'],['role_link_role','ConsumerRoleLink','RoleDefinition'],['service_offering_ref','ConsumerServiceOffering','ServiceOffering'],
  ['incentive_offering_ref','ConsumerIncentiveOffering','IncentiveOffering'],['scope_of_role','RoleAssignmentScope','RoleDefinition']];
const node=(id:string):GraphNode=>({id,label:id,sub:'v1 · 3 Property · 0 Object'});
const stage={nodes:[...new Set(CATALOG.flatMap(r=>[r[1],r[2]]).concat(['Tenant','Order','Product','Store','Staff']))].map(node),
  edges:CATALOG.map(([id,from,to]):GraphEdge=>({id,from,to,label:`${id} N:1`}))};
const satellites=Array.from({length:16},(_,i)=>`Satellite${String(i).padStart(2,'0')}`);
const star={nodes:['Consumer',...satellites].map(node),edges:satellites.map((s,i):GraphEdge=>({id:`rel_${i}`,from:s,to:'Consumer',label:`rel_${i} N:1`}))};
// Several relations between the same types, both directions, and a self-relation.
const multi={nodes:['Order','Consumer','Store','Product'].map(node),edges:([['placed_by','Order','Consumer'],['paid_by','Order','Consumer'],['refunded_to','Order','Consumer'],
  ['favourite_store','Consumer','Store'],['served_by','Consumer','Store'],['sells','Store','Product'],['bundle_of','Product','Product'],['ordered_item','Order','Product']] as const)
  .map(([id,from,to]):GraphEdge=>({id,from,to,label:`${id} N:N`}))};

function render(g:{nodes:GraphNode[];edges:GraphEdge[]},focus:string|null=null){
  const html=renderToStaticMarkup(h(SchemaGraph,{nodes:g.nodes,edges:g.edges,focus,onFocus:()=>{},label:'Object Type 关系图'}));
  const [x0,y0,w,hgt]=/viewBox="([^"]+)"/.exec(html)![1].split(' ').map(Number);
  const rects=[...html.matchAll(/<rect x="([-\d.]+)" y="([-\d.]+)" width="168" height="48" rx="12"/g)].map(m=>({x:Number(m[1]),y:Number(m[2])}));
  return {html,view:{x0,y0,x1:x0+w,y1:y0+hgt},rects,scale:Number(/data-scale="([\d.]+)"/.exec(html)![1]),labelPx:Number(/data-label-px="([\d.]+)"/.exec(html)![1]),height:Number(/<svg[^>]* height="(\d+)"/.exec(html)![1])};
}

test.each([['the stage catalog (Consumer the hub of 25 relations)',stage],['a star of 16 types around one',star],['several relations between the same types',multi]])(
  '%s: in the default view every node is inside the viewBox, with labels at or above the floor',(_name,g)=>{
  const r=render(g);
  expect(r.rects).toHaveLength(g.nodes.length);
  for(const p of r.rects){
    expect(p.x).toBeGreaterThanOrEqual(r.view.x0);expect(p.y).toBeGreaterThanOrEqual(r.view.y0);
    expect(p.x+NODE_W).toBeLessThanOrEqual(r.view.x1+1e-6);expect(p.y+NODE_H).toBeLessThanOrEqual(r.view.y1+1e-6);
  }
  expect(r.labelPx).toBeGreaterThanOrEqual(MIN_LABEL_PX);
  // The picture is no wider than the canvas at the floor; the canvas is as tall as the fitted picture (above MAX_H when needed).
  const l=layout(g.nodes,g.edges,DEFAULT_W/MAX_H,Math.floor(DEFAULT_W/MIN_SCALE/40)*40);
  expect(l.width*MIN_SCALE).toBeLessThanOrEqual(DEFAULT_W+1e-6);
  expect(r.scale).toBeCloseTo(fitScale(l.width,l.height,DEFAULT_W),3);
  const fit=fitScale(l.width,l.height,DEFAULT_W);expect(r.height).toBe(canvasHeight(l.height,fit,fit));expect(r.height).toBeGreaterThanOrEqual(Math.min(MAX_H,l.height*r.scale)-1);
});

test('the stage catalog no longer explodes: compact, Consumer near the middle, every relation drawn',()=>{
  const l=layout(stage.nodes,stage.edges,DEFAULT_W/MAX_H,1120);
  expect(l.width).toBeLessThanOrEqual(1120);expect(l.height).toBeLessThan(1200);  // the force layout gave 4239 x 6470
  const consumer=l.nodes.find(n=>n.id==='Consumer')!;
  const linked=new Set(CATALOG.flatMap(r=>[r[1],r[2]]));const ys=l.nodes.filter(n=>linked.has(n.id)).map(n=>n.y);
  expect(consumer.y).toBeGreaterThan(Math.min(...ys));expect(consumer.y).toBeLessThan(Math.max(...ys));
  expect(render(stage).html.match(/data-edge="/g)).toHaveLength(24);expect(render(stage).html).toContain('data-self-loop="supersedes"');
});

const overlap=(a:{x0:number;y0:number;x1:number;y1:number},b:{x0:number;y0:number;x1:number;y1:number})=>
  Math.max(0,Math.min(a.x1,b.x1)-Math.max(a.x0,b.x0))*Math.max(0,Math.min(a.y1,b.y1)-Math.max(a.y0,b.y0));
test.each([['stage',stage,1120],['star',star,1120],['multi',multi,1120],['stage on a 1024 px screen',stage,720]] as const)(
  '%s: no drawn edge through another box (wide canvas); the labels drawn by default clear the boxes and each other',(name,g,width)=>{
  const l=layout(g.nodes,g.edges,DEFAULT_W/MAX_H,width);const at=new Map(l.nodes.map(n=>[n.id,n]));
  const paths=edgePaths(l.nodes,g.edges,EDGE_PX,l);
  if(width>=1120){
    expect(paths.every(p=>p.clear)).toBe(true);
    if(name!=='star')for(const e of g.edges)if(e.from!==e.to)for(const n of l.nodes)
      if(n.id!==e.from&&n.id!==e.to)expect(segmentHitsBox(at.get(e.from)!,at.get(e.to)!,n),`${e.id} through ${n.id}`).toBe(false);
  }
  const label=new Map(g.edges.map(e=>[e.id,e.label!]));
  const shown=paths.filter(p=>!p.crowded);
  if(width>=1120)expect(shown.length/paths.length,`${name}: labels shown by default`).toBeGreaterThanOrEqual(0.8);
  const boxes=shown.map(p=>labelBox(p.label!,label.get(p.id)!,EDGE_PX,p.anchor));
  for(const b of boxes){expect(b.x0).toBeGreaterThanOrEqual(0);expect(b.y0).toBeGreaterThanOrEqual(0);expect(b.x1).toBeLessThanOrEqual(l.width);expect(b.y1).toBeLessThanOrEqual(l.height);}
  for(let i=0;i<boxes.length;i++){
    for(let j=i+1;j<boxes.length;j++)expect(overlap(boxes[i],boxes[j]),`${shown[i].id} / ${shown[j].id}`).toBe(0);
    for(const n of l.nodes)expect(overlap(boxes[i],{x0:n.x-NODE_W/2,y0:n.y-NODE_H/2,x1:n.x+NODE_W/2,y1:n.y+NODE_H/2}),`${shown[i].id} on ${n.id}`).toBe(0);
  }
});

test('a crowded label is drawn while one of its types is selected; every edge keeps its name as a title',()=>{
  const l=layout(stage.nodes,stage.edges,DEFAULT_W/MAX_H,720);
  const crowded=edgePaths(l.nodes,stage.edges,EDGE_PX,l).filter(p=>p.crowded);
  expect(crowded.length).toBeGreaterThan(0);  // a narrow canvas cannot place them all
  const edge=stage.edges.find(e=>e.id===crowded[0].id)!;
  // Server render measures nothing: it lays out for DEFAULT_W, so check the markup against that layout's own crowded set.
  const wl=layout(stage.nodes,stage.edges,DEFAULT_W/MAX_H,Math.floor(DEFAULT_W/MIN_SCALE/40)*40);
  const wide=edgePaths(wl.nodes,stage.edges,EDGE_PX,wl).filter(p=>p.crowded);
  const html=render(stage).html;
  expect(html.match(/<title>[^<]+→[^<]+：/g)).toHaveLength(stage.edges.length);
  for(const p of wide){const e=stage.edges.find(x=>x.id===p.id)!;
    expect(html).toContain(`data-crowded=""`);
    expect(html.split(`>${e.label}</text>`).length-1).toBe(0);
    expect(render(stage,e.from).html).toContain(`>${e.label}</text>`);}
  expect(edge.label).toBeTruthy();
});

test('relations between the same two types are curved apart, both directions on opposite sides; a lone one is straight',()=>{
  const l=layout(multi.nodes,multi.edges,DEFAULT_W/MAX_H,1120);const paths=new Map(edgePaths(l.nodes,multi.edges,EDGE_PX,l).map(p=>[p.id,p]));
  const three=['placed_by','paid_by','refunded_to'].map(id=>paths.get(id)!);
  expect(new Set(three.map(p=>p.d)).size).toBe(3);
  expect(three.filter(p=>p.d.includes(' Q'))).toHaveLength(2);  // the middle one of three stays straight, the others bend
  expect(paths.get('favourite_store')!.d).toContain(' Q');expect(paths.get('served_by')!.d).toContain(' Q');
  expect(paths.get('sells')!.d).toContain(' L');expect(paths.get('bundle_of')!.self).toBe(true);
  // The self-relation's loop leaves on a side clear of the neighbouring box (here not the right, where Order is).
  expect(paths.get('bundle_of')!.anchor).not.toBe('start');
});

test('fit frames the picture: a tall one makes the canvas taller instead of cutting it; zooming in never grows it further',()=>{
  expect(canvasHeight(400,1,1)).toBe(400);expect(canvasHeight(100,1,1)).toBe(240);
  expect(canvasHeight(1400,MIN_SCALE,MIN_SCALE)).toBe(Math.round(1400*MIN_SCALE));  // above MAX_H: the whole fitted picture
  expect(canvasHeight(1400,2,MIN_SCALE)).toBe(Math.round(1400*MIN_SCALE));
  expect(canvasHeight(500,2,1)).toBe(MAX_H);
});

test('the layout stays quick for a large schema',()=>{
  const nodes=Array.from({length:60},(_,i)=>node(`T${String(i).padStart(2,'0')}`));
  const edges=nodes.slice(1).map((x,i):GraphEdge=>({id:'e'+i,from:nodes[Math.floor(i/3)].id,to:x.id,label:'link N:1'}));
  const t=performance.now();const l=layout(nodes,edges,DEFAULT_W/MAX_H,1120);
  expect(performance.now()-t).toBeLessThan(3000);expect(l.width).toBeLessThanOrEqual(1120);
});

test('above the cap the placement is skipped for a name-order grid, quickly, with every node still inside the viewBox',()=>{
  const nodes=Array.from({length:MAX_GRAPH_TYPES+40},(_,i)=>node(`T${String(i).padStart(3,'0')}`));
  const edges=nodes.slice(1).map((x,i):GraphEdge=>({id:'e'+i,from:nodes[i%5].id,to:x.id,label:'link N:1'}));
  const t=performance.now();const l=layout(nodes,edges,DEFAULT_W/MAX_H,1120);
  expect(performance.now()-t).toBeLessThan(200);expect(l.capped).toBe(true);expect(l.width).toBeLessThanOrEqual(1120);
  expect(layout(nodes.slice(0,MAX_GRAPH_TYPES),edges.filter(e=>Number(e.to.slice(1))<MAX_GRAPH_TYPES),DEFAULT_W/MAX_H,1120).capped).toBeUndefined();
  const r=render({nodes,edges});
  expect(r.html).toContain('data-layout="capped"');expect(r.html).toContain(`超过 ${MAX_GRAPH_TYPES} 个`);
  expect(r.rects).toHaveLength(nodes.length);
  for(const p of r.rects){expect(p.x).toBeGreaterThanOrEqual(r.view.x0);expect(p.y).toBeGreaterThanOrEqual(r.view.y0);
    expect(p.x+NODE_W).toBeLessThanOrEqual(r.view.x1+1e-6);expect(p.y+NODE_H).toBeLessThanOrEqual(r.view.y1+1e-6);}
});

test('R56 W2: the type detail never covers the canvas: side by side when the content area is wide, under the canvas when narrow',async()=>{
  const {readFileSync}=await import('node:fs');
  const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
  expect(css).not.toMatch(/\.world-panel\{position:absolute/);  // the floating panel hid the right-hand nodes and the toolbar hint
  expect(css).toContain('.workbench .world-graph-frame{container-type:inline-size;min-width:0}');
  const wide=/@container \(min-width:960px\)\{\.workbench \.world-graph-panel\{([^}]*)\}/.exec(css);
  expect(wide).not.toBeNull();expect(wide![1]).toContain('grid-template-columns:minmax(0,1fr) 340px');
  // Below that the base grid has one column: the panel stacks under the full-width canvas.
  expect(css).toContain('.workbench .world-graph{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-4)}');
});
