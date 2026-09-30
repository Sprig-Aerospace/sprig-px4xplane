import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {prepare, stageDraft, publishDraft} from '../scripts/local/publish-release.mjs';
import crypto from 'node:crypto';

const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function repo(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'px4 release fixture '));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const git = (...args) => {
    const r = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8'});
    assert.equal(r.status, 0, r.stderr);
  };
  git('init', '-q'); git('config', 'user.name', 'Release Fixture'); git('config', 'user.email', 'release@example.invalid');
  fs.writeFileSync(path.join(root, '.gitignore'), '.pipeline-state/\n');
  fs.writeFileSync(path.join(root, 'source.txt'), 'release fixture\n'); git('add', '.'); git('commit', '-qm', 'fixture source'); git('tag', 'v1.2.3');
  const artifactPath = path.join(root, '.pipeline-state/packages/px4xplane-ci-mac.tar.gz');
  fs.mkdirSync(path.dirname(artifactPath), {recursive: true}); fs.writeFileSync(artifactPath, 'qualified fixture package');
  const bytes = fs.readFileSync(artifactPath);
  const reportPath = path.join(root, '.pipeline-state/reports/package/plugin-build.json');
  fs.mkdirSync(path.dirname(reportPath), {recursive: true});
  fs.writeFileSync(reportPath, JSON.stringify({outcome: 'passed', assertions: 5, platform: 'mac', artifact: {
    path: '.pipeline-state/packages/px4xplane-ci-mac.tar.gz', bytes: bytes.length, sha256: sha(bytes)}}));
  return {root, git, artifactPath, reportPath};
}

function fakeGh(f, sourceCommit) {
  const fixtureState = path.join(f.root, '.pipeline-state/test-fake-gh');
  const bin = path.join(fixtureState, 'bin'), statePath = path.join(fixtureState, 'remote-release.json');
  fs.mkdirSync(bin, {recursive: true});
  const executable = path.join(bin, 'gh');
  fs.writeFileSync(executable, `#!/usr/bin/env node
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const args = process.argv.slice(2), statePath = process.env.FAKE_GH_STATE_FILE;
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
if (process.env.GH_TOKEN || process.env.GITHUB_TOKEN || process.env.GH_ENTERPRISE_TOKEN || process.env.GITHUB_OAUTH_TOKEN) process.exit(31);
if (args[0] === 'release' && args[1] === 'create') {
  const tag = args[2], assetPath = args[3], notesPath = args[args.indexOf('--notes-file') + 1];
  const bytes = fs.readFileSync(assetPath), body = fs.readFileSync(notesPath, 'utf8');
  const state = {tag_name: tag, draft: true, html_url: 'https://github.com/Sprig-Aerospace/sprig-px4xplane/releases/tag/' + tag, body,
    assets: [{name: path.basename(assetPath), size: bytes.length, digest: 'sha256:' + hash(bytes)}]};
  fs.writeFileSync(statePath, JSON.stringify(state)); process.stdout.write(state.html_url); process.exit(0);
}
if (args[0] === 'api' && args[1].includes('/commits/')) { process.stdout.write(JSON.stringify({sha: process.env.FAKE_GH_SOURCE_COMMIT})); process.exit(0); }
if (args[0] === 'api' && args[1].includes('/releases/tags/')) { process.stdout.write(fs.readFileSync(statePath, 'utf8')); process.exit(0); }
if (args[0] === 'release' && args[1] === 'edit') {
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8')); state.draft = false;
  fs.writeFileSync(statePath, JSON.stringify(state)); process.exit(0);
}
process.exit(32);
`);
  fs.chmodSync(executable, 0o755);
  return {bin, statePath, sourceCommit};
}

test('release preflight binds a clean tagged source to passing macOS package bytes', t => {
  const f = repo(t), plan = prepare(f.root, 'v1.2.3');
  assert.equal(plan.repository, 'Sprig-Aerospace/sprig-px4xplane');
  assert.equal(plan.tag, 'v1.2.3');
  assert.equal(plan.asset.sha256, sha(fs.readFileSync(f.artifactPath)));
  assert.equal(plan.notesSha256, sha(fs.readFileSync(path.join(f.root, '.pipeline-state/reports/release/release-notes.md'))));
  assert.match(fs.readFileSync(path.join(f.root, '.pipeline-state/reports/release/release-notes.md'), 'utf8'), /sprig-package source=/);
  assert.match(fs.readFileSync(path.join(f.root, '.pipeline-state/reports/release/release-plan.json'), 'utf8'), /draft-only-until-explicit-confirmation/);
});

test('release preflight rejects a tag that points at another commit', t => {
  const f = repo(t); fs.writeFileSync(path.join(f.root, 'source.txt'), 'changed\n');
  f.git('add', '.'); f.git('commit', '-qm', 'different source');
  assert.throws(() => prepare(f.root, 'v1.2.3'), /does not identify the current source commit/);
});

test('release preflight rejects package bytes changed after qualification', t => {
  const f = repo(t); fs.writeFileSync(f.artifactPath, 'tampered package');
  assert.throws(() => prepare(f.root, 'v1.2.3'), /do not match the passing Pipeline report/);
});

test('draft staging and publication require exact tag confirmation before network access', t => {
  const f = repo(t); prepare(f.root, 'v1.2.3');
  assert.throws(() => stageDraft(f.root, 'v1.2.3', 'v0.0.0'), /requires --confirm v1\.2\.3/);
  assert.throws(() => publishDraft(f.root, 'v1.2.3', 'v0.0.0'), /requires --confirm v1\.2\.3/);
});

test('draft and publication verify the remote source and package digest before publishing', t => {
  const f = repo(t), sourceCommit = spawnSync('git', ['-C', f.root, 'rev-parse', 'HEAD'], {encoding: 'utf8'}).stdout.trim();
  const fake = fakeGh(f, sourceCommit), oldPath = process.env.PATH, oldToken = process.env.GH_TOKEN;
  process.env.PATH = `${fake.bin}:${oldPath ?? ''}`;
  process.env.GH_TOKEN = 'fixture-only-sentinel';
  process.env.FAKE_GH_STATE_FILE = fake.statePath;
  process.env.FAKE_GH_SOURCE_COMMIT = sourceCommit;
  try {
    prepare(f.root, 'v1.2.3');
    const draft = stageDraft(f.root, 'v1.2.3', 'v1.2.3');
    assert.equal(draft.draft, true);
    assert.equal(draft.sha256, sha(fs.readFileSync(f.artifactPath)));
    const published = publishDraft(f.root, 'v1.2.3', 'v1.2.3');
    assert.equal(published.draft, false);
    assert.equal(published.sha256, draft.sha256);
    assert.equal(JSON.parse(fs.readFileSync(fake.statePath, 'utf8')).draft, false);
  } finally {
    process.env.PATH = oldPath ?? '';
    if (oldToken === undefined) delete process.env.GH_TOKEN; else process.env.GH_TOKEN = oldToken;
    delete process.env.FAKE_GH_STATE_FILE;
    delete process.env.FAKE_GH_SOURCE_COMMIT;
  }
});

test('publication rejects a draft with a mismatched remote asset digest', t => {
  const f = repo(t), sourceCommit = spawnSync('git', ['-C', f.root, 'rev-parse', 'HEAD'], {encoding: 'utf8'}).stdout.trim();
  const fake = fakeGh(f, sourceCommit), oldPath = process.env.PATH;
  process.env.PATH = `${fake.bin}:${oldPath ?? ''}`;
  process.env.FAKE_GH_STATE_FILE = fake.statePath;
  process.env.FAKE_GH_SOURCE_COMMIT = sourceCommit;
  try {
    prepare(f.root, 'v1.2.3');
    stageDraft(f.root, 'v1.2.3', 'v1.2.3');
    const state = JSON.parse(fs.readFileSync(fake.statePath, 'utf8'));
    state.assets[0].digest = 'sha256:' + '0'.repeat(64);
    fs.writeFileSync(fake.statePath, JSON.stringify(state));
    assert.throws(() => publishDraft(f.root, 'v1.2.3', 'v1.2.3'), /Draft metadata and asset digest/);
    assert.equal(JSON.parse(fs.readFileSync(fake.statePath, 'utf8')).draft, true);
  } finally {
    process.env.PATH = oldPath ?? '';
    delete process.env.FAKE_GH_STATE_FILE;
    delete process.env.FAKE_GH_SOURCE_COMMIT;
  }
});

test('release preflight rejects uncommitted source', t => {
  const f = repo(t); fs.writeFileSync(path.join(f.root, 'source.txt'), 'dirty\n');
  assert.throws(() => prepare(f.root, 'v1.2.3'), /dirty source checkout/);
});
