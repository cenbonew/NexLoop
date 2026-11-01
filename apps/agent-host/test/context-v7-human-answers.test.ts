// K4c (L6 W2): the Host's own line on staff answers, apart from SQL's - an evidence.human_answers item is admitted only in a v7 plan
// (Role) Run pack; in a message Run's pack, in a v6 pack or in another section it is refused.
import {expect,test} from 'vitest';
import {assertHumanAnswers,CONTEXT_PROTOCOL_V6,CONTEXT_PROTOCOL_V7} from '../src/context-input.js';

const question='0f0e0d0c-0b0a-4908-8706-050403020100';
const answer={subsection:'human_answers',ref:'nexloop:human_answer:'+question+'@3',revision:'3',evidence_kind:'user_statement',tags:[],
  access_decision_ref:'decision:'+'a'.repeat(64),relevance_permille:1000,at:'2026-11-01T08:00:00.000000Z',content_hash:'b'.repeat(64),
  content:{question_ref:'nexloop:human_question:'+question,question_kind:'customer_fact',question_text:'客户下周二在家吗？',
    answer:{kind:'text',text:'他说下周三才在家'},answerer_role:'operator',answered_at:'2026-11-01T08:00:00.000000Z',plan_ref:'nexloop:plan:'+question+'@1'}};
const pack=(section:string,item:unknown)=>({constraints:[],consumer_state:[],open_work:[],evidence:[],semantics:[],experience:[],[section]:[item]});
const refused=(protocol:string,roleRun:boolean,p:Record<string,unknown>)=>{try{assertHumanAnswers(protocol,roleRun,p);return false;}catch(error){return (error as {code?:string}).code==='runtime_context_invalid';}};

test('a v7 plan Run pack admits a well-formed staff answer in evidence',()=>{
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',answer))).toBe(false);
});

test('a message Run pack with a staff answer is refused, also under v7',()=>{
  expect(refused(CONTEXT_PROTOCOL_V7,false,pack('evidence',answer))).toBe(true);
});

test('a v6 pack with a staff answer is refused, plan Run or not',()=>{
  expect(refused(CONTEXT_PROTOCOL_V6,true,pack('evidence',answer))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V6,false,pack('evidence',answer))).toBe(true);
});

test('a staff answer elsewhere or relabelled is refused',()=>{
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('consumer_state',{...answer,evidence_kind:'policy'}))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',{...answer,subsection:'conversation'}))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',{...answer,evidence_kind:'conversation'}))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',{...answer,tags:['unconfirmed']}))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',{...answer,content:{...answer.content,answerer_role:'reviewer'}}))).toBe(true);
  expect(refused(CONTEXT_PROTOCOL_V7,true,pack('evidence',{...answer,revision:'2'}))).toBe(true);
});
