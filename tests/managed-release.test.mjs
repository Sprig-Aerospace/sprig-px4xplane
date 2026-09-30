import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {generate, execute} from '../scripts/local/managed-release-workflow.mjs';

const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const fileSha = file => sha(fs.readFileSync(file));
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'px4-managed-release-')));
  if (!process.env.PIPELINE_TEST_KEEP) t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const source = path.join(root, 'source'), run = path.join(root, 'package-run');
  fs.mkdirSync(path.join(source, 'scripts/local'), {recursive: true});
  fs.mkdirSync(path.join(source, 'pipeline/workflows/local-build'), {recursive: true});
  fs.writeFileSync(path.join(source, '.gitignore'), '.pipeline-state/\n');
  fs.copyFileSync(new URL('../scripts/local/managed-release-workflow.mjs', import.meta.url),
    path.join(source, 'scripts/local/managed-release-workflow.mjs'));
  fs.writeFileSync(path.join(source, 'pipeline/workflows/local-build/environment.json'), JSON.stringify({
    schemaVersion: 2, contractVersion: 'pipeline.local-workflow/v2',
    pipelineCompatibility: {environmentContract: 2, runnerProtocol: 1,
      requiredFeatures: ['bounded-package-artifacts', 'committed-package-snapshots']},
    profile: {id: 'fixture', supportedHosts: [{os: 'darwin', architecture: 'arm64'}]},
    tools: {task: {version: '3.53.1'}},
    paths: {sourceInputs: ['.gitignore', 'scripts/local/managed-release-workflow.mjs',
      'pipeline/workflows/local-build/environment.json'], stateRoot: '.pipeline-state'},
    resourceBudget: {hostConcurrentRuns: 1, logMiBPerRun: 10, stateStorageMiBPerWorktree: 512},
    entryPoints: {}
  }));
  const git = (...args) => {
    const result = spawnSync('git', ['-C', source, ...args], {encoding: 'utf8'});
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  git('init', '-q'); git('config', 'user.name', 'Release Fixture'); git('config', 'user.email', 'fixture@example.invalid');
  git('add', '.'); git('commit', '-qm', 'candidate'); git('tag', 'v1.2.3');
  const commit = git('rev-parse', 'HEAD');
  fs.mkdirSync(path.join(run, 'package'), {recursive: true});
  fs.mkdirSync(path.join(run, 'reports/package'), {recursive: true});
  const artifact = path.join(run, 'package/artifact');
  fs.writeFileSync(artifact, 'qualified macOS package');
  const asset = {bytes: fs.statSync(artifact).size, sha256: fileSha(artifact)};
  fs.writeFileSync(path.join(run, 'record.json'), JSON.stringify({task: 'package', outcome: 'passed',
    category: 'passed', releaseEligible: true, source: {clean: true, commit},
    package: {name: 'px4xplane-ci-mac.tar.gz', ...asset}}));
  fs.writeFileSync(path.join(run, 'reports/package/plugin-build.json'), JSON.stringify({outcome: 'passed',
    platform: 'mac', assertions: 5, artifact: {path: '.pipeline-state/packages/px4xplane-ci-mac.tar.gz', ...asset}}));
  const taskBinary = process.env.PIPELINE_TEST_TASK ?? path.join(root, 'task');
  if (!process.env.PIPELINE_TEST_TASK) {
    fs.writeFileSync(taskBinary, '#!/bin/sh\necho 3.53.1\n'); fs.chmodSync(taskBinary, 0o755);
  }
  const prep = path.join(root, 'preparation.json');
  fs.writeFileSync(prep, JSON.stringify({source: {clean: true, commit}}));
  return {root, source, run, commit, artifact, asset, taskBinary, prep};
}

function fakeGh(f) {
  const bin = path.join(f.root, 'fake-gh-bin');
  const state = path.join(f.root, 'fake-release.json');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'gh'), `#!/usr/bin/env node
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const args = process.argv.slice(2), state = ${JSON.stringify(state)}, commit = ${JSON.stringify(f.commit)};
if (process.env.GH_TOKEN || process.env.GITHUB_TOKEN || process.env.GH_ENTERPRISE_TOKEN || process.env.GITHUB_OAUTH_TOKEN) process.exit(41);
if (args[0] === 'api' && args[1].includes('/commits/')) { process.stdout.write(JSON.stringify({sha: commit})); process.exit(0); }
if (args[0] === 'api' && args[1].includes('/releases/tags/')) {
  if (!fs.existsSync(state)) process.exit(42);
  process.stdout.write(fs.readFileSync(state)); process.exit(0);
}
if (args[0] === 'release' && args[1] === 'create') {
  if (fs.existsSync(state)) process.exit(43);
  const tag = args[2], asset = args[3], notes = fs.readFileSync(args[args.indexOf('--notes-file') + 1], 'utf8');
  const bytes = fs.readFileSync(asset);
  fs.writeFileSync(state, JSON.stringify({tag_name: tag, draft: true,
    html_url: 'https://example.invalid/release/' + tag, body: notes,
    assets: [{name: path.basename(asset), size: bytes.length,
      digest: 'sha256:' + crypto.createHash('sha256').update(bytes).digest('hex')}]}));
  process.exit(0);
}
if (args[0] === 'release' && args[1] === 'edit') {
  const current = JSON.parse(fs.readFileSync(state)); current.draft = false;
  fs.writeFileSync(state, JSON.stringify(current)); process.exit(0);
}
process.exit(44);
`);
  fs.chmodSync(path.join(bin, 'gh'), 0o755);
  return {bin, state};
}

test('generated managed plan binds tag, source and retained package without network access', t => {
  const f = fixture(t), workflow = path.join(f.root, 'stage');
  const output = generate([f.source, f.run, 'v1.2.3', workflow, 'stage', f.taskBinary]);
  assert.equal(output.packageSha256, f.asset.sha256);
  const contract = JSON.parse(fs.readFileSync(path.join(workflow, 'environment.json')));
  assert.deepEqual(Object.keys(contract.entryPoints), ['check:fast', 'check', 'package']);
  assert.equal(contract.entryPoints.package.artifact, '.pipeline-state/packages/release-receipt.tar.gz');
  const previous = {source: process.env.PIPELINE_SOURCE_DIR, control: process.env.PIPELINE_WORKFLOW_DIR,
    prep: process.env.PIPELINE_RUN_REQUEST};
  process.env.PIPELINE_SOURCE_DIR = f.source;
  process.env.PIPELINE_WORKFLOW_DIR = workflow;
  process.env.PIPELINE_RUN_REQUEST = f.prep;
  try {
    assert.equal(execute('check:fast').operation, 'plan');
    assert.equal(JSON.parse(fs.readFileSync(path.join(f.source,
      '.pipeline-state/reports/check-fast/run.json'))).outcome, 'passed');
    fs.writeFileSync(f.artifact, 'changed bytes');
    assert.throws(() => execute('check:fast'), /Retained package bytes do not match/);
  } finally {
    for (const [key, name] of [['source', 'PIPELINE_SOURCE_DIR'], ['control', 'PIPELINE_WORKFLOW_DIR'],
      ['prep', 'PIPELINE_RUN_REQUEST']]) {
      if (previous[key] === undefined) delete process.env[name]; else process.env[name] = previous[key];
    }
  }
});

test('generation rejects a different package source and a dirty release checkout', t => {
  const f = fixture(t);
  const recordPath = path.join(f.run, 'record.json'), record = JSON.parse(fs.readFileSync(recordPath));
  record.source.commit = '0'.repeat(40);
  fs.writeFileSync(recordPath, JSON.stringify(record));
  assert.throws(() => generate([f.source, f.run, 'v1.2.3', path.join(f.root, 'wrong'),
    'stage', f.taskBinary]), /selected source/);
  record.source.commit = f.commit;
  fs.writeFileSync(recordPath, JSON.stringify(record));
  fs.writeFileSync(path.join(f.source, 'new-untracked.txt'), 'dirty');
  assert.throws(() => generate([f.source, f.run, 'v1.2.3', path.join(f.root, 'dirty'),
    'stage', f.taskBinary]), /Source checkout must be clean/);
});

test('explicit draft, remote readback and publication use only selected fixture bytes', t => {
  const f = fixture(t), fake = fakeGh(f);
  const old = {source: process.env.PIPELINE_SOURCE_DIR, control: process.env.PIPELINE_WORKFLOW_DIR,
    prep: process.env.PIPELINE_RUN_REQUEST, path: process.env.PATH, token: process.env.GH_TOKEN};
  process.env.PIPELINE_SOURCE_DIR = f.source;
  process.env.PIPELINE_RUN_REQUEST = f.prep;
  process.env.PATH = `${fake.bin}:${old.path ?? ''}`;
  process.env.GH_TOKEN = 'fixture-token-must-not-reach-gh';
  try {
    const stage = path.join(f.root, 'stage');
    generate([f.source, f.run, 'v1.2.3', stage, 'stage', f.taskBinary]);
    process.env.PIPELINE_WORKFLOW_DIR = stage;
    assert.equal(execute('package').remote.draft, true);
    assert.equal(execute('check').remote.draft, true);
    const publish = path.join(f.root, 'publish');
    generate([f.source, f.run, 'v1.2.3', publish, 'publish', f.taskBinary]);
    process.env.PIPELINE_WORKFLOW_DIR = publish;
    assert.equal(execute('package').remote.draft, false);
    assert.equal(execute('check').remote.draft, false);
    const remote = JSON.parse(fs.readFileSync(fake.state));
    assert.equal(remote.assets[0].digest, `sha256:${f.asset.sha256}`);
  } finally {
    for (const [key, name] of [['source', 'PIPELINE_SOURCE_DIR'], ['control', 'PIPELINE_WORKFLOW_DIR'],
      ['prep', 'PIPELINE_RUN_REQUEST'], ['path', 'PATH'], ['token', 'GH_TOKEN']]) {
      if (old[key] === undefined) delete process.env[name]; else process.env[name] = old[key];
    }
  }
});

test('the selected native Pipeline runner executes the finite local plan', t => {
  if (!process.env.PIPELINE_TEST_RUNNER || !process.env.PIPELINE_TEST_TASK) return t.skip('Runner identity not selected.');
  const f = fixture(t), workflow = path.join(f.root, 'managed-plan');
  generate([f.source, f.run, 'v1.2.3', workflow, 'stage', f.taskBinary]);
  const fake = fakeGh(f);
  const hostHome = path.join(f.root, 'test-host-home'); fs.mkdirSync(hostHome);
  const env = {...process.env, PIPELINE_HOME: path.join(f.root, 'pipeline-state'),
    HOME: hostHome, PATH: `${fake.bin}:${process.env.PATH ?? ''}`};
  const runner = (...args) => {
    const result = spawnSync(process.env.PIPELINE_TEST_RUNNER,
      ['--workflow', workflow, ...args], {env, encoding: 'utf8', timeout: 120000});
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
    return JSON.parse(result.stdout);
  };
  assert.equal(runner('run', f.source, 'check:fast', '600').outcome, 'passed');
  // Approval below is limited to a generated, isolated test identity with fake gh.
  const inspect = runner('onboarding', 'inspect', f.source);
  assert.equal(inspect.state, 'needs-trust');
  runner('onboarding', 'approve', f.source, inspect.reviewId, '--consent');
  const preview = runner('package-preview', f.source);
  assert.equal(preview.commit, f.commit);
  assert.equal(runner('run', f.source, 'package', '600').outcome, 'passed');
  assert.equal(runner('run', f.source, 'check', '600').outcome, 'passed');
  fs.writeFileSync(f.artifact, 'tampered retained package');
  const rejected = spawnSync(process.env.PIPELINE_TEST_RUNNER,
    ['--workflow', workflow, 'run', f.source, 'check:fast', '600'],
    {env, encoding: 'utf8', timeout: 120000});
  assert.equal(rejected.status, 1);
  assert.equal(JSON.parse(rejected.stdout).outcome, 'failed');
});
