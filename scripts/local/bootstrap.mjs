import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {verifyTask} from '../../pipeline/runtime.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const state = path.join(root, '.pipeline-state');
const tools = path.join(state, 'tools');
const contract = JSON.parse(fs.readFileSync(path.join(root, 'pipeline/environment.json'), 'utf8'));
const pin = contract.tools.task.artifacts['darwin-arm64'];
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const verify = (name, bytes, digest, size) => {
  if (bytes.length !== size || sha256(bytes) !== digest) throw new Error(`Pinned Task artifact failed integrity check: ${name}`);
};

async function main() {
  fs.mkdirSync(state, {recursive: true, mode: 0o700});
  if (fs.lstatSync(state).isSymbolicLink()) throw new Error('Refusing symlink Pipeline state directory');
  try {
    await verifyTask(tools);
    console.log('Pinned Pipeline Task is already installed and verified.');
    return;
  } catch { /* Install a fresh, hash-pinned copy below. */ }

  const temporary = fs.mkdtempSync(path.join(state, 'task-setup-'));
  try {
    const archives = [
      ['task_darwin_arm64.tar.gz', pin.url, pin.sha256, pin.sizeBytes],
      ['task_checksums.txt', pin.checksumManifestUrl, pin.checksumManifestSha256, pin.checksumManifestSizeBytes],
    ];
    for (const [name, url, digest, size] of archives) {
      const response = await fetch(url, {signal: AbortSignal.timeout(60000)});
      if (!response.ok) throw new Error(`Pinned Task download failed: ${name} HTTP ${response.status}`);
      const bytes = Buffer.from(await response.arrayBuffer());
      verify(name, bytes, digest, size);
      fs.writeFileSync(path.join(temporary, name), bytes, {mode: 0o600, flag: 'wx'});
    }
    const extraction = spawnSync('/usr/bin/tar', ['-xzf', path.join(temporary, archives[0][0]), '-C', temporary], {encoding:'utf8',timeout:60000});
    if (extraction.status !== 0) throw new Error(`Pinned Task extraction failed: ${extraction.stderr || extraction.error || extraction.status}`);
    const task = path.join(temporary, 'task');
    const version = spawnSync(task, ['--version'], {encoding:'utf8',timeout:10000});
    if (version.status !== 0 || version.stdout.trim() !== pin.version) throw new Error('Pinned Task version mismatch');
    if (sha256(fs.readFileSync(path.join(temporary, 'LICENSE'))) !== sha256(fs.readFileSync(path.join(root, 'pipeline/LICENSE.task')))) {
      throw new Error('Pinned Task license mismatch');
    }
    fs.mkdirSync(tools, {recursive:true, mode:0o700});
    if (fs.lstatSync(tools).isSymbolicLink()) throw new Error('Refusing symlink Task tools directory');
    for (const [name] of archives) fs.renameSync(path.join(temporary, name), path.join(tools, name));
    fs.renameSync(task, path.join(tools, 'task'));
    fs.chmodSync(path.join(tools, 'task'), 0o755);
    await verifyTask(tools);
    console.log('Downloaded and verified the pinned Pipeline Task release.');
  } finally {
    fs.rmSync(temporary, {recursive:true,force:true});
  }
}

main().catch(error => {
  console.error(`Pipeline Task setup failed: ${error.message}`);
  process.exitCode = 1;
});
