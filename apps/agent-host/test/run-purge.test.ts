/** NX-029 D8: the Host removes one settled Run directory; absent is not purged; symlinks and bad ids are refused. */
import {mkdtempSync,mkdirSync,writeFileSync,existsSync,symlinkSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {describe,it,expect} from 'vitest';
import {purgeRunDirectory} from '../dist/run-purge.js';

describe('run purge',()=>{
  it('removes the Run directory with its SQLite and WAL files, then reports absent',()=>{
    const root=mkdtempSync(join(tmpdir(),'nx029-purge-')),run=randomUUID(),dir=join(root,run);
    try{
      mkdirSync(dir,{mode:0o700});for(const f of ['runtime.sqlite','runtime.sqlite-wal','runtime.sqlite-shm'])writeFileSync(join(dir,f),'synthetic');
      expect(purgeRunDirectory(root,run)).toBe('purged');
      expect(existsSync(dir)).toBe(false);
      expect(purgeRunDirectory(root,run)).toBe('absent');
    }finally{rmSync(root,{recursive:true,force:true});}
  });
  it('refuses a symlinked Run directory and anything that is not a Run id',()=>{
    const root=mkdtempSync(join(tmpdir(),'nx029-purge-')),outside=mkdtempSync(join(tmpdir(),'nx029-outside-')),run=randomUUID();
    try{
      writeFileSync(join(outside,'keep'),'x');symlinkSync(outside,join(root,run));
      expect(()=>purgeRunDirectory(root,run)).toThrow('runtime_storage_refused');
      expect(existsSync(join(outside,'keep'))).toBe(true);
      for(const bad of ['..','../x',run.toUpperCase(),'','not-a-uuid'])expect(()=>purgeRunDirectory(root,bad)).toThrow('invalid_runtime_request');
    }finally{rmSync(root,{recursive:true,force:true});rmSync(outside,{recursive:true,force:true});}
  });
});
