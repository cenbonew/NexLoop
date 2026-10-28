import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {DataTable} from '../src/workbench/DataTable';
import {conversationLabel,localTime} from '../src/WebChat';

test('every cell carries its column label for the narrow card view; main, count and wide columns are marked', ()=>{
  const html=renderToStaticMarkup(h(DataTable<{id:string;n:number;text:string}>,{label:'样例',rows:[{id:'r1',n:3,text:'长文本'}],rowKey:r=>r.id,
    columns:[{label:'名称',main:true,cell:r=>r.id},{label:'次数',num:true,cell:r=>r.n},{label:'说明',wide:true,cell:r=>r.text}]}));
  expect(html).toContain('<table class="data-table" aria-label="样例">');
  expect(html).toContain('<th scope="col" class="cell-main">名称</th>');
  expect(html).toContain('<td data-label="名称" class="cell-main">r1</td>');
  expect(html).toContain('<td data-label="次数" class="cell-num">3</td>');
  expect(html).toContain('<td data-label="说明" class="cell-wide">长文本</td>');
});

test('a conversation is named by its place in the list, not by its full ID; times are local', ()=>{
  const id='f'.repeat(64);
  expect(conversationLabel(0,id)).toBe('对话 1');expect(conversationLabel(0,id)).not.toContain('ffffff');
  expect(conversationLabel(0,id)).not.toContain(id);
  expect(localTime('2026-10-10T09:00:00Z')).toBe(new Date('2026-10-10T09:00:00Z').toLocaleString('zh-CN',{hour12:false}));
  expect(localTime('not a time')).toBe('not a time');
});
