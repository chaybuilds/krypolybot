import {
  copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statSync, writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import type { McpClientId, McpClientSetup, McpInstallResult } from '../../shared/types';
import { serverName } from '../../shared/agents';

export interface RelaySpec {
  command: string;
  args: string[];
  env: Record<string, string>;
}

export interface AgentRef {
  id: string;
  name: string;
}

export interface PathEnv {
  platform: NodeJS.Platform;
  home: string;
  appData?: string;
  localAppData?: string;
  xdgConfig?: string;
  codexHome?: string;
}

export function systemPathEnv(): PathEnv {
  return {
    platform: process.platform,
    home: homedir(),
    appData: process.env.APPDATA,
    localAppData: process.env.LOCALAPPDATA,
    xdgConfig: process.env.XDG_CONFIG_HOME,
    codexHome: process.env.CODEX_HOME,
  };
}

export const CLAUDE_DESKTOP_FILE = 'claude_desktop_config.json';

export function claudeDesktopConfigDirs(env: PathEnv): string[] {
  if (env.platform === 'darwin') {
    return [join(env.home, 'Library', 'Application Support', 'Claude')];
  }
  if (env.platform === 'win32') {
    const dirs = [join(env.appData || join(env.home, 'AppData', 'Roaming'), 'Claude')];
    if (env.localAppData) {
      const pkgs = join(env.localAppData, 'Packages');
      try {
        for (const name of readdirSync(pkgs)) {
          if (/^Claude_/i.test(name)) dirs.push(join(pkgs, name, 'LocalCache', 'Roaming', 'Claude'));
        }
      } catch { }
    }
    return dirs;
  }
  return [join(env.xdgConfig || join(env.home, '.config'), 'Claude')];
}

const isDir = (d: string): boolean => {
  try { return statSync(d).isDirectory(); } catch { return false; }
};

export function existingClaudeDesktopDirs(env: PathEnv): string[] {
  return claudeDesktopConfigDirs(env).filter(isDir);
}

export function codexConfigDir(env: PathEnv): string {
  return env.codexHome && env.codexHome.trim() ? env.codexHome : join(env.home, '.codex');
}

function jsonFile(id: McpClientId, env: PathEnv): string | null {
  if (id === 'claude-desktop') return join(claudeDesktopConfigDirs(env)[0], CLAUDE_DESKTOP_FILE);
  if (id === 'cursor') return join(env.home, '.cursor', 'mcp.json');
  if (id === 'windsurf') return join(env.home, '.codeium', 'windsurf', 'mcp_config.json');
  return null;
}

function jsonFiles(id: McpClientId, env: PathEnv): string[] {
  if (id === 'claude-desktop') {
    const dirs = existingClaudeDesktopDirs(env);
    return dirs.length ? dirs.map((d) => join(d, CLAUDE_DESKTOP_FILE)) : [jsonFile(id, env)!];
  }
  const f = jsonFile(id, env);
  return f ? [f] : [];
}

export function configFolder(id: McpClientId, env: PathEnv): string | null {
  if (id === 'claude-desktop') return existingClaudeDesktopDirs(env)[0] ?? null;
  if (id === 'codex') return codexConfigDir(env);
  const f = jsonFile(id, env);
  return f ? dirname(f) : null;
}

export function agentSpec(spec: RelaySpec, agent: AgentRef): RelaySpec {
  return { command: spec.command, args: [...spec.args, '--agent', agent.id], env: { ...spec.env } };
}

function stdioEntry(spec: RelaySpec): Record<string, unknown> {
  return { command: spec.command, args: spec.args, env: spec.env };
}

function shellQuote(s: string, platform: NodeJS.Platform): string {
  if (platform === 'win32') return `"${s.replace(/"/g, '\\"')}"`;
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

export function codexBlock(spec: RelaySpec, name: string): string {
  const env = Object.entries(spec.env)
    .map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(', ');
  return [
    `[mcp_servers.${name}]`,
    `command = ${JSON.stringify(spec.command)}`,
    `args = [${spec.args.map((a) => JSON.stringify(a)).join(', ')}]`,
    `env = { ${env} }`,
    'tool_timeout_sec = 600',
    'startup_timeout_sec = 30',
  ].join('\n');
}

export type MergeResult = { ok: true; text: string; replaced: boolean } | { ok: false; message: string };

function parseJsonDoc(existing: string | null, label: string): { ok: true; doc: Record<string, unknown>; raw: string } | { ok: false; message: string } {
  const raw = (existing ?? '').replace(/^\uFEFF/, '');
  if (!raw.trim()) return { ok: true, doc: {}, raw };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {
      ok: false,
      message: `Your ${label} config file is not valid JSON, so the app left it alone rather than risk `
        + 'your other settings. Open the config folder, fix or remove the file, and try again (or copy the '
        + 'setup and paste it in by hand).',
    };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, message: `Your ${label} config file is not in the shape ${label} uses, so the app left it alone.` };
  }
  const servers = (parsed as Record<string, unknown>).mcpServers;
  if (servers !== undefined && (servers === null || typeof servers !== 'object' || Array.isArray(servers))) {
    return {
      ok: false,
      message: `The "mcpServers" section of your ${label} config is not a list of servers, so the app left `
        + 'the file alone. Open the config folder to look at it, or copy the setup by hand.',
    };
  }
  return { ok: true, doc: parsed as Record<string, unknown>, raw };
}

function serialise(doc: Record<string, unknown>, raw: string): string {
  const nl = raw.includes('\r\n') ? '\r\n' : '\n';
  return JSON.stringify(doc, null, 2).replace(/\n/g, nl) + nl;
}

export function mergeJsonServer(existing: string | null, name: string, entry: unknown, label = 'that app'): MergeResult {
  const p = parseJsonDoc(existing, label);
  if (!p.ok) return p;
  const cur = (p.doc.mcpServers as Record<string, unknown> | undefined) ?? {};
  const replaced = name in cur;
  const out = { ...p.doc, mcpServers: { ...cur, [name]: entry } };
  return { ok: true, text: serialise(out, p.raw), replaced };
}

export function removeJsonServer(existing: string | null, name: string, label = 'that app'): MergeResult {
  const p = parseJsonDoc(existing, label);
  if (!p.ok) return p;
  const cur = { ...((p.doc.mcpServers as Record<string, unknown> | undefined) ?? {}) };
  const replaced = name in cur;
  delete cur[name];
  return { ok: true, text: serialise({ ...p.doc, mcpServers: cur }, p.raw), replaced };
}

function jsonHas(file: string, name: string): boolean {
  try {
    const servers = JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, '')).mcpServers;
    return !!servers && typeof servers === 'object' && name in servers;
  } catch {
    return false;
  }
}

export function codexServerName(block: string): string | null {
  const m = /^\s*\[\s*mcp_servers\.("?)([A-Za-z0-9_-]+)\1\s*\]/m.exec(block);
  return m ? m[2] : null;
}

function isOwnHeader(line: string, name: string): boolean {
  const m = /^\s*\[\s*mcp_servers\.("?)([A-Za-z0-9_-]+)\1(\.[^\]]*)?\s*\]\s*(#.*)?$/.exec(line);
  return !!m && m[2] === name;
}

function withoutTable(raw: string, name: string): { kept: string[]; at: number; found: boolean } {
  const lines = raw.length ? raw.split(/\r?\n/) : [];
  const kept: string[] = [];
  let inOwn = false;
  let found = false;
  let at = -1;
  for (const line of lines) {
    if (/^\s*\[/.test(line)) {
      if (isOwnHeader(line, name)) {
        if (!inOwn && at < 0) at = kept.length;
        inOwn = true;
        found = true;
        continue;
      }
      inOwn = false;
    }
    if (!inOwn) kept.push(line);
  }
  return { kept, at, found };
}

export function mergeCodexToml(existing: string | null, block: string): MergeResult {
  const name = codexServerName(block);
  if (!name) return { ok: false, message: 'The app could not build the Codex entry. Nothing was changed.' };
  const raw = (existing ?? '').replace(/^\uFEFF/, '');
  const nl = raw.includes('\r\n') ? '\r\n' : '\n';
  const { kept, at, found } = withoutTable(raw, name);
  const rest = kept.join('\n');
  const esc = name.replace(/[-]/g, '\\-');
  if (new RegExp(`mcp_servers\\s*\\.\\s*"?${esc}"?\\s*[.=]|^\\s*"?${esc}"?\\s*=`, 'm').test(rest)) {
    return {
      ok: false,
      message: `Your Codex config already defines "${name}" in a form the app can't safely edit, so it `
        + 'left the file alone. Open the config folder and remove that entry, or copy the setup by hand.',
    };
  }
  const ours = block.replace(/\r\n/g, '\n').replace(/\n+$/, '').split('\n');
  let outLines: string[];
  if (at >= 0) {
    const before = kept.slice(0, at);
    const after = kept.slice(at);
    while (after.length && !after[0].trim()) after.shift();
    outLines = [...before, ...ours, ...(after.length ? ['', ...after] : [])];
  } else {
    const base = [...kept];
    while (base.length && !base[base.length - 1].trim()) base.pop();
    outLines = base.length ? [...base, '', ...ours] : ours;
  }
  while (outLines.length && !outLines[outLines.length - 1].trim()) outLines.pop();
  return { ok: true, text: outLines.join(nl) + nl, replaced: found };
}

export function removeCodexTable(existing: string | null, name: string): MergeResult {
  const raw = (existing ?? '').replace(/^\uFEFF/, '');
  const nl = raw.includes('\r\n') ? '\r\n' : '\n';
  const { kept, found } = withoutTable(raw, name);
  const lines = [...kept];
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  const text = lines.join(nl).replace(new RegExp(`(${nl}){3,}`, 'g'), nl + nl);
  return { ok: true, text: text ? text + nl : '', replaced: found };
}

function codexHas(file: string, name: string): boolean {
  try {
    return readFileSync(file, 'utf8').split(/\r?\n/).some((l) => isOwnHeader(l, name));
  } catch {
    return false;
  }
}

export function backupPath(file: string, now = new Date()): string {
  const stamp = now.toISOString().replace(/\.\d+Z$/, '').replace(/:/g, '-');
  return `${file}.${stamp}.bak`;
}

export type WriteResult = { ok: true; backup: string | null } | { ok: false; message: string };

export function writeWithBackup(file: string, text: string, now = new Date()): WriteResult {
  try {
    mkdirSync(dirname(file), { recursive: true });
    let backup: string | null = null;
    if (existsSync(file)) {
      backup = backupPath(file, now);
      copyFileSync(file, backup);
    }
    const tmp = `${file}.krypt-tmp`;
    writeFileSync(tmp, text, 'utf-8');
    renameSync(tmp, file);
    return { ok: true, backup };
  } catch (e) {
    const code = (e as NodeJS.ErrnoException)?.code;
    return {
      ok: false,
      message: code === 'EACCES' || code === 'EPERM' || code === 'EBUSY'
        ? `The app wasn't allowed to write ${file}. If that program is open, quit it and try again.`
        : `The app couldn't write ${file}. Nothing else was changed.`,
    };
  }
}

function readOrNull(file: string): string | null {
  try { return existsSync(file) ? readFileSync(file, 'utf-8') : null; } catch { return null; }
}

const LABEL: Record<McpClientId, string> = {
  'claude-desktop': 'Claude Desktop',
  'claude-code': 'Claude Code',
  cursor: 'Cursor',
  codex: 'Codex (OpenAI)',
  windsurf: 'Windsurf',
  vscode: 'VS Code (Copilot agent mode)',
};

const INSTALLABLE = new Set<McpClientId>(['claude-desktop', 'cursor', 'windsurf', 'codex']);

export function snippetFor(id: McpClientId, base: RelaySpec, agent: AgentRef, platform: NodeJS.Platform = process.platform): string {
  const spec = agentSpec(base, agent);
  const name = serverName(agent);
  const entry = stdioEntry(spec);
  if (id === 'codex') return codexBlock(spec, name);
  if (id === 'claude-code') {
    const envFlags = Object.entries(spec.env).map(([k, v]) => `--env ${k}=${shellQuote(v, platform)}`).join(' ');
    return `claude mcp add ${name} --scope user ${envFlags} -- ${shellQuote(spec.command, platform)} `
      + spec.args.map((a) => shellQuote(a, platform)).join(' ');
  }
  if (id === 'vscode') return JSON.stringify({ servers: { [name]: { type: 'stdio', ...entry } } }, null, 2);
  return JSON.stringify({ mcpServers: { [name]: entry } }, null, 2);
}

const NOTE: Record<McpClientId, string> = {
  'claude-desktop': 'Restart Claude Desktop after adding (right-click its tray icon → Quit, then open it). The tools appear under the connectors (plug) menu.',
  'claude-code': 'Run this once in a terminal. Claude Code picks it up in new sessions.',
  cursor: 'Cursor → Settings → MCP shows the server once added. Cursor uses at most 40 tools across all servers.',
  codex: 'Used by the Codex CLI and the Codex IDE extension. Restart Codex after adding.',
  windsurf: 'Restart Windsurf after adding.',
  vscode: 'Command Palette → "MCP: Open User Configuration", then merge this in.',
};

export function listClients(spec: RelaySpec | null, agent: AgentRef, env: PathEnv = systemPathEnv()): McpClientSetup[] {
  const missing = 'The backend binary could not be located, so no launch command can be generated.';
  const name = serverName(agent);
  const ids: McpClientId[] = ['claude-desktop', 'claude-code', 'cursor', 'codex', 'windsurf', 'vscode'];
  return ids.map((id) => {
    let configPath: string | null = null;
    let configPaths: string[] | undefined;
    let detected = false;
    let installed = false;
    if (id === 'codex') {
      configPath = join(codexConfigDir(env), 'config.toml');
      detected = isDir(codexConfigDir(env));
      installed = codexHas(configPath, name);
    } else if (id === 'claude-code') {
      detected = isDir(join(env.home, '.claude'));
    } else if (id !== 'vscode') {
      configPaths = jsonFiles(id, env);
      configPath = configPaths[0] ?? null;
      detected = id === 'claude-desktop' ? existingClaudeDesktopDirs(env).length > 0
        : !!configPath && isDir(dirname(configPath));
      installed = configPaths.some((f) => jsonHas(f, name));
    }
    return {
      id, label: LABEL[id], agentId: agent.id, serverName: name,
      configPath, configPaths, detected, installed,
      canInstall: INSTALLABLE.has(id) && !!spec,
      snippet: spec ? snippetFor(id, spec, agent, env.platform) : missing,
      snippetKind: id === 'codex' ? 'toml' : id === 'claude-code' ? 'shell' : 'json',
      note: NOTE[id],
    };
  });
}

export function install(id: McpClientId, base: RelaySpec | null, agent: AgentRef,
  env: PathEnv = systemPathEnv(), now = new Date()): McpInstallResult {
  if (!base) return { ok: false, message: 'The backend binary could not be located.', files: [], backups: [] };
  if (!INSTALLABLE.has(id)) return { ok: false, message: 'This client is set up by copy/paste.', files: [], backups: [] };
  const spec = agentSpec(base, agent);
  const name = serverName(agent);
  if (id === 'codex') {
    const file = join(codexConfigDir(env), 'config.toml');
    const m = mergeCodexToml(readOrNull(file), codexBlock(spec, name));
    if (!m.ok) return { ok: false, message: m.message, files: [], backups: [] };
    const w = writeWithBackup(file, m.text, now);
    if (!w.ok) return { ok: false, message: w.message, files: [], backups: [] };
    return {
      ok: true, files: [file], backups: w.backup ? [w.backup] : [],
      message: `${m.replaced ? 'Updated' : 'Added'} "${name}" in Codex's config. Restart Codex so it picks it up.`,
    };
  }
  if (id === 'claude-desktop' && existingClaudeDesktopDirs(env).length === 0) {
    return {
      ok: false, files: [], backups: [],
      message: 'Claude Desktop doesn\'t seem to be installed on this computer (its settings folder isn\'t '
        + 'there). Install it, open it once, then press Add again.',
    };
  }
  const plans: { file: string; text: string }[] = [];
  for (const file of jsonFiles(id, env)) {
    const m = mergeJsonServer(readOrNull(file), name, stdioEntry(spec), LABEL[id]);
    if (!m.ok) return { ok: false, message: m.message, files: [], backups: [] };
    plans.push({ file, text: m.text });
  }
  const files: string[] = [];
  const backups: string[] = [];
  for (const p of plans) {
    const w = writeWithBackup(p.file, p.text, now);
    if (!w.ok) return { ok: files.length > 0, files, backups, message: w.message };
    files.push(p.file);
    if (w.backup) backups.push(w.backup);
  }
  return {
    ok: true, files, backups,
    message: `Added "${name}" to ${LABEL[id]}. ${id === 'claude-desktop'
      ? 'Now fully quit Claude Desktop (right-click its tray icon → Quit) and open it again — it only reads this file when it starts.'
      : `Restart ${LABEL[id]} to pick it up.`}`,
  };
}

export function uninstall(id: McpClientId, agent: AgentRef, env: PathEnv = systemPathEnv(), now = new Date()): McpInstallResult {
  if (!INSTALLABLE.has(id)) return { ok: false, message: 'This client is set up by copy/paste.', files: [], backups: [] };
  const name = serverName(agent);
  const targets = id === 'codex' ? [join(codexConfigDir(env), 'config.toml')] : jsonFiles(id, env);
  const plans: { file: string; text: string }[] = [];
  for (const file of targets) {
    const raw = readOrNull(file);
    if (raw === null) continue;
    const m = id === 'codex' ? removeCodexTable(raw, name) : removeJsonServer(raw, name, LABEL[id]);
    if (!m.ok) return { ok: false, message: m.message, files: [], backups: [] };
    if (m.replaced) plans.push({ file, text: m.text });
  }
  if (!plans.length) return { ok: true, message: 'Nothing to remove.', files: [], backups: [] };
  const files: string[] = [];
  const backups: string[] = [];
  for (const p of plans) {
    const w = writeWithBackup(p.file, p.text, now);
    if (!w.ok) return { ok: false, files, backups, message: w.message };
    files.push(p.file);
    if (w.backup) backups.push(w.backup);
  }
  return { ok: true, files, backups, message: `Removed "${name}" from ${LABEL[id]}. A backup of the old file was kept.` };
}
