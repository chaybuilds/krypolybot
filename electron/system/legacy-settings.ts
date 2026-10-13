import type { TraderConfig } from '../../shared/types';
import { cleanAgents, DEFAULT_AGENT_ID, isValidAgentId } from '../../shared/agents';

export const RETIRED_CONFIG_KEYS = [
  'mcpLiveTradingEnabled', 'mcpDailyLimitUsd', 'aiAnthropicModel', 'aiOpenaiModel',
] as const;

const AI_PROVIDERS = new Set(['anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio']);

export interface MigrateOpts {
  live?: boolean;
  existingUser?: boolean;
}

export function migrateAccountMode(src: Record<string, unknown>, opts: MigrateOpts = {}): void {
  if (src.accountMode !== 'live' && src.accountMode !== 'paper') {
    const absent = !('accountMode' in src);
    src.accountMode = absent && src.terminalPaperMode !== true && opts.live === true && opts.existingUser === true
      ? 'live' : 'paper';
  }
  src.terminalPaperMode = src.accountMode === 'paper';
}

export function migrateAgents(src: Record<string, unknown>, defaults: TraderConfig): void {
  const legacyLive = src.mcpLiveTradingEnabled === true;
  if (!('mcpTradeMode' in src)) src.mcpTradeMode = legacyLive ? 'live' : defaults.mcpTradeMode;
  if (!['off', 'paper', 'live'].includes(String(src.mcpTradeMode))) src.mcpTradeMode = defaults.mcpTradeMode;
  if (!('mcpAllowLiveSwitches' in src) && legacyLive) src.mcpAllowLiveSwitches = true;
  if (!('mcpDailySpendUsd' in src) && typeof src.mcpDailyLimitUsd === 'number'
      && Number.isFinite(src.mcpDailyLimitUsd)) {
    src.mcpDailySpendUsd = src.mcpDailyLimitUsd;
  }
  src.mcpAgents = cleanAgents('mcpAgents' in src ? src.mcpAgents : undefined, String(src.mcpTradeMode));
  const apId = String(src.autopilotAgentId ?? '').trim().toLowerCase();
  src.autopilotAgentId = isValidAgentId(apId) ? apId : DEFAULT_AGENT_ID;
}

export function migrateAi(src: Record<string, unknown>, defaults: TraderConfig): void {
  const p = String(src.aiProvider ?? '').trim().toLowerCase();
  src.aiProvider = AI_PROVIDERS.has(p) ? p : defaults.aiProvider;
  if (typeof src.aiModel !== 'string') {
    const old = src.aiProvider === 'openai' ? src.aiOpenaiModel
      : src.aiProvider === 'anthropic' ? src.aiAnthropicModel : undefined;
    if (typeof old === 'string' && old.trim()) src.aiModel = old.trim();
    else if (src.aiProvider === 'anthropic') src.aiModel = defaults.aiModel;
    else src.aiModel = '';
  }
}

export function migrateConfig(raw: unknown, defaults: TraderConfig, opts: MigrateOpts = {}): TraderConfig {
  const src: Record<string, unknown> =
    raw && typeof raw === 'object' && !Array.isArray(raw) ? { ...(raw as Record<string, unknown>) } : {};
  migrateAccountMode(src, opts);
  migrateAgents(src, defaults);
  migrateAi(src, defaults);
  for (const k of RETIRED_CONFIG_KEYS) delete src[k];
  return { ...defaults, ...src } as TraderConfig;
}
