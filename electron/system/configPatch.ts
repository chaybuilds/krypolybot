type Kind = 'boolean' | 'number' | 'directionMode' | 'offOnly' | 'agentModeNarrow';

export const CONFIG_PATCH_ALLOW: Record<string, Kind> = {
  enableTrading: 'boolean',
  tradeWhales: 'boolean',
  tradeMomentum: 'boolean',
  crypto15mEnabled: 'boolean',
  copyEnabled: 'boolean',
  scriptsLiveEnabled: 'boolean',
  perpsEnabled: 'boolean',
  autopilotEnabled: 'offOnly',
  mcpTradeMode: 'agentModeNarrow',
  crypto15mDirectionMode: 'directionMode',
  crypto15mEntryThreshold: 'number',
  crypto15mEntryMax: 'number',
  crypto15mMinDeltaPct: 'number',
  crypto15mTimeDelayMin: 'number',
};

const SCRIPT_OFF_ONLY = new Set(['enableTrading', 'perpsEnabled']);

export function sanitizeChannelPatch(
  source: string,
  patch: Record<string, unknown>,
  drop: (k: string, why: string) => void,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch ?? {})) {
    const kind = CONFIG_PATCH_ALLOW[k];
    if (!kind) { drop(k, 'not patchable from this channel'); continue; }
    if (kind === 'boolean') {
      if (typeof v !== 'boolean') { drop(k, `expected boolean, got ${typeof v}`); continue; }
      if (SCRIPT_OFF_ONLY.has(k) && v && source !== 'remote' && source !== 'mcp') {
        drop(k, 'can only be turned OFF from a script'); continue;
      }
      out[k] = v;
    } else if (kind === 'offOnly') {
      if (v !== false) { drop(k, 'can only be turned OFF from this channel'); continue; }
      out[k] = false;
    } else if (kind === 'agentModeNarrow') {
      if (v !== 'off' && v !== 'paper') { drop(k, "only 'off' or 'paper' from this channel"); continue; }
      out[k] = v;
    } else if (kind === 'number') {
      if (typeof v !== 'number' || !Number.isFinite(v)) { drop(k, 'not a finite number'); continue; }
      out[k] = v;
    } else if (v === 'favorite' || v === 'contrarian' || v === 'model') {
      out[k] = v;
    } else {
      drop(k, 'not a valid direction mode');
    }
  }
  return out;
}
