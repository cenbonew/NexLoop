// Constructor negatives only, never an authorization stub or real model call.
import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';
const scope={offering_id:'a'.repeat(64),offering_revision:1,requested_guarantees:['profit-guarantee'],requested_discounts:[]};
function refused(change){const root=mkdtempSync(join(tmpdir(),'nexloop-scope-negative-'));const path=join(root,'config.json');
 const config={guard_url:'https://127.0.0.1:12345/internal/v1/runtime/authorize',guard_key_file:join(root,'key'),guard_ca_file:join(root,'ca'),runtime_profile:'deterministic-test',effect_tools:true,deterministic_message_from_input:true,context_input_protocol:'nexloop.context-pack.v2',deterministic_effect_request_scope:scope,...change};
 writeFileSync(path,JSON.stringify(config),{mode:0o600});const reads=[];
 try{assert.throws(()=>new RuntimeHost(root,path,(file)=>{reads.push(file);return readFileSync(file);},()=>{}));assert.deepEqual(reads,[path]);}finally{rmSync(root,{recursive:true});}}
test('real profile never accepts synthetic scope override',()=>refused({runtime_profile:'deepseek-flash',model_configuration_file:'/not-read/private-model.json',maximum_request_cost:'1',deterministic_message_from_input:undefined}));
test('scope injection requires explicit synthetic message-driven profile',()=>refused({deterministic_message_from_input:undefined}));
test('strict typed scope rejects unknown keys',()=>refused({deterministic_effect_request_scope:{...scope,permit:true}}));
test('strict typed scope rejects unsafe revision',()=>refused({deterministic_effect_request_scope:{...scope,offering_revision:Number.MAX_SAFE_INTEGER+1}}));
test('strict typed scope rejects duplicate terms',()=>refused({deterministic_effect_request_scope:{...scope,requested_discounts:['x','x']}}));
test('strict typed scope rejects arbitrary offering id',()=>refused({deterministic_effect_request_scope:{...scope,offering_id:'anything'}}));
