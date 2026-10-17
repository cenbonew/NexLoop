import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test,vi} from 'vitest';
import {KNOWLEDGE_READ_ACTION,REVIEW_ACTION,me,readsKnowledgeOnly,reviews} from '../src/workbench/api';

// The pages' queries settle with fixed data per key (static render); the member comes from /me.
let member=me({principal_id:'p',role:'owner',actions:[KNOWLEDGE_READ_ACTION]});
const evidence={candidate_id:'c1',status:'pending_review',kind:'property',display_name:'联系偏好',evidence:[]};
vi.mock('@tanstack/react-query',async original=>({...(await original<typeof import('@tanstack/react-query')>()),
  useQuery:({queryKey}:{queryKey:unknown[]})=>queryKey[1]==='me'?{isPending:false,isError:false,data:member}
    :queryKey[1]==='review-decision'?{isPending:true,isError:false,data:undefined}
    :{isPending:false,isError:false,data:queryKey.length===2?[{candidate_id:'c1',kind:'property',display_name:'联系偏好',evidence_count:1,created_at:'t'}]:evidence}}));
const {Knowledge,ReviewEvidencePage,READ_ONLY_TEXT}=await import('../src/workbench/pages/Knowledge');

test('who reviews and who only reads (NX-028 D2 revision 2026-10-18)',()=>{
  const owner=me({principal_id:'o',role:'owner',actions:[KNOWLEDGE_READ_ACTION]});const reviewer=me({principal_id:'r',role:'reviewer',actions:[REVIEW_ACTION]});
  const operator=me({principal_id:'x',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
  expect([reviews(owner),readsKnowledgeOnly(owner)]).toEqual([false,true]);
  expect([reviews(reviewer),readsKnowledgeOnly(reviewer)]).toEqual([true,false]);
  expect([reviews(operator),readsKnowledgeOnly(operator)]).toEqual([false,false]);
});

test('the owner sees the queue and the evidence read only, without the decision section',()=>{
  member=me({principal_id:'o',role:'owner',actions:[KNOWLEDGE_READ_ACTION]});
  const queue=renderToStaticMarkup(h(Knowledge,{onOpen:()=>{}}));expect(queue).toContain(READ_ONLY_TEXT);expect(queue).toContain('联系偏好');
  const page=renderToStaticMarkup(h(ReviewEvidencePage,{candidateId:'c1'}));
  expect(page).toContain(READ_ONLY_TEXT);expect(page).not.toContain('review-decisions-heading');expect(page).not.toContain('正在读取候选详情');
});

test('the reviewer keeps the decision section and no read-only note',()=>{
  member=me({principal_id:'r',role:'reviewer',actions:[REVIEW_ACTION]});
  const page=renderToStaticMarkup(h(ReviewEvidencePage,{candidateId:'c1'}));
  expect(page).toContain('review-decisions-heading');expect(page).not.toContain(READ_ONLY_TEXT);
});
