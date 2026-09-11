const http = require('http');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const BASE = path.join(process.env.HOME, 'zamal-ai-operator');
const CONFIG = path.join(BASE, 'config', 'cpc_project_path.txt');
const PORT = 8787;

function readProjectPath() {
  if (!fs.existsSync(CONFIG)) throw new Error('Missing config/cpc_project_path.txt');
  const p = fs.readFileSync(CONFIG, 'utf8').trim();
  if (!p || !fs.existsSync(p) || !fs.statSync(p).isDirectory()) {
    throw new Error('Invalid CPC project path: ' + p);
  }
  if (!p.startsWith(process.env.HOME + path.sep)) {
    throw new Error('Project path must be inside HOME');
  }
  return p;
}

function safeRead(project, relativePath, maxChars = 12000) {
  const full = path.resolve(project, relativePath);
  if (!full.startsWith(path.resolve(project) + path.sep) && full !== path.resolve(project)) {
    return null;
  }
  if (!fs.existsSync(full) || !fs.statSync(full).isFile()) return null;
  const txt = fs.readFileSync(full, 'utf8');
  return txt.slice(0, maxChars);
}

function runGit(project, args) {
  try {
    return execFileSync('git', args, {
      cwd: project,
      encoding: 'utf8',
      timeout: 5000,
      env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' }
    }).trim();
  } catch (e) {
    return '';
  }
}

function listFiles(project, limit = 220) {
  const skip = new Set(['.git', 'node_modules', 'dist', 'build', '.next', '.cache', 'coverage']);
  const out = [];

  function walk(dir, rel, depth) {
    if (out.length >= limit || depth > 4) return;
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    entries.sort((a, b) => a.name.localeCompare(b.name));

    for (const entry of entries) {
      if (out.length >= limit) break;
      if (skip.has(entry.name)) continue;

      const full = path.join(dir, entry.name);
      const r = path.join(rel, entry.name);

      if (entry.isDirectory()) {
        out.push(r + '/');
        walk(full, r, depth + 1);
      } else {
        out.push(r);
      }
    }
  }

  walk(project, '', 0);
  return out;
}

function getPackageInfo(project) {
  const raw = safeRead(project, 'package.json', 50000);
  if (!raw) return null;
  try {
    const json = JSON.parse(raw);
    return {
      name: json.name || null,
      version: json.version || null,
      scripts: json.scripts || {},
      dependenciesCount: json.dependencies ? Object.keys(json.dependencies).length : 0,
      devDependenciesCount: json.devDependencies ? Object.keys(json.devDependencies).length : 0
    };
  } catch {
    return { error: 'package.json parse failed' };
  }
}

function getDocs(project) {
  const files = [
    'README.md',
    'TASK.md',
    'CHANGELOG.md',
    'DECISIONS.md',
    'HANDOVER.md',
    'PROJECT_KNOWLEDGE_BASE.md',
    'ROADMAP.md',
    'docs/TASK.md',
    'docs/CHANGELOG.md',
    'docs/DECISIONS.md',
    'docs/HANDOVER.md',
    'docs/PROJECT_KNOWLEDGE_BASE.md',
    'docs/ROADMAP.md'
  ];

  const docs = {};
  for (const f of files) {
    const txt = safeRead(project, f, 9000);
    if (txt) docs[f] = txt;
  }
  return docs;
}

function summary() {
  const project = readProjectPath();
  const isGit = fs.existsSync(path.join(project, '.git'));

  return {
    ok: true,
    mode: 'READ_ONLY',
    warning: 'This server only reads CPC project files. It does not edit, delete, commit, push, install, or run project code.',
    projectPath: project,
    generatedAt: new Date().toISOString(),
    git: isGit ? {
      branch: runGit(project, ['branch', '--show-current']),
      lastCommit: runGit(project, ['log', '-1', '--oneline']),
      statusShortNoUntracked: runGit(project, ['status', '--short', '--untracked-files=no']),
      remote: runGit(project, ['remote', '-v']).split('\n').slice(0, 4)
    } : null,
    package: getPackageInfo(project),
    importantDocs: getDocs(project),
    structureSample: listFiles(project)
  };
}

const server = http.createServer((req, res) => {
  try {
    if (req.url === '/health') {
      const project = readProjectPath();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        ok: true,
        service: 'CPC Read-Only Inspector',
        projectPath: project,
        readOnly: true
      }, null, 2));
      return;
    }

    if (req.url === '/summary') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(summary(), null, 2));
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'Use /health or /summary' }, null, 2));
  } catch (e) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: e.message }, null, 2));
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('✅ CPC Read-Only Inspector running');
  console.log('Health:  http://127.0.0.1:8787/health');
  console.log('Summary: http://127.0.0.1:8787/summary');
  console.log('');
  console.log('Read-only mode: no edit, no delete, no commit, no push.');
});
