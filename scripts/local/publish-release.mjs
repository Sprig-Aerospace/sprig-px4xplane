import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';

const repo = 'Sprig-Aerospace/sprig-px4xplane';
const artifactRelative = '.pipeline-state/packages/px4xplane-ci-mac.tar.gz';
const reportRelative = '.pipeline-state/reports/package/plugin-build.json';
const planRelative = '.pipeline-state/reports/release/release-plan.json';
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const fail = message => { throw new Error(message); };
const validTag = tag => /^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(tag) || /^dev-\d{8}-\d{6}$/.test(tag);

function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8'});
  if (result.status !== 0) fail(`git ${args[0]} failed`);
  return result.stdout.trim();
}

function candidate(root, tag) {
  if (!validTag(tag)) fail('Release tag must be vMAJOR.MINOR.PATCH[-prerelease] or dev-YYYYMMDD-HHMMSS.');
  const status = git(root, 'status', '--porcelain', '--untracked-files=normal');
  if (status) fail('Refusing release from a dirty source checkout.');
  const commit = git(root, 'rev-parse', 'HEAD');
  let tagCommit;
  try { tagCommit = git(root, 'rev-parse', `${tag}^{commit}`); }
  catch { fail(`Selected tag does not exist locally: ${tag}`); }
  if (tagCommit !== commit) fail(`Selected tag ${tag} does not identify the current source commit.`);

  const reportPath = path.join(root, reportRelative);
  const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
  if (report.outcome !== 'passed' || report.assertions < 5 || report.platform !== 'mac') fail('A passing macOS package report is required.');
  if (report.artifact?.path !== artifactRelative) fail('Package report names an unexpected artifact.');
  const artifactPath = path.join(root, artifactRelative);
  const artifact = fs.readFileSync(artifactPath);
  const artifactSha256 = hash(artifact);
  if (artifact.length !== report.artifact.bytes || artifactSha256 !== report.artifact.sha256) fail('Package bytes do not match the passing Pipeline report.');
  return {schemaVersion: 1, repository: repo, tag, sourceCommit: commit,
    asset: {path: artifactRelative, bytes: artifact.length, sha256: artifactSha256},
    packageReport: reportRelative, publication: 'draft-only-until-explicit-confirmation'};
}

function gh(args, root) {
  const env = {...process.env};
  for (const name of ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_OAUTH_TOKEN']) delete env[name];
  const result = spawnSync('gh', args, {cwd: root, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
  if (result.status !== 0) fail(`GitHub CLI operation failed (${result.status ?? 'signal'}); inspect GitHub CLI authentication and release state.`);
  return result.stdout.trim();
}

function notes(root, plan) {
  const {tag, sourceCommit, asset} = plan;
  let previous;
  try { previous = git(root, 'describe', '--tags', '--abbrev=0', `${tag}^`); }
  catch {
    const tags = git(root, 'tag', '--list', 'v*').split('\n').filter(Boolean).filter(item => item !== tag);
    previous = tags.find(item => git(root, 'rev-parse', `${item}^{commit}`) === sourceCommit);
    if (!previous && tags.length) {
      fail('Cannot identify the previous release tag; the selected source needs complete release history.');
    }
    if (!previous) return `# px4xplane ${tag}\n\nInitial release.\n\n- Source commit: ${sourceCommit}\n- Qualified output: macOS arm64\n- Package SHA-256: ${asset.sha256}\n\n<!-- sprig-package source=${sourceCommit} sha256=${asset.sha256} -->\n`;
  }
  const changes = git(root, 'log', '--pretty=format:- %s', `${previous}..${tag}`);
  return `# px4xplane ${tag}\n\n## Changes\n\n${changes || '- No commits since the previous tag.'}\n\n## Build\n\n- Source commit: ${sourceCommit}\n- Qualified output: macOS arm64\n- Package SHA-256: ${asset.sha256}\n\n<!-- sprig-package source=${sourceCommit} sha256=${asset.sha256} -->\n`;
}

export function prepare(root, tag) {
  const plan = candidate(root, tag);
  const releaseNotes = notes(root, plan);
  plan.notesSha256 = hash(Buffer.from(releaseNotes));
  const planPath = path.join(root, planRelative);
  fs.mkdirSync(path.dirname(planPath), {recursive: true});
  fs.writeFileSync(path.join(path.dirname(planPath), 'release-notes.md'), releaseNotes, {mode: 0o600});
  fs.writeFileSync(planPath, `${JSON.stringify(plan, null, 2)}\n`, {mode: 0o600});
  return plan;
}

export function stageDraft(root, tag, confirmation) {
  if (confirmation !== tag) fail(`Draft creation requires --confirm ${tag}.`);
  const plan = candidate(root, tag);
  const releaseNotes = notes(root, plan);
  plan.notesSha256 = hash(Buffer.from(releaseNotes));
  const planPath = path.join(root, planRelative);
  if (fs.readFileSync(planPath, 'utf8') !== `${JSON.stringify(plan, null, 2)}\n`) fail('Release plan is missing or stale; rerun release:prepare.');
  const notesPath = path.join(root, '.pipeline-state/reports/release/release-notes.md');
  fs.writeFileSync(notesPath, releaseNotes, {mode: 0o600});
  const args = ['release', 'create', tag, plan.asset.path, '--repo', repo, '--title', `px4xplane ${tag}`,
    '--notes-file', notesPath, '--draft', '--verify-tag'];
  if (tag.startsWith('dev-') || /alpha|beta|rc/.test(tag)) args.push('--prerelease');
  const url = gh(args, root);
  const readback = gh(['api', `repos/${repo}/releases/tags/${tag}`], root);
  const release = JSON.parse(readback);
  if (release.tag_name !== tag || release.draft !== true || !release.html_url) fail('GitHub draft release readback did not match the selected tag.');
  const asset = (release.assets ?? []).find(item => item.name === path.basename(plan.asset.path));
  if (!asset || asset.size !== plan.asset.bytes || asset.digest !== `sha256:${plan.asset.sha256}`) fail('GitHub draft asset readback does not match the qualified package digest.');
  return {url: release.html_url, tag, draft: true, asset: asset.name, bytes: asset.size, sha256: plan.asset.sha256, cliOutput: url};
}

export function publishDraft(root, tag, confirmation) {
  if (confirmation !== tag) fail(`Publishing requires --confirm ${tag}.`);
  if (!validTag(tag)) fail('Release tag must be vMAJOR.MINOR.PATCH[-prerelease] or dev-YYYYMMDD-HHMMSS.');
  const before = JSON.parse(gh(['api', `repos/${repo}/releases/tags/${tag}`], root));
  if (before.tag_name !== tag || before.draft !== true) fail('Only the matching verified draft release can be published.');
  const asset = (before.assets ?? []).find(item => item.name === path.basename(artifactRelative));
  const metadata = new RegExp(`<!-- sprig-package source=([0-9a-f]{40}) sha256=([0-9a-f]{64}) -->`).exec(before.body ?? '');
  if (!metadata || !asset || asset.digest !== `sha256:${metadata[2]}`) fail('Draft metadata and asset digest are missing or inconsistent.');
  const remoteCommit = JSON.parse(gh(['api', `repos/${repo}/commits/${tag}`], root)).sha;
  if (remoteCommit !== metadata[1]) fail('Remote tag no longer identifies the source bound to the staged package.');
  gh(['release', 'edit', tag, '--repo', repo, '--draft=false'], root);
  const after = JSON.parse(gh(['api', `repos/${repo}/releases/tags/${tag}`], root));
  const publishedAsset = (after.assets ?? []).find(item => item.name === asset.name);
  if (after.tag_name !== tag || after.draft !== false || !after.html_url || publishedAsset?.digest !== asset.digest) fail('Published release readback failed.');
  return {url: after.html_url, tag, draft: false, asset: asset.name, bytes: asset.size, sha256: metadata[2]};
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve('scripts/local/publish-release.mjs')) {
  const [operation, tag, confirmationFlag, confirmation] = process.argv.slice(2);
  const root = process.env.PIPELINE_SOURCE_DIR ? path.resolve(process.env.PIPELINE_SOURCE_DIR) : process.cwd();
  let result;
  if (operation === 'prepare' && tag) result = prepare(root, tag);
  else if (operation === 'stage-draft' && confirmationFlag === '--confirm' && tag && confirmation) result = stageDraft(root, tag, confirmation);
  else if (operation === 'publish' && confirmationFlag === '--confirm' && tag && confirmation) result = publishDraft(root, tag, confirmation);
  else fail('Usage: publish-release.mjs prepare TAG | stage-draft TAG --confirm TAG | publish TAG --confirm TAG');
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}
