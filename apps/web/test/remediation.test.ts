import {expect,test} from 'vitest';
import {remediations} from '../src/workbench/api';
import {statusLabel,statusTone} from '../src/workbench/status-labels';
// NX-057 R1: the remediation list as the workbench read returns it (no reason text; the Consumer may be withheld), and its labels.

const row={remediation_id:'0b0c6c57-0000-4000-8000-000000000001',source_intent_id:'0b0c6c57-0000-4000-8000-000000000002',source_action:'synthetic.send',
  source_action_version:1,consumer_id:null,failure_kind:'confirmed_not_delivered',failed_at:'2026-10-23T08:00:00Z',rule_id:'synthetic_escalate',rule_version:1,
  kind:'escalate',mode:'manual',state:'proposed',due_by:'2026-10-30T08:00:00Z',attempts:0,last_code:'proposed',created_at:'2026-10-23T08:01:00Z',plan_id:null};

test('the remediation list is parsed strictly and a withheld Consumer stays withheld',()=>{
  const value=remediations({remediations:[row],hidden:1,paused:false,switch_revision:0,rate_capped:[{hour:'2026-10-23T08:00:00Z',over_count:2}],
    held_rules:[{rule_id:'synthetic_reevaluate',reason_code:'origin_failures',observed:3,held_at:'2026-10-23T08:30:00Z'}]});
  expect(value.remediations[0].consumer_id).toBeNull();
  expect(value.hidden).toBe(1);
  expect(value.rate_capped).toEqual([{hour:'2026-10-23T08:00:00Z',over_count:2}]);
  expect(value.held_rules[0].rule_id).toBe('synthetic_reevaluate');
  expect(()=>remediations({remediations:[{...row,state:undefined}],hidden:0,paused:false,switch_revision:0,rate_capped:[],held_rules:[]})).toThrow();
  expect(()=>remediations({remediations:[],hidden:0,paused:'no',switch_revision:0,rate_capped:[],held_rules:[]})).toThrow();
});

test('remediation states, kinds and failure reasons read as their glossary labels',()=>{
  expect(statusLabel('remediation_state','proposed')).toBe('待处理');
  expect(statusLabel('remediation_state','escalated')).toBe('已升级（待人处理）');
  expect(statusLabel('remediation_state','handled')).toBe('已处理');
  expect(statusTone('remediation_state','failed')).toBe('fail');
  expect(statusLabel('remediation_kind','escalate')).toBe('交给负责人');
  expect(statusLabel('failure_reason','origin_expired')).toBe('来源 Run 已过期');
  expect(statusLabel('remediation_hold','origin_failures')).toBe('补救后的 Action 又失败');
});
