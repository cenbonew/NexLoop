// Capability gap P2: an object's detail laid out by its type's Object View (sections, order, title property, Links first); a
// restricted property stays withheld; no View keeps the default layout. Layout only: a View never shows what the read did not give.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {WorkbenchError,objectView,type ObjectNeighbors,type PropertyValue} from '../src/workbench/api';
import {ViewLayout,orderedByView} from '../src/workbench/pages/WorldInstances';

const raw={view:{stable_name:'consumer.profile',version:2,object_type:'Consumer',title_property:'sport',
  sections:[{name:'sport',title:'运动偏好',fields:['level','sport']},{name:'money',title:'消费能力',fields:['income']}],relations:['plays_with']}};
const props:PropertyValue[]=[{name:'sport',status:'ok',value:'网球',valueType:'text'},{name:'level',status:'ok',value:'4',valueType:'text'},
  {name:'income',status:'withheld',reason:'restricted'},{name:'note',status:'ok',value:'备注',valueType:'text'}];

test('the parser: a View or null; names checked; a View without sections refused',()=>{
  const v=objectView(raw)!;
  expect(v.sections.map(s=>s.fields)).toEqual([['level','sport'],['income']]);expect(v.title_property).toBe('sport');
  expect(objectView({view:null})).toBeNull();
  expect(()=>objectView({view:{...raw.view,sections:[]}})).toThrow(WorkbenchError);
  expect(()=>objectView({view:{...raw.view,sections:[{name:'x',title:'X',fields:['bad name']}]}})).toThrow(WorkbenchError);
});

test('rendering: sections in the View order with their titles, the title value above, withheld stays withheld, the rest folded',()=>{
  const html=renderToStaticMarkup(h(ViewLayout,{view:objectView(raw)!,properties:props}));
  expect(html).toContain('data-view="consumer.profile@2"');
  const order=['data-title-property="sport"','>运动偏好</h3>','<dt>level</dt>','<dt>sport</dt>','>消费能力</h3>','<dt>income</dt>','其他 Property（1）','<dt>note</dt>'].map(x=>html.indexOf(x));
  expect(order.every(i=>i>=0)).toBe(true);expect([...order].sort((a,b)=>a-b)).toEqual(order);
  const money=html.slice(html.indexOf('>消费能力</h3>'),html.indexOf('其他 Property'));
  expect(money).toContain('<dt>income</dt>');expect(money).not.toContain('9000');expect(money).toMatch(/withheld|受限|不可见/);
  expect(html).toContain('只决定位置和顺序，不改变谁能看到哪个 Property');
  // A View field the current schema no longer has is said as such, never invented.
  const stale=renderToStaticMarkup(h(ViewLayout,{view:{...objectView(raw)!,sections:[{name:'a',title:'A',fields:['gone']}]},properties:props}));
  expect(stale).toContain('data-code="not_in_schema"');
});

test('Links: the View relations first in its order, every other group kept after them',()=>{
  const n:ObjectNeighbors={type_name:'Consumer',object_id:'a'.repeat(64),depth:1,group_limit:25,groups:[
    {direction:'outgoing',via:'property:goal_id',type_name:'Goal',items:[],invisible:0,collapsed:0},
    {direction:'outgoing',via:'plays_with',type_name:'Consumer',items:[],invisible:0,collapsed:0}]};
  expect(orderedByView(n,objectView(raw)!).groups.map(g=>g.via)).toEqual(['plays_with','property:goal_id']);
  expect(orderedByView(n,objectView(raw)!).groups.length).toBe(2);
});
