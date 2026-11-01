/** S84 (S83 D2/G2): the model reads a shorter, equivalent JSON Schema of the plan outcome, incentive and reply parameters.
 * pi-durable checks a tool call's arguments against the offered schema (validateToolArguments); the compact one accepts exactly
 * what the TypeBox one accepts. The Host's own checks and the guard's RunOutcome contract stay the validation of record: values
 * the compact schema lets through (an integer above the JS safe range) are refused by the Host before the network. */
import {describe,expect,it} from 'vitest';
import {validateToolArguments} from '../../../vendor/pi/packages/ai/dist/utils/validation.js';
import {INCENTIVE_PARAMETERS,PLAN_OUTCOME_PARAMETERS,REPLY_PARAMETERS,RuntimeEffectClient,compactSchema,runOutcome} from '../src/runtime-effect-tools.ts';
import {command} from './pi-runtime-support.ts';

// pi-durable's check: the arguments the tool's execute receives (after pi's conversion), or undefined when refused.
const checked=(parameters:unknown,args:unknown):unknown=>{
  try{return validateToolArguments({name:'t',description:'',parameters} as never,{type:'toolCall',id:'c',name:'t',arguments:args} as never);}
  catch{return undefined;}
};
const accepts=(parameters:unknown,args:unknown)=>checked(parameters,args)!==undefined;
const outcome={schema_version:'1.0',kind:'no_action',reasons:['nothing changed'],reassess_at:null,evidence_refs:[],intent_ref:null,plan_update:null};
const step={step_key:'s1',title:'x'};
const OUTCOMES:unknown[]=[
  outcome,{...outcome,kind:'waiting_external',reassess_at:'2026-11-02T00:00:00Z'},{...outcome,kind:'action_intent',intent_ref:'11111111-1111-4111-8111-111111111111'},
  {...outcome,kind:'plan_update',plan_update:{strategy:null,steps:[step]}},{...outcome,kind:'plan_update',plan_update:{strategy:'s',steps:[step,{step_key:'s2'}]}},
  {...outcome,kind:'invented'},{...outcome,schema_version:'2.0'},{...outcome,reassess_at:'tomorrow'},{...outcome,reassess_at:1},{...outcome,intent_ref:'x'},
  {...outcome,plan_update:{strategy:null,steps:[]}},{...outcome,plan_update:{strategy:'',steps:[step]}},{...outcome,plan_update:{steps:[step]}},
  {...outcome,plan_update:{strategy:null,steps:[step],extra:1}},{...outcome,extra:1},{...outcome,reasons:Array(9).fill('r')},{...outcome,kind:null},
  (({kind:_k,...rest})=>rest)(outcome),{...outcome,plan_update:{strategy:null,steps:['not an object']}},
];
const incentive={incentive_offering_id:'d'.repeat(64),offering_revision:1,binding_id:'e'.repeat(64),amount_minor:'200',currency:'CNY',notification_text:''};
const INCENTIVES:unknown[]=[incentive,{...incentive,offering_revision:0},{...incentive,offering_revision:1.5},{...incentive,offering_revision:'1'},
  {...incentive,amount_minor:'01'},{...incentive,currency:'cny'},{...incentive,extra:1},(({currency:_c,...rest})=>rest)(incentive)];
const scope={offering_id:'a'.repeat(64),offering_revision:2,requested_guarantees:['g'],requested_discounts:[]};
const REPLIES:unknown[]=[{message:'hi'},{message:'hi',request_scope:scope},{message:''},{message:'hi',request_scope:{...scope,offering_revision:0}},
  {message:'hi',request_scope:{...scope,requested_guarantees:['g','g']}},{message:'hi',request_scope:{...scope,extra:1}},{message:'hi',extra:1},{}];

describe('compact model-facing schemas',()=>{
  it('accept what the TypeBox parameters accept, with the same arguments, except values pi used to convert silently',()=>{
    // pi converts arguments against a TypeBox schema before the check (Value.Convert: 1.5 -> 1 for an integer, '' -> null for a
    // nullable non-empty string) and passes the converted value on; against a plain JSON Schema it does not, so the compact schema
    // refuses these two (the model is told invalid_arguments and can correct) instead of the Run acting on a value it never sent.
    const STRICTER=new Set([JSON.stringify({...outcome,plan_update:{strategy:'',steps:[step]}}),JSON.stringify({...incentive,offering_revision:1.5})]);
    for(const [raw,samples] of [[PLAN_OUTCOME_PARAMETERS,OUTCOMES],[INCENTIVE_PARAMETERS,INCENTIVES],[REPLY_PARAMETERS,REPLIES]] as const){
      const small=compactSchema(raw);
      expect(JSON.stringify(small).length).toBeLessThan(JSON.stringify(raw).length);
      for(const sample of samples){
        const key=JSON.stringify(sample);
        if(STRICTER.has(key)){expect([accepts(raw,sample),accepts(small,sample),key]).toEqual([true,false,key]);continue;}
        expect([checked(small,sample),key]).toEqual([checked(raw,sample),key]);
      }
      expect(samples.some(sample=>accepts(raw,sample))&&samples.some(sample=>!accepts(raw,sample))).toBe(true);
    }
  });
  it('write enum and nullable types, no any-key pattern and no JS safe-integer maximum',()=>{
    const text=JSON.stringify([compactSchema(PLAN_OUTCOME_PARAMETERS),compactSchema(INCENTIVE_PARAMETERS),compactSchema(REPLY_PARAMETERS)]);
    expect(text).not.toContain('anyOf');expect(text.match(/"const"/g)).toEqual(['"const"']);  // only schema_version '1.0'expect(text).not.toContain('patternProperties');expect(text).not.toContain(String(Number.MAX_SAFE_INTEGER));
    expect(text).toContain('"enum":["no_action","needs_information","waiting_external","escalate","plan_update","action_intent"]');
    expect(text).toContain('"type":["string","null"]');
  });
  it('the offered tools carry the compact schemas',()=>{
    const client=new RuntimeEffectClient({guardURL:new URL('https://127.0.0.1:9999/internal/v1/runtime/authorize'),keyPath:'k',caPath:'c',
      privateMaterial:()=>Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24)});
    const tools=Object.fromEntries(client.toolsForRun(command(),{planOutcome:true,incentive:true}).map(t=>[t.name,(t as unknown as {parameters:unknown}).parameters]));
    expect(tools['nexloop.plan.outcome']).toEqual(compactSchema(PLAN_OUTCOME_PARAMETERS));
    expect(tools['nexloop.incentive.issue']).toEqual(compactSchema(INCENTIVE_PARAMETERS));
    expect(tools['nexloop.service.request']).toEqual(compactSchema(REPLY_PARAMETERS));
  });
  it('the Host still refuses an integer above the safe range, which the compact schema no longer bounds',async()=>{
    const huge=2**53;
    expect(accepts(compactSchema(INCENTIVE_PARAMETERS),{...incentive,offering_revision:huge})).toBe(true);  // the model-facing schema lets it through
    const client=new RuntimeEffectClient({guardURL:new URL('https://127.0.0.1:9999/internal/v1/runtime/authorize'),keyPath:'k',caPath:'c',
      privateMaterial:()=>Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24)});
    for(const value of [huge,1e30])
      await expect(client.issueIncentive(command(),{...incentive,offering_revision:value})).rejects.toMatchObject({code:'invalid_incentive_request'});
    for(const value of [huge,1e30])
      await expect(client.call(command(),'submit',{message:'hi',request_scope:{...scope,offering_revision:value}})).rejects.toMatchObject({code:'invalid_effect_request'});
    // The run outcome's own check is the hand-written contract mirror, independent of the offered schema.
    expect(()=>runOutcome({...outcome,kind:'invented'})).toThrow();expect(runOutcome(outcome)).toEqual(outcome);
  });
});
