/** NX-029 D8: removal of one settled Run's private runtime directory (Pi SQLite and its WAL files).
 *  Reached only through the Host's loopback internal API with the internal key; the retention keeper calls it for Runs
 *  the database already holds terminal and in its purge queue. The directory must be a real directory under the runtime
 *  root (never a symlink); a missing directory is reported as absent, never as purged. */
import {constants,closeSync,fsyncSync,lstatSync,openSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {RuntimeError} from './runtime-adapter.js';

const RUN_ID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validRunId(runId:unknown):runId is string{return typeof runId==='string'&&RUN_ID.test(runId);}

export function purgeRunDirectory(root:string,runId:string):'purged'|'absent'{
  if(!validRunId(runId))throw new RuntimeError('invalid_runtime_request');
  const directory=join(root,runId);
  let info;
  try{info=lstatSync(directory);}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return 'absent';throw new RuntimeError('runtime_purge_failed');}
  if(info.isSymbolicLink()||!info.isDirectory())throw new RuntimeError('runtime_storage_refused');
  try{rmSync(directory,{recursive:true,force:false});}catch{throw new RuntimeError('runtime_purge_failed');}
  const fd=openSync(root,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);
  try{fsyncSync(fd);}finally{closeSync(fd);}
  return 'purged';
}
