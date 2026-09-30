// Generate a reviewed, finite Pipeline workflow for one immutable release attempt.
// Only its package entry can write to GitHub; check:fast plans and check reads back.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';

const repository = 'Sprig-Aerospace/sprig-px4xplane';
const packageName = 'px4xplane-ci-mac.tar.gz';
const profile = 'px4xplane-release-v1';
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function fileSha(file) {
  const digest = crypto.createHash('sha256'), buffer = Buffer.allocUnsafe(1024 * 1024);
  const descriptor = fs.openSync(file, 'r');
  try {
    for (;;) {
      const bytes = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (!bytes) return digest.digest('hex');
      digest.update(buffer.subarray(0, bytes));
    }
  } finally { fs.closeSync(descriptor); }
}
const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const requireTrue = (condition, message) => { if (!condition) throw new Error(message); };
const validTag = tag => /^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(tag) || /^dev-\d{8}-\d{6}$/.test(tag);
const hex = (value, length) => typeof value === 'string' && new RegExp(`^[0-9a-f]{${length}}$`).test(value);

function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8'});
  requireTrue(result.status === 0, `git ${args[0]} failed`);
  return result.stdout.trim();
}

function packageEvidence(runDirectory, expectedCommit, expectedRecordSha) {
  const recordPath = path.join(runDirectory, 'record.json');
  requireTrue(fs.lstatSync(recordPath).isFile(), 'Package record must be a regular file.');
  requireTrue(fileSha(recordPath) === expectedRecordSha, 'Package run record changed after selection.');
  const record = readJson(recordPath);
  requireTrue(record.task === 'package' && record.outcome === 'passed' && record.category === 'passed' &&
    record.releaseEligible === true && record.source?.clean === true && record.source?.commit === expectedCommit,
  'Package run is not a passing, clean build of the selected source.');
  requireTrue(record.package?.name === packageName && hex(record.package?.sha256, 64) &&
    Number.isSafeInteger(record.package?.bytes), 'Package record has no supported macOS artifact.');
  const report = readJson(path.join(runDirectory, 'reports/package/plugin-build.json'));
  requireTrue(report.outcome === 'passed' && report.platform === 'mac' && report.assertions >= 5 &&
    report.artifact?.path === `.pipeline-state/packages/${packageName}` &&
    report.artifact.sha256 === record.package.sha256 && report.artifact.bytes === record.package.bytes,
  'Package report does not agree with the passing Pipeline record.');
  const artifact = path.join(runDirectory, 'package/artifact');
  const stat = fs.lstatSync(artifact);
  requireTrue(stat.isFile() && stat.size === record.package.bytes && fileSha(artifact) === record.package.sha256,
  'Retained package bytes do not match the passing Pipeline record.');
  return {artifact, bytes: stat.size, sha256: record.package.sha256};
}

function notes(root, tag, commit, artifactSha) {
  const versionTags = git(root, 'tag', '--list', 'v*').split('\n').filter(Boolean).filter(item => item !== tag);
  let previous = '';
  if (versionTags.length) {
    const result = spawnSync('git', ['-C', root, 'describe', '--tags', '--match', 'v*', '--abbrev=0', `${tag}^`], {encoding: 'utf8'});
    requireTrue(result.status === 0 && result.stdout.trim(), 'Full release history is needed to identify the previous version tag.');
    previous = result.stdout.trim();
  }
  const changes = previous ? git(root, 'log', '--pretty=format:- %s', `${previous}..${tag}`) : '- Initial release.';
  return `# px4xplane ${tag}\n\n## Changes\n\n${changes || '- No commits since the previous tag.'}\n\n## Build\n\n- Source commit: ${commit}\n- Qualified output: macOS arm64\n- Package SHA-256: ${artifactSha}\n\n<!-- sprig-package source=${commit} sha256=${artifactSha} -->\n`;
}

function generate([sourceArg, runArg, tag, workflowArg, action, taskArg, ...options]) {
  requireTrue(sourceArg && runArg && tag && workflowArg && taskArg && ['stage', 'publish'].includes(action),
    'Usage: generate SOURCE PACKAGE_RUN TAG NEW_WORKFLOW stage|publish TASK_BINARY [--auth-home PATH] [--gh-config-dir PATH]');
  requireTrue(validTag(tag), 'Select a version or explicit dev prerelease tag.');
  const source = fs.realpathSync(sourceArg), run = fs.realpathSync(runArg);
  const workflow = path.resolve(workflowArg), task = fs.realpathSync(taskArg);
  requireTrue(!fs.existsSync(workflow) && !workflow.startsWith(`${source}${path.sep}`),
    'Choose a new workflow directory outside the source checkout.');
  requireTrue(!git(source, 'status', '--porcelain', '--untracked-files=normal'), 'Source checkout must be clean.');
  requireTrue(git(source, 'rev-parse', '--is-shallow-repository') === 'false', 'Full source history is required for release notes.');
  const commit = git(source, 'rev-parse', 'HEAD');
  requireTrue(git(source, 'rev-parse', `${tag}^{commit}`) === commit, 'Selected tag must identify the exact source commit.');
  const packageRecordSha256 = fileSha(path.join(run, 'record.json'));
  const asset = packageEvidence(run, commit, packageRecordSha256);
  const auth = {};
  for (let i = 0; i < options.length; i += 2) {
    requireTrue(i + 1 < options.length && ['--auth-home', '--gh-config-dir'].includes(options[i]), 'Unsupported credential route option.');
    const key = options[i] === '--auth-home' ? 'ghHomeDirectory' : 'ghConfigDirectory';
    requireTrue(!auth[key], 'Duplicate credential route option.');
    auth[key] = fs.realpathSync(options[i + 1]);
    requireTrue(fs.statSync(auth[key]).isDirectory(), 'Credential route must be an existing directory.');
  }
  const releaseNotes = notes(source, tag, commit, asset.sha256);
  const request = {schemaVersion: 1, repository, tag, sourceCommit: commit, action,
    packageRunDirectory: run, packageRecordSha256, asset: {bytes: asset.bytes, sha256: asset.sha256},
    notes: releaseNotes, notesSha256: sha(Buffer.from(releaseNotes)), ...auth};
  const contract = readJson(path.join(source, 'pipeline/workflows/local-build/environment.json'));
  requireTrue(contract.contractVersion === 'pipeline.local-workflow/v2', 'Unsupported source workflow contract.');
  const implementation = 'scripts/local/managed-release-workflow.mjs';
  requireTrue(git(source, 'ls-files', '--error-unmatch', '--', implementation) === implementation &&
    contract.paths.sourceInputs.some(input => input === implementation ||
      (input.endsWith('/**') && implementation.startsWith(input.slice(0, -3) + '/'))),
    'Commit the release workflow implementation before generating a candidate.');
  contract.profile = {...contract.profile, id: profile};
  contract.tools.task = {version: '3.53.1', executable: task, executableSha256: fileSha(task)};
  contract.entryPoints = {};
  for (const entry of ['check:fast', 'check', 'package']) {
    const folder = entry.replace(':', '-');
    contract.entryPoints[entry] = {finite: true, timeoutSeconds: 600,
      requiredSuites: [{id: 'release.receipt', minimumAssertions: 1,
        report: `.pipeline-state/reports/${folder}/release.json`}],
      ...(entry === 'package' ? {sourceMode: 'complete-commit', maximumArtifactMiB: 10,
        artifact: '.pipeline-state/packages/release-receipt.tar.gz'} : {})};
  }
  fs.mkdirSync(workflow, {recursive: true, mode: 0o700});
  fs.mkdirSync(path.join(workflow, 'scripts'), {mode: 0o700});
  fs.writeFileSync(path.join(workflow, 'environment.json'), `${JSON.stringify(contract, null, 2)}\n`, {mode: 0o600});
  fs.writeFileSync(path.join(workflow, 'scripts/request.json'), `${JSON.stringify(request, null, 2)}\n`, {mode: 0o600});
  fs.writeFileSync(path.join(workflow, 'Taskfile.yml'), `version: '3'\ntasks:\n${['check:fast', 'check', 'package'].map(entry =>
    `  ${entry}:\n    cmds:\n      - node "$PIPELINE_SOURCE_DIR/scripts/local/managed-release-workflow.mjs" execute ${entry}\n`).join('')}`, {mode: 0o600});
  return {workflow, tag, sourceCommit: commit, action, packageSha256: asset.sha256,
    requestSha256: fileSha(path.join(workflow, 'scripts/request.json')), taskSha256: fileSha(task),
    mutationEntry: 'package', automaticPublication: false};
}

function gh(args, request) {
  const env = {...process.env};
  for (const name of ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_OAUTH_TOKEN']) delete env[name];
  if (request.ghHomeDirectory) env.HOME = request.ghHomeDirectory;
  if (request.ghConfigDirectory) env.GH_CONFIG_DIR = request.ghConfigDirectory;
  const result = spawnSync('gh', args, {env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
  requireTrue(result.status === 0, `GitHub CLI ${args[0]} failed (${result.status ?? 'signal'}); inspect the local credential route and remote release state.`);
  return result.stdout.trim();
}

function remoteRelease(request, expectedDraft) {
  const release = JSON.parse(gh(['api', `repos/${repository}/releases/tags/${request.tag}`], request));
  const asset = (release.assets ?? []).find(item => item.name === packageName);
  const remoteCommit = JSON.parse(gh(['api', `repos/${repository}/commits/${request.tag}`], request)).sha;
  requireTrue(remoteCommit === request.sourceCommit && release.tag_name === request.tag &&
    typeof release.html_url === 'string' && release.html_url.startsWith('https://') &&
    release.draft === expectedDraft && asset?.size === request.asset.bytes &&
    asset?.digest === `sha256:${request.asset.sha256}` &&
    release.body?.includes(`<!-- sprig-package source=${request.sourceCommit} sha256=${request.asset.sha256} -->`),
  'Remote release, tag, notes or package digest differs from the selected candidate.');
  return {url: release.html_url, draft: release.draft, asset: asset.name};
}

function execute(entry) {
  requireTrue(['check:fast', 'check', 'package'].includes(entry), 'Unsupported Pipeline release entry.');
  const source = process.env.PIPELINE_SOURCE_DIR, control = process.env.PIPELINE_WORKFLOW_DIR;
  requireTrue(source && control && process.env.PIPELINE_RUN_REQUEST, 'Run through an explicit Pipeline workflow.');
  const request = readJson(path.join(control, 'scripts/request.json'));
  const preparation = readJson(process.env.PIPELINE_RUN_REQUEST);
  requireTrue(request.schemaVersion === 1 && request.repository === repository && validTag(request.tag) &&
    hex(request.sourceCommit, 40) && ['stage', 'publish'].includes(request.action) &&
    preparation.source?.clean === true && preparation.source?.commit === request.sourceCommit,
  'Managed source snapshot differs from the reviewed release request.');
  requireTrue(sha(Buffer.from(request.notes)) === request.notesSha256, 'Release notes changed after review.');
  const asset = packageEvidence(request.packageRunDirectory, request.sourceCommit, request.packageRecordSha256);
  requireTrue(asset.bytes === request.asset.bytes && asset.sha256 === request.asset.sha256,
    'Selected package differs from the reviewed release request.');
  let remote = null;
  if (entry === 'check') remote = remoteRelease(request, request.action === 'stage');
  if (entry === 'package') {
    requireTrue(preparation.source.clean, 'Remote publication requires clean source.');
    const remoteCommit = JSON.parse(gh(['api', `repos/${repository}/commits/${request.tag}`], request)).sha;
    requireTrue(remoteCommit === request.sourceCommit, 'Remote tag does not select the qualified source.');
    if (request.action === 'stage') {
      const packageDir = path.join(source, '.pipeline-state/packages');
      fs.mkdirSync(packageDir, {recursive: true});
      const selectedAsset = path.join(packageDir, packageName);
      fs.copyFileSync(asset.artifact, selectedAsset);
      requireTrue(fileSha(selectedAsset) === asset.sha256, 'Selected package changed during copy.');
      const notesFile = path.join(packageDir, 'release-notes.md');
      fs.writeFileSync(notesFile, request.notes, {mode: 0o600});
      const args = ['release', 'create', request.tag, selectedAsset, '--repo', repository,
        '--title', `px4xplane ${request.tag}`, '--notes-file', notesFile, '--draft', '--verify-tag'];
      if (request.tag.startsWith('dev-') || /alpha|beta|rc/.test(request.tag)) args.push('--prerelease');
      gh(args, request);
      remote = remoteRelease(request, true);
    } else {
      remoteRelease(request, true);
      gh(['release', 'edit', request.tag, '--repo', repository, '--draft=false'], request);
      remote = remoteRelease(request, false);
    }
  }
  const folder = path.join(source, '.pipeline-state/reports', entry.replace(':', '-'));
  fs.mkdirSync(folder, {recursive: true});
  const receipt = {schemaVersion: 1, outcome: 'passed', operation: entry === 'check:fast' ? 'plan' :
    entry === 'check' ? 'verify' : request.action, tag: request.tag,
    sourceCommit: request.sourceCommit, packageSha256: asset.sha256,
    packageRecordSha256: request.packageRecordSha256, remote};
  fs.writeFileSync(path.join(folder, 'release.json'), `${JSON.stringify(receipt, null, 2)}\n`);
  fs.writeFileSync(path.join(folder, 'run.json'), JSON.stringify({outcome: 'passed', entry, profile,
    counts: {'release.receipt': 1}}));
  if (entry === 'package') {
    const packages = path.join(source, '.pipeline-state/packages');
    fs.mkdirSync(packages, {recursive: true});
    const tar = spawnSync('/usr/bin/tar', ['-czf', path.join(packages, 'release-receipt.tar.gz'),
      '-C', folder, 'release.json'], {stdio: 'ignore'});
    requireTrue(tar.status === 0, 'Release receipt archive could not be retained.');
  }
  return {outcome: 'passed', operation: receipt.operation, tag: request.tag,
    sourceCommit: request.sourceCommit, packageSha256: asset.sha256, remote};
}

export {generate, execute};
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve('scripts/local/managed-release-workflow.mjs')) {
  const command = process.argv[2];
  const result = command === 'generate' ? generate(process.argv.slice(3)) :
    command === 'execute' ? execute(process.argv[3]) : null;
  requireTrue(result, 'Use generate or execute.');
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
