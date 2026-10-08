// Frontend unit evidence only; PG/Cookie authority is tested independently.
import test from 'node:test';import assert from 'node:assert/strict';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import ts from 'typescript';import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
const build=new URL('../.ci-results/scope-test-build/',import.meta.url);mkdirSync(build,{recursive:true});
for(const file of ['api.ts','native-message.ts','chat-api.ts','MessageReceipt.tsx']){
 let input=readFileSync(new URL('../src/'+file,import.meta.url),'utf8');
 input=input.replaceAll("'./api'","'./api.js'").replaceAll("'./chat-api'","'./chat-api.js'").replaceAll("'./native-message'","'./native-message.js'");
 writeFileSync(new URL(file.replace(/\.tsx?$/,'.js'),build),ts.transpileModule(input,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText);
}
const {scopeDenial}=await import(new URL('chat-api.js',build));const {MessageReceipt}=await import(new URL('MessageReceipt.js',build));
const mid='a'.repeat(64);
const scope={service_code:'local.json-export',deliverable:'固定私有目录内可核验的 JSON 文本导出文件',price_amount:'0',currency:'CNY',guarantees:[],discounts:[],limitations:['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'],evidence_kind:'fsynced_json_export'};
const denial={record_id:'b'.repeat(64),code:'outside_catalog_terms',scope,recorded_at:'2026-10-08T00:00:00Z'};
function render(data){const client=new QueryClient();client.setQueryData(['message-service-receipt',mid],data);try{return renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(MessageReceipt,{messageId:mid})));}finally{client.clear();}}
test('strict projection rejects fake fulfillment, extra private fields and expanded terms',()=>{
 assert.deepEqual(scopeDenial({message_id:mid,denial}),{message_id:mid,denial});
 for(const changed of [{...denial,business_action_success:true},{...denial,source_digest:'private'},{...denial,code:'fulfilled'},{...denial,scope:{...scope,discounts:['50%']}},{...denial,scope:{...scope,secret:'x'}},{...denial,recorded_at:'not-a-date'}])assert.throws(()=>scopeDenial({message_id:mid,denial:changed}));
 assert.deepEqual(scopeDenial({message_id:mid,denial:null}),{message_id:mid,denial:null});
});
test('real projected denial replaces queued wording without claiming delivery',()=>{
 const html=render({receipt:{message_id:mid,run:{run_id:'x'},receipt:null},denial});
 assert.match(html,/超出已授权服务范围/);assert.match(html,/未创建新的交付意图/);assert.match(html,/JSON 文本导出/);assert.doesNotMatch(html,/已进入处理队列|服务已履行/);
});
test('successful governed receipt retains fulfillment presentation',()=>{
 const html=render({receipt:{message_id:mid,run:{run_id:'x'},receipt:{state:'fulfilled',business_action_success:true,receipt_id:'original-receipt'}},denial:null});
 assert.match(html,/服务已履行（治理回执）/);assert.match(html,/original-receipt/);assert.doesNotMatch(html,/超出已授权服务范围/);
});
