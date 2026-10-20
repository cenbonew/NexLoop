// NX-039 V4 semantic release (D39-7/8/9/14): strict parsing, the section only for a role carrying the release Actions, the class
// shown as the evaluator gave it (never an input), the rollback report as words with the Claim count, and the note limit.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,semanticReleases,type Me} from '../src/workbench/api';
import {NOTE_LIMIT,SemanticReleaseSection,itemsText,reportText} from '../src/workbench/pages/SemanticReleases';

const C='c'.repeat(8)+'-cccc-4ccc-8ccc-'+'c'.repeat(12),CLAIM='a'.repeat(64);
const VIEW={current:'0.1.1',
  releases:[{version:'0.1.1',reason:'rollback',candidate_id:null,change_class:null,report_digest:null,rolled_back_to:'0.0.1',owner_reason:'回退',recorded_at:'2026-10-20T12:00:00Z',
      rollback_report:{from_version:'0.1.0',to_version:'0.0.1',removed_aliases:1,claim_count:1,claim_ids:[CLAIM],dropped_erased_items:0,removed_refs:['eios:object_type:Product'],
        window_start:'2026-10-20T11:00:00Z',claims_changed:0},items:[{kind:'semantic_note',ref:'eios:object_type:Product',body:'只指实物商品'}]},
    {version:'0.1.0',reason:'publish',candidate_id:C,change_class:'minor',report_digest:'d'.repeat(64),rolled_back_to:null,rollback_report:null,owner_reason:'发布',
      recorded_at:'2026-10-20T11:00:00Z',items:[{kind:'alias',ref:'eios:object_type:Product',body:'synthetic-term'},{kind:'semantic_note',ref:'eios:object_type:Product',body:'只指实物商品'}]}],
  publishable:[{candidate_id:C,change_class:'patch',change_kind:'alias',target_refs:['eios:object_type:Product'],term_hashes:1,parent_ok:true}]};
const me=(actions:string[]):Me=>({tenant_id:'t',principal_id:'p',role:'owner',actions} as unknown as Me);
const RELEASE=['eios:action:nexloop.semantic_release.publish:1','eios:action:nexloop.semantic_release.rollback:1'];

function render(member:Me,view=VIEW){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','me'],member);
  c.setQueryData(['workbench','semantic-releases'],semanticReleases(view));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(SemanticReleaseSection)));
}

test('parsing is strict: versions, classes and kinds from the fixed vocabularies',()=>{
  const v=semanticReleases(VIEW);
  expect(v.current).toBe('0.1.1');expect(v.releases[1].change_class).toBe('minor');expect(v.publishable[0].change_class).toBe('patch');
  expect(()=>semanticReleases({...VIEW,publishable:[{...VIEW.publishable[0],change_class:'major'}]})).toThrow(WorkbenchError);  // closed
  expect(()=>semanticReleases({...VIEW,current:'v1'})).toThrow(WorkbenchError);
  expect(()=>semanticReleases({...VIEW,releases:[{...VIEW.releases[1],items:[{kind:'definition',ref:'x',body:'y'}]}]})).toThrow(WorkbenchError);
});

test('only a role carrying the release Actions sees the section; the class is shown, never asked',()=>{
  expect(render(me(['eios:action:nexloop.workbench.read:1']))).toBe('');
  const html=render(me(RELEASE));
  expect(html).toContain('语义发布');expect(html).toContain('v0.1.1');expect(html).toContain('补丁级');expect(html).toContain('回退到 v0.0.1');
  expect(html).not.toContain('name="change_class"');expect(html).not.toContain('>major<');
  expect(html).toContain('必须是候选证据中未命中的那个词');
});

test('the rollback report says the Claim count and that Claims were not changed; items are counted',()=>{
  const v=semanticReleases(VIEW);
  expect(reportText(v.releases[0])).toBe('移除别名 1 个；期间经这些定义匹配的 Claim 1 条（未改动，需人工复核）');
  expect(reportText(v.releases[1])).toBe('—');
  expect(itemsText(v.releases[1])).toBe('别名 1 个，补充说明 1 条');
  expect(NOTE_LIMIT).toBe(200);
});
