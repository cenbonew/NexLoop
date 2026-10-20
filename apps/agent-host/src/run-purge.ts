/** NX-029 D8: removal of one settled Run's private runtime directory (Pi SQLite and its WAL files).
 *  Reached only through the Host's loopback internal API with the internal key; the retention keeper calls it for Runs
 *  the database already holds terminal and in its purge queue. The directory must be a real directory under the runtime
 *  root (never a symlink); a missing directory is reported as absent, never as purged. */
import {constants,closeSync,fsyncSync,lstatSync,openSync,readdirSync,rmSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
import {RuntimeError} from './runtime-adapter.js';
import {worldDirectory} from './worlds.js';

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

/** NX-041 S6 (D2, D6) / review 3.2: what a world directory held - every entry under it (never following a symlink), as sorted
 *  "path<TAB>kind<TAB>size" lines; the digest of those lines and their count go into the Host's signed deletion proof. */
export function worldListing(directory:string):{entries:number;listing_digest:string}{
  const lines:string[]=[];
  const walk=(path:string,relative:string)=>{
    for(const entry of readdirSync(path,{withFileTypes:true})){
      const full=join(path,entry.name),rel=relative?relative+'/'+entry.name:entry.name,info=lstatSync(full);
      const kind=info.isSymbolicLink()?'link':info.isDirectory()?'dir':info.isFile()?'file':'other';
      lines.push(`${rel}\t${kind}\t${kind==='file'?info.size:0}`);
      if(kind==='dir')walk(full,rel);
    }
  };
  walk(directory,'');lines.sort();
  return {entries:lines.length,listing_digest:createHash('sha256').update(lines.join('\n')).digest('hex')};
}
const EMPTY_LISTING={entries:0,listing_digest:createHash('sha256').update('').digest('hex')};
/** NX-041 S6 (D2, D6): removal of one deleted simulation world's directory (every Run's Pi SQLite and files of that world) under a
 *  simulation Host's runtime root. Only sim:/shadow: worlds; never a symlink; a missing directory is absent, never purged. The
 *  listing is taken before removal and the directory is checked absent after it. */
export function purgeWorldDirectory(root:string,world:string):{outcome:'purged'|'absent';entries:number;listing_digest:string}{
  const segment=worldDirectory(world);
  if(segment===null)throw new RuntimeError('invalid_runtime_request');
  const directory=join(root,segment);
  let info;
  try{info=lstatSync(directory);}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return {outcome:'absent',...EMPTY_LISTING};throw new RuntimeError('runtime_purge_failed');}
  if(info.isSymbolicLink()||!info.isDirectory())throw new RuntimeError('runtime_storage_refused');
  let listing;
  try{listing=worldListing(directory);rmSync(directory,{recursive:true,force:false});}catch{throw new RuntimeError('runtime_purge_failed');}
  const fd=openSync(root,constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW);
  try{fsyncSync(fd);}finally{closeSync(fd);}
  try{lstatSync(directory);throw new RuntimeError('runtime_purge_failed');}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw new RuntimeError('runtime_purge_failed');}
  return {outcome:'purged',...listing};
}
