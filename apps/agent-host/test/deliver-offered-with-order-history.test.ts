/** 10/18 (trial 4f5d795c): a customer who ordered before asks again. The enterprise tool depends only on this Run's bound
 * enterprise slot (the pack's effect_supplies), never on the conversation history: with an earlier order in the history it is
 * still offered, with the new-order wording. */
import { describe, expect, it } from 'vitest';
import { enterpriseSupply, messageRun } from '../src/runtime-host.ts';
import { RuntimeEffectClient } from '../src/runtime-effect-tools.ts';
import { command } from './pi-runtime-support.ts';

const supply={action:'nexloop.enterprise.deliver:1',offering_id:'d'.repeat(64),offering_revision:1,binding_id:'e'.repeat(64),binding_revision:1,provenance:'eios:object:'+'d'.repeat(64),
  properties:{service_code:'enterprise.synthetic-delivery',title:'Synthetic enterprise offering customer-2',delivery_action:'nexloop.enterprise.deliver:1',content_kind:'enterprise-delivery',
    price_amount:'12.50',currency:'CNY',eligibility:'current_consumer_plan',allowed_guarantees:[],allowed_discounts:[],evidence_kind:'enterprise_receipt',active:true,valid_until:'2026-12-31T00:00:00Z'}};
const pack=(supplies:unknown)=>JSON.stringify({user_statement:{message_id:'m'.repeat(64),body:'确认，请现在直接下单。'},current_event:{kind:'consumer_message'},
  evidence:[{subsection:'conversation',content:{speaker:'consumer',body:'我要下单 Synthetic enterprise offering customer-2'}},
            {subsection:'conversation',content:{speaker:'agent',body:'已为您下单，企业会跟进。'}}],...(supplies?{effect_supplies:supplies}:{})});

describe('enterprise delivery with an earlier order in the history',()=>{
  it('is offered from the bound enterprise slot, whatever the history says',()=>{
    const input=pack([supply]);
    expect(messageRun(input)).toBe(true);
    const found=enterpriseSupply(input);
    expect(found).toMatchObject({action:'nexloop.enterprise.deliver:1',title:'Synthetic enterprise offering customer-2'});
    const client=new RuntimeEffectClient({guardURL:new URL('https://127.0.0.1:9443/internal/v1/runtime/authorize'),keyPath:'k',caPath:'c',privateMaterial:()=>Buffer.from(''),activationForRun:()=>undefined});
    const names=client.toolsForRun(command(),{enterprise:found,replyEndsRun:true}).map(t=>t.name);
    expect(names).toContain('nexloop.enterprise.deliver');
    // Without the bound slot (no effect_supplies) there is no enterprise tool: that is decided by the relay's binding, not the model.
    expect(enterpriseSupply(pack(undefined))).toBeUndefined();
  });
});
