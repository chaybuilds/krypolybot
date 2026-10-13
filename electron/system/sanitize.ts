import { cleanAgents, isValidAgentId } from '../../shared/agents';
import type { McpClientId } from '../../shared/types';

export function clampInt(v: unknown, lo: number, hi: number, dflt: number): number {
  if (v === null || v === undefined || v === '') return dflt;
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, Math.round(n)));
}

export const cleanMcpAgents = cleanAgents;

const TRADE_MODES = new Set(['off', 'paper', 'live']);

export function cleanConfigPatch<T extends Record<string, unknown>>(patch: T): T {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return {} as T;
  const out: Record<string, unknown> = { ...patch };
  if ('mcpAgents' in out) {
    if (Array.isArray(out.mcpAgents)) out.mcpAgents = cleanAgents(out.mcpAgents);
    else delete out.mcpAgents;
  }
  if ('autopilotAgentId' in out) {
    const id = String(out.autopilotAgentId ?? '').trim().toLowerCase();
    if (isValidAgentId(id)) out.autopilotAgentId = id;
    else delete out.autopilotAgentId;
  }
  if ('mcpTradeMode' in out && !TRADE_MODES.has(String(out.mcpTradeMode))) delete out.mcpTradeMode;
  if ('accountMode' in out && out.accountMode !== 'paper' && out.accountMode !== 'live') delete out.accountMode;
  if ('aiProvider' in out) {
    const p = cleanAiProvider(out.aiProvider);
    if (p) out.aiProvider = p;
    else delete out.aiProvider;
  }
  delete out.terminalPaperMode;
  for (const k of ['mcpLiveTradingEnabled', 'mcpDailyLimitUsd', 'aiAnthropicModel', 'aiOpenaiModel']) {
    delete out[k];
  }
  return out as T;
}

export function removedAgentIds(before: unknown, after: unknown): string[] {
  const ids = (l: unknown): string[] => (Array.isArray(l)
    ? l.map((a) => String((a as { id?: unknown })?.id ?? '').trim().toLowerCase()).filter(Boolean) : []);
  const keep = new Set(ids(after));
  return ids(before).filter((id) => !keep.has(id));
}

export function cleanAgentIdArg(v: unknown): string | null {
  if (typeof v !== 'string') return null;
  const id = v.trim().toLowerCase();
  return isValidAgentId(id) ? id : null;
}

export const MCP_CLIENT_IDS: readonly McpClientId[] = [
  'claude-desktop', 'claude-code', 'cursor', 'codex', 'windsurf', 'vscode',
];

export function cleanClientId(v: unknown): McpClientId | null {
  const s = String(v ?? '').trim();
  return (MCP_CLIENT_IDS as readonly string[]).includes(s) ? (s as McpClientId) : null;
}

export function cleanHealthArgs(raw: unknown): { deep: boolean } {
  if (raw === true) return { deep: true };
  const r = (raw && typeof raw === 'object' && !Array.isArray(raw))
    ? raw as Record<string, unknown> : {};
  return { deep: r.deep === true };
}

const AGENT_NEVER_KEYS = new Set([
  'accountMode', 'terminalPaperMode', 'terminalPaperBankrollUsd', 'mcpPaperBankrollUsd', 'network',
]);

export function filterAgentConfigPatch(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw as Record<string, unknown>).filter(([k]) =>
    !AGENT_NEVER_KEYS.has(k)
    && !/^(mcp|remote|ai|autopilot|account|terminalMax)/.test(k)
    && !/webhook|url|token|discord|telegram|key|secret|private|passphrase|funder/i.test(k)));
}

export function cleanAgentConfigPatch(
  raw: unknown,
  defaults: Record<string, unknown>,
  allowLiveSwitches: boolean,
  log: (key: string, why: string) => void = () => {},
): Record<string, unknown> {
  const src = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw as Record<string, unknown> : {};
  const kept = filterAgentConfigPatch(src);
  for (const k of Object.keys(src)) if (!(k in kept)) log(k, 'never patchable by an AI agent');
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(kept)) {
    const def = defaults[k];
    if (def === undefined) { log(k, 'not a known setting'); continue; }
    const typed = def === null ? (v === null || typeof v !== 'object' || Array.isArray(v))
      : Array.isArray(def) ? Array.isArray(v)
        : typeof def === 'object' ? (!!v && typeof v === 'object' && !Array.isArray(v))
          : typeof def === 'number' ? (typeof v === 'number' && Number.isFinite(v))
            : typeof v === typeof def;
    if (!typed) { log(k, `expected ${Array.isArray(def) ? 'array' : def === null ? 'a value' : typeof def}`); continue; }
    if (!allowLiveSwitches && v !== false) { log(k, 'whole-account powers are off for agents'); continue; }
    out[k] = v;
  }
  return out;
}

export const AI_PROVIDERS = [
  'anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio',
] as const;
export type AiProviderId = typeof AI_PROVIDERS[number];

export const AI_KEYED_PROVIDERS: readonly AiProviderId[] = ['anthropic', 'openai', 'openrouter', 'gemini'];

export function cleanAiProvider(v: unknown): AiProviderId | null {
  const p = String(v ?? '').trim().toLowerCase();
  return (AI_PROVIDERS as readonly string[]).includes(p) ? (p as AiProviderId) : null;
}

export function cleanAiKeyProvider(v: unknown): AiProviderId | null {
  const p = cleanAiProvider(v);
  return p && AI_KEYED_PROVIDERS.includes(p) ? p : null;
}

export function cleanApiKey(v: unknown): string | null {
  if (v === null || v === undefined) return '';
  const k = String(v).trim();
  if (k === '') return '';
  if (k.length > 400) return null;
  return /^[\x21-\x7e]+$/.test(k) ? k : null;
}

const PERSONAL_PREFIXES = ['mcp', 'autopilot', 'remote', 'ai', 'account'];
const PERSONAL_KEYS = new Set([
  'accountMode', 'terminalPaperMode', 'terminalPaperBankrollUsd', 'mcpPaperBankrollUsd',
  'network', 'scriptsLiveEnabled', 'terminalOfflineMode', 'terminalNotifications',
]);

export function isPersonalKey(k: string): boolean {
  return PERSONAL_KEYS.has(k) || PERSONAL_PREFIXES.some((p) => k.startsWith(p)) || /webhook/i.test(k);
}

export function omitPersonal<T extends object>(config: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config as Record<string, unknown>)) {
    if (!isPersonalKey(k)) out[k] = v;
  }
  return out as Partial<T>;
}

const SHARE_EXCLUDE = new Set(['enableTrading', 'crypto15mEnabled', 'copyEnabled', 'perpsEnabled']);

export function omitForShare<T extends object>(config: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config as Record<string, unknown>)) {
    if (!isPersonalKey(k) && !SHARE_EXCLUDE.has(k)) out[k] = v;
  }
  return out as Partial<T>;
}
