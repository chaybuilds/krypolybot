import type { AccountSnapshot, BotPosition, ScannerStats, SignalRow, TraderConfig } from '@shared/types';
import type { SavedBacktest } from './library';
import type { MarketRow } from './casino/sportsbook';
import { sessionPnlOf } from './payday';

export interface HubAutopilot {
  enabled?: boolean;
  running?: boolean;
  step?: number;
  runs?: { finishedAt: string | null; trigger?: string | null }[];
}

export type HubVerdict = 'too-few' | 'indistinguishable' | 'agent-better' | 'market-better';

export interface HubAgentScore {
  agentId: string;
  n: number;
  nPaired: number;
  brierAgent: number | null;
  brierAgentPaired?: number | null;
  brierMarket: number | null;
  skill: number | null;
  verdict: HubVerdict;
  total?: number;
  pending?: number;
  lastAt?: string | null;
  models?: string[];
  clients?: string[];
}

export interface HubForecastRow {
  id?: number;
  createdAt: string | null;
  conditionId: string;
  title: string | null;
  model: string | null;
  client: string | null;
  agentId?: string | null;
  fairValueCents: number | null;
  marketMidCents: number | null;
  outcome?: number | null;
}

export interface HubScoreboard {
  byAgent?: HubAgentScore[];
  recent?: HubForecastRow[];
}

interface HubReads {
  autopilot?: { status?: () => Promise<HubAutopilot | null> };
  ai?: { scoreboard?: (recent?: number) => Promise<HubScoreboard | null> };
  terminal?: {
    discover?: (args: { column: string; limit?: number; filters?: { categories?: string[] } }) => Promise<{ rows?: unknown[]; note?: string | null } | null>;
    market?: (conditionId: string) => Promise<{ market?: unknown } | null>;
  };
}

export interface HubSportsbook { rows: MarketRow[]; at: number; note: string | null }

function isRow(r: unknown): r is MarketRow {
  return !!r && typeof r === 'object' && typeof (r as MarketRow).conditionId === 'string' && typeof (r as MarketRow).title === 'string';
}

export async function readSportsMarkets(): Promise<HubSportsbook | null | undefined> {
  const fn = reads()?.terminal?.discover;
  if (typeof fn !== 'function') return undefined;
  const filters = { categories: ['sports'] };
  const [vol, soon] = await Promise.all([
    fn({ column: 'volume', limit: 100, filters }).catch(() => null),
    fn({ column: 'closing', limit: 100, filters }).catch(() => null),
  ]);
  if (!vol && !soon) return null;
  const rows = [...(vol?.rows ?? []), ...(soon?.rows ?? [])].filter(isRow);
  return { rows, at: Date.now(), note: rows.length ? null : (vol?.note ?? soon?.note ?? null) };
}

export async function readMarkets(ids: readonly string[]): Promise<MarketRow[] | undefined> {
  const fn = reads()?.terminal?.market;
  if (typeof fn !== 'function') return undefined;
  const out: MarketRow[] = [];
  for (const id of ids) {
    try {
      const r = await fn(id);
      if (isRow(r?.market)) out.push(r!.market as MarketRow);
    } catch { }
  }
  return out;
}

function reads(): HubReads | undefined {
  return (window as unknown as { krypt?: HubReads }).krypt;
}

export async function readAutopilot(): Promise<HubAutopilot | null | undefined> {
  const fn = reads()?.autopilot?.status;
  if (typeof fn !== 'function') return undefined;
  return (await fn()) ?? null;
}

export async function readScoreboard(recent = 200): Promise<HubScoreboard | null | undefined> {
  const fn = reads()?.ai?.scoreboard;
  if (typeof fn !== 'function') return undefined;
  return (await fn(recent)) ?? null;
}

export interface HubData {
  signals: SignalRow[];
  positions: BotPosition[];
  account: AccountSnapshot | null;
  scannerStats: ScannerStats | null;
  config?: TraderConfig | null;
  library?: SavedBacktest[];
  autopilot?: HubAutopilot | null;
  scoreboard?: HubScoreboard | null;
  councilChoice?: 'personas' | 'agents' | null;
  sportsbook?: HubSportsbook | null;
}

export interface HudNumbers {
  pnl: number | null;
  roi: number | null;
  winRate: number | null;
  settledToday: number | null;
  open: number | null;
  signals: number | null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export function hudNumbers(a: AccountSnapshot | null, s: ScannerStats | null): HudNumbers {
  const pnl = sessionPnlOf(a);
  const wins = num(a?.wins);
  const losses = num(a?.losses);
  const tw = num(a?.todayWins);
  const tl = num(a?.todayLosses);
  return {
    pnl,
    roi: pnl === null ? null : num(a?.sessionRoiPct),
    winRate: wins !== null && losses !== null && wins + losses > 0 ? num(a?.winRate) : null,
    settledToday: tw !== null && tl !== null ? tw + tl : null,
    open: num(a?.openCount),
    signals: s ? s.whales.total + s.momentum.total : null,
  };
}

export function balanceOf(a: AccountSnapshot | null): number | null {
  if (!a) return null;
  const t = num(a.totalUsd);
  if (t === null) return null;
  if (t > 0) return t;
  return num(a.sessionBaselineUsd) !== null && (a.sessionBaselineUsd as number) > 0 ? t : null;
}
