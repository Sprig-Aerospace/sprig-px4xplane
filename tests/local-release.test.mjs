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

test('release preflight binds a clean tagged source to passing macOS package bytes', t => {
  const f = repo(t), plan = prepare(f.root, 'v1.2.3');
  assert.equal(plan.repository, 'Sprig-Aerospace/sprig-px4xplane');
  assert.equal(plan.tag, 'v1.2.3');
  assert.equal(plan.asset.sha256, sha(fs.readFileSync(f.artifactPath)));
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

test('release preflight rejects uncommitted source', t => {
  const f = repo(t); fs.writeFileSync(path.join(f.root, 'source.txt'), 'dirty\n');
  assert.throws(() => prepare(f.root, 'v1.2.3'), /dirty source checkout/);
});
