/** Owned private files; empty material is permitted only by explicit caller. */
import {constants,closeSync,fstatSync,openSync,readSync} from 'node:fs';
export function readPrivateMaterial(path:string,maximum:number,allowEmpty=false):Buffer{
  if(!Number.isSafeInteger(maximum)||maximum<1)throw new Error('private material refused');
  const descriptor=openSync(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{
    const info=fstatSync(descriptor);
    if(!info.isFile()||info.uid!==process.getuid!()||(info.mode&0o777)!==0o600||info.nlink!==1||info.size>(maximum)||(!allowEmpty&&info.size===0))throw new Error('private material refused');
    const value=Buffer.alloc(maximum+1);let length=0;
    while(length<=maximum){const count=readSync(descriptor,value,length,value.length-length,null);if(count===0)break;length+=count;}
    if(length>maximum||(!allowEmpty&&length===0))throw new Error('private material refused');
    return value.subarray(0,length);
  }finally{closeSync(descriptor);}
}
