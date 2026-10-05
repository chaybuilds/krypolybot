import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import type { McpClientId, McpClientSetup } from '../../shared/types';

export const SERVER_KEY = 'krypt-polybot';

export interface RelaySpec {
  command: string;
  args: string[];
  env: Record<string, string>;
}

function claudeDesktopConfig(): string {
  const home = homedir();
  if (process.platform === 'win32') {
    return join(process.env.APPDATA || join(home, 'AppData', 'Roaming'),
      'Claude', 'claude_desktop_config.json');
  }
  if (process.platform === 'darwin') {
    return join(home, 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json');
  }
  return join(process.env.XDG_CONFIG_HOME || join(home, '.config'),
    'Claude', 'claude_desktop_config.json');
}

function codexConfig(): string {
  return join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'config.toml');
}

const JSON_TARGETS: Partial<Record<McpClientId, () => string>> = {
  'claude-desktop': claudeDesktopConfig,
  cursor: () => join(homedir(), '.cursor', 'mcp.json'),
  windsurf: () => join(homedir(), '.codeium', 'windsurf', 'mcp_config.json'),
};

function stdioEntry(spec: RelaySpec): Record<string, unknown> {
  return { command: spec.command, args: spec.args, env: spec.env };
}

function shellQuote(s: string): string {
  if (process.platform === 'win32') return `"${s.replace(/"/g, '\\"')}"`;
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

function codexBlock(spec: RelaySpec): string {
  const env = Object.entries(spec.env)
    .map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(', ');
  return [
    `[mcp_servers.${SERVER_KEY}]`,
    `command = ${JSON.stringify(spec.command)}`,
    `args = [${spec.args.map((a) => JSON.stringify(a)).join(', ')}]`,
    `env = { ${env} }`,
    'tool_timeout_sec = 600',
    'startup_timeout_sec = 30',
  ].join('\n');
}

const CODEX_TABLE = new RegExp(
  `^\\s*\\[\\s*mcp_servers\\.(?:"${SERVER_KEY}"|${SERVER_KEY})(?:\\.[^\\]]*)?\\s*\\]`);

function codexHasBlock(text: string): boolean {
  return text.split(/\r?\n/).some((l) => CODEX_TABLE.test(l));
}

function stripCodexBlock(text: string): string {
  const lines = text.split(/\r?\n/);
  const out: string[] = [];
  let skipping = false;
  for (const line of lines) {
    if (CODEX_TABLE.test(line)) { skipping = true; continue; }
    if (skipping && /^\s*\[/.test(line)) skipping = false;
    if (!skipping) out.push(line);
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd();
}

function readJson(path: string): Record<string, unknown> {
  if (!existsSync(path)) return {};
  const raw = readFileSync(path, 'utf8');
  if (!raw.trim()) return {};
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('the file is not a JSON object');
  }
  return parsed as Record<string, unknown>;
}

function backupOnce(path: string): void {
  const bak = `${path}.krypt-backup`;
  if (existsSync(path) && !existsSync(bak)) copyFileSync(path, bak);
}

function jsonInstalled(path: string): boolean {
  try {
    const servers = readJson(path).mcpServers as Record<string, unknown> | undefined;
    return !!servers && typeof servers === 'object' && SERVER_KEY in servers;
  } catch {
    return false;
  }
}

export function listClients(spec: RelaySpec | null, httpUrl: string): McpClientSetup[] {
  const missing = 'The backend binary could not be located, so no launch command can be generated.';
  const entry = spec ? stdioEntry(spec) : null;
  const jsonSnippet = entry
    ? JSON.stringify({ mcpServers: { [SERVER_KEY]: entry } }, null, 2) : missing;

  const out: McpClientSetup[] = [];
  const jsonClient = (id: McpClientId, label: string, note?: string) => {
    const path = JSON_TARGETS[id]!();
    out.push({
      id, label, configPath: path,
      detected: existsSync(dirname(path)),
      installed: jsonInstalled(path),
      snippet: jsonSnippet, snippetKind: 'json', note,
    });
  };

  jsonClient('claude-desktop', 'Claude Desktop',
    'Restart Claude Desktop after installing. The tools appear under the connectors (plug) menu.');

  const envFlags = spec
    ? Object.entries(spec.env).map(([k, v]) => `--env ${k}=${shellQuote(v)}`).join(' ') : '';
  out.push({
    id: 'claude-code', label: 'Claude Code', configPath: null,
    detected: existsSync(join(homedir(), '.claude')),
    installed: false,
    snippet: spec
      ? `claude mcp add ${SERVER_KEY} --scope user ${envFlags} -- ${shellQuote(spec.command)} ${spec.args.map(shellQuote).join(' ')}`
      : missing,
    snippetKind: 'shell',
    note: `Run this once in a terminal. Or connect over HTTP: claude mcp add --transport http ${SERVER_KEY} ${httpUrl} --header "Authorization: Bearer <token>"`,
  });

  jsonClient('cursor', 'Cursor',
    'Cursor → Settings → MCP shows the server once installed. Cursor uses at most 40 tools across all servers.');

  const codexPath = codexConfig();
  let codexInstalled = false;
  try {
    codexInstalled = existsSync(codexPath) && codexHasBlock(readFileSync(codexPath, 'utf8'));
  } catch {}
  out.push({
    id: 'codex', label: 'Codex (OpenAI)', configPath: codexPath,
    detected: existsSync(dirname(codexPath)),
    installed: codexInstalled,
    snippet: spec ? codexBlock(spec) : missing, snippetKind: 'toml',
    note: 'Used by the Codex CLI and the Codex IDE extension.',
  });

  jsonClient('windsurf', 'Windsurf');

  out.push({
    id: 'vscode', label: 'VS Code (Copilot agent mode)', configPath: null,
    detected: false, installed: false,
    snippet: entry
      ? JSON.stringify({ servers: { [SERVER_KEY]: { type: 'stdio', ...entry } } }, null, 2)
      : missing,
    snippetKind: 'json',
    note: 'Command Palette → "MCP: Open User Configuration", then merge this in.',
  });
  return out;
}

export function install(id: McpClientId, spec: RelaySpec | null): { ok: boolean; message: string } {
  if (!spec) return { ok: false, message: 'The backend binary could not be located.' };
  try {
    if (id === 'codex') {
      const path = codexConfig();
      mkdirSync(dirname(path), { recursive: true });
      const prev = existsSync(path) ? readFileSync(path, 'utf8') : '';
      backupOnce(path);
      const base = stripCodexBlock(prev);
      writeFileSync(path, `${base ? `${base}\n\n` : ''}${codexBlock(spec)}\n`, 'utf8');
      return { ok: true, message: `Added to ${path}. Restart Codex to pick it up.` };
    }
    const target = JSON_TARGETS[id];
    if (!target) return { ok: false, message: 'This client is set up by copy/paste.' };
    const path = target();
    let cfg: Record<string, unknown>;
    try {
      cfg = readJson(path);
    } catch (e) {
      return {
        ok: false,
        message: `${path} could not be parsed (${(e as Error).message}), so it was left alone. Paste the snippet in by hand.`,
      };
    }
    const servers = (cfg.mcpServers && typeof cfg.mcpServers === 'object'
      ? cfg.mcpServers : {}) as Record<string, unknown>;
    servers[SERVER_KEY] = stdioEntry(spec);
    cfg.mcpServers = servers;
    mkdirSync(dirname(path), { recursive: true });
    backupOnce(path);
    writeFileSync(path, `${JSON.stringify(cfg, null, 2)}\n`, 'utf8');
    return { ok: true, message: `Added to ${path}. Restart the app to pick it up.` };
  } catch (e) {
    return { ok: false, message: `Could not write the config: ${(e as Error).message}` };
  }
}

export function uninstall(id: McpClientId): { ok: boolean; message: string } {
  try {
    if (id === 'codex') {
      const path = codexConfig();
      if (!existsSync(path)) return { ok: true, message: 'Nothing to remove.' };
      const stripped = stripCodexBlock(readFileSync(path, 'utf8'));
      writeFileSync(path, stripped ? `${stripped}\n` : '', 'utf8');
      return { ok: true, message: `Removed from ${path}.` };
    }
    const target = JSON_TARGETS[id];
    if (!target) return { ok: false, message: 'This client is set up by copy/paste.' };
    const path = target();
    if (!existsSync(path)) return { ok: true, message: 'Nothing to remove.' };
    const cfg = readJson(path);
    const servers = cfg.mcpServers as Record<string, unknown> | undefined;
    if (servers && typeof servers === 'object') delete servers[SERVER_KEY];
    writeFileSync(path, `${JSON.stringify(cfg, null, 2)}\n`, 'utf8');
    return { ok: true, message: `Removed from ${path}.` };
  } catch (e) {
    return { ok: false, message: `Could not edit the config: ${(e as Error).message}` };
  }
}
