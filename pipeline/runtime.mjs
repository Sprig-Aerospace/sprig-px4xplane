// Pipeline project template v2. Commands belong to Task; this adapter verifies
// the native profile, immutable Task artifacts and required report counts.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
const contract=JSON.parse(fs.readFileSync('pipeline/environment.json','utf8'));
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
const command=(name,args)=>execFileSync(name,args,{encoding:'utf8',timeout:15000,stdio:['ignore','pipe','pipe']}).trim();
export function verifyDependencyLocks(){
  for(const directory of contract.projectPackages??[]){
    const lock=JSON.parse(fs.readFileSync(path.join(directory,'package-lock.json'),'utf8'));
    assert(lock.lockfileVersion===3&&lock.packages&&typeof lock.packages==='object','Unsupported npm lockfile; version 3 packages required');
    for(const [name,pin]of Object.entries(lock.packages)){
      if(name==='')continue;
      assert(!pin.link&&/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)*$/.test(pin.version??''),`Unpinned package version or unsupported local link: ${name}`);
      const url=new URL(pin.resolved??'invalid:');
      assert(url.protocol==='https:'&&!url.username&&!url.password&&!url.hash,`Package requires an immutable HTTPS artifact: ${name}`);
      assert(/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(pin.integrity??'')&&Buffer.from(pin.integrity.slice(7),'base64').length===64,`Package SHA-512 integrity required: ${name}`);
    }
  }
}
export async function profile(){
  verifyDependencyLocks();
  assert(contract.profile.supportedHosts.some(host=>host.os===process.platform&&host.architecture===process.arch&&host.osVersions.includes(command('/usr/bin/sw_vers',['-productVersion']))),'Unsupported native host profile');
  assert(process.versions.node===contract.tools.node.version,'Node version mismatch');
  assert(command('npm',['--version'])===contract.tools.node.packageManager.version,'npm version mismatch');
  if(contract.tools.cmake){
    for(const name of ['cmake','ctest'])assert(command(name,['--version']).split('\n')[0]===`${name} version ${contract.tools.cmake.version}`,`${name} version mismatch`);
    assert(command('ninja',['--version'])===contract.tools.ninja.version,'Ninja version mismatch');
    const clang=command('clang++',['--version']);
    assert(clang.includes(`Apple clang version ${contract.tools.appleClang.version} (${contract.tools.appleClang.build})`)&&clang.includes(`Target: ${contract.tools.appleClang.target}`),'Apple Clang profile mismatch');
  }
}
export async function verifyTask(directory='.pipeline-state/tools'){
  const pin=contract.tools.task.artifacts['darwin-arm64'];
  for(const [file,sha,size]of [['task_darwin_arm64.tar.gz','sha256','sizeBytes'],['task_checksums.txt','checksumManifestSha256','checksumManifestSizeBytes']]){
    const bytes=fs.readFileSync(path.join(directory,file));assert(bytes.length===pin[size]&&hash(bytes)===pin[sha],`Task artifact integrity mismatch: ${file}`);
  }
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'pipeline-task-verify-'));
  try{
    command('/usr/bin/tar',['-xzf',path.resolve(directory,'task_darwin_arm64.tar.gz'),'-C',temporary]);
    assert(hash(fs.readFileSync(path.join(temporary,'task')))===hash(fs.readFileSync(path.join(directory,'task'))),'Task executable substitution');
    assert(hash(fs.readFileSync(path.join(temporary,'LICENSE')))===hash(fs.readFileSync(contract.tools.task.licenseFile)),'Task license mismatch');
    assert(command(path.resolve(directory,'task'),['--version'])===contract.tools.task.version,'Task version mismatch');
  }finally{fs.rmSync(temporary,{recursive:true,force:true});}
}
function report(entry){
  const counts={};
  for(const suite of contract.entryPoints[entry].requiredSuites){
    const text=fs.readFileSync(suite.report,'utf8');
    let count;
    if(suite.report.endsWith('.xml')){
      assert(/<testsuites?\b/.test(text)&&/<\/testsuites?>\s*$/.test(text),'Invalid JUnit report');
      assert(!/<(?:failure|error|skipped)\b/.test(text),'JUnit contains failed, errored or skipped tests');
      const cases=[...text.matchAll(/<testcase\b[^>]*(?:\/\s*>|>[\s\S]*?<\/testcase>)/g)];
      assert(cases.every(item=>/\bname\s*=\s*['"][^'"]+['"]/.test(item[0])),'JUnit testcase name missing');
      count=cases.length;
    }else{
      const value=JSON.parse(text);assert(value.outcome==='passed','Required assertion report did not pass');count=value.assertions;
    }
    assert(Number.isInteger(count)&&count>=Math.max(suite.minimumTests??0,suite.minimumAssertions??0),`Missing required counts: ${suite.id}`);counts[suite.id]=count;
  }
  const directory=`.pipeline-state/reports/${entry.replace(':','-')}`;
  fs.mkdirSync(directory,{recursive:true});fs.writeFileSync(`${directory}/run.json`,JSON.stringify({outcome:'passed',entry,profile:contract.profile.id,counts}));
}
if(process.argv[1]&&path.resolve(process.argv[1])===path.resolve('pipeline/runtime.mjs')){
  if(process.argv[2]==='report')report(process.argv[3]);
  else if(process.argv[2]==='profile')await profile();
  else throw new Error('Unsupported adapter operation');
}
