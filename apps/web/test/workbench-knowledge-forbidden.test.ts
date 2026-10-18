import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test,vi} from 'vitest';
import {STATE_TEXT,WorkbenchError} from '../src/workbench/api';

// Every page query settles as the error under test (static render: no fetch); the real pages and Page component render it.
let failure:Error=new WorkbenchError('forbidden');
vi.mock('@tanstack/react-query',async original=>({...(await original<typeof import('@tanstack/react-query')>()),
  useQuery:()=>({isPending:false,isError:true,error:failure,data:undefined,refetch:()=>Promise.resolve()})}));
const {Knowledge,ReviewEvidencePage}=await import('../src/workbench/pages/Knowledge');
const {Goals}=await import('../src/workbench/pages/Goals');

test('a role without access (the operator) is told which Actions the knowledge pages need, not a bare 403, and the owner is not named',()=>{
  // NX-028 D2 and its 2026-10-18 revision: a role with neither ontology.schema.review nor nexloop.knowledge.read (the operator) gets 403 by design.
  failure=new WorkbenchError('forbidden');
  for(const html of [renderToStaticMarkup(h(Knowledge,{onOpen:()=>{}})),renderToStaticMarkup(h(ReviewEvidencePage,{candidateId:'c1'}))]){
    expect(html).toContain('审核员（reviewer）');expect(html).toContain('ontology.schema.review');expect(html).toContain('role="alert"');
    expect(html).not.toContain(STATE_TEXT.forbidden);expect(html).toContain('nexloop.knowledge.read');expect(html).not.toContain('负责人');
  }
});

test('other failures and other pages keep the generic texts',()=>{
  failure=new WorkbenchError('unavailable');
  expect(renderToStaticMarkup(h(Knowledge,{onOpen:()=>{}}))).toContain(STATE_TEXT.unavailable);
  failure=new WorkbenchError('forbidden');
  const goals=renderToStaticMarkup(h(Goals,{}));
  expect(goals).toContain(STATE_TEXT.forbidden);expect(goals).not.toContain('审核员');
});
