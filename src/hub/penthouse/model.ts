import {
  DEFAULT_AGENT_ID, effectiveAgentMode, effectiveCaps, isValidAgentId, rulesInWords,
  type GlobalTradeMode, type McpAgent,
} from '@shared/agents';
import type { TraderConfig } from '@shared/types';
import type { HubAgentScore, HubForecastRow, HubScoreboard } from '../data';
import type { ActivityRecord, AgentCallActivity } from '../../state/activity';
import { configuredAgents, hubAgentIdentity, namedAgentIdentity, type AgentIdentity } from '../../utils/agents';

export type ToolGroup = 'research' | 'forecast' | 'order' | 'account' | 'build' | 'other';

const GROUPS: Record<Exclude<ToolGroup, 'other'>, readonly string[]> = {
  research: [
    'discover_markets', 'search_markets', 'get_market', 'get_order_book', 'get_price_history',
    'get_trade_tape', 'get_resolution_risk', 'get_related_markets', 'get_cross_venue',
    'manage_watchlist', 'get_trader',
  ],
  forecast: ['record_forecast'],
  order: [
    'preview_order', 'place_order', 'cancel_order', 'close_position', 'cancel_all_orders',
    'flatten_wallet', 'manage_standing_instructions',
  ],
  account: ['get_status', 'get_my_agent', 'get_portfolio', 'get_orders', 'get_trade_history', 'get_engine_activity'],
  build: [
    'pause_all_engines', 'set_engines', 'update_config', 'perps', 'backtest_main_engine',
    'backtest_script', 'parlay_schedule', 'research_data', 'get_script_docs', 'list_scripts',
    'validate_script', 'save_script', 'set_script_state',
  ],
};
const GROUP_OF = new Map<string, ToolGroup>(
  Object.entries(GROUPS).flatMap(([g, tools]) => tools.map((t) => [t, g as ToolGroup] as const)),
);

export function toolGroup(tool: string | null | undefined): ToolGroup {
  return (tool && GROUP_OF.get(tool)) || 'other';
}

export function isOrderTool(tool: string | null | undefined): boolean {
  return toolGroup(tool) === 'order';
}

export interface DeskCall {
  seq: number;
  at: number;
  connect: boolean;
  tool: string | null;
  group: ToolGroup;
  summary: string;
  outcome: 'ok' | 'refused' | 'error';
  reason: string | null;
  mode: 'paper' | 'live' | null;
  conditionId: string | null;
  title: string | null;
  fairCents: number | null;
  midCents: number | null;
  edgeCents: number | null;
  side: 'yes' | 'no' | null;
  suppressed: number;
  durationMs: number | null;
}

export interface DeskMarket {
  conditionId: string;
  title: string | null;
  fairCents: number | null;
  midCents: number | null;
  edgeCents: number | null;
  side: 'yes' | 'no' | null;
  at: number;
  tool: string | null;
  forecastAt: number | null;
}

export interface Desk {
  id: string;
  agent: McpAgent;
  who: AgentIdentity;
  mode: GlobalTradeMode;
  enabled: boolean;
  rules: string[];
  caps: { maxOrderUsd: number; dailySpendUsd: number; minEdgeCents: number };
  calls: DeskCall[];
  orders: DeskCall[];
  pulse: { at: number; outcome: DeskCall['outcome'] }[];
  ok: number;
  refused: number;
  error: number;
  suppressed: number;
  lastAt: number | null;
  connectAt: number | null;
  client: string | null;
  model: string | null;
  market: DeskMarket | null;
  score: HubAgentScore | null;
  forecasts: HubForecastRow[];
  rev: number;
}

export const CALLS_KEPT = 40;
export const ORDERS_KEPT = 10;
export const FORECASTS_KEPT = 10;
export const PULSE_WINDOW_MS = 30 * 60_000;

function deskFor(agent: McpAgent): Desk {
  return {
    id: agent.id, agent, who: namedAgentIdentity({ ...agent, name: agent.name || 'Default' }),
    mode: 'off', enabled: agent.enabled !== false, rules: [],
    caps: { maxOrderUsd: 0, dailySpendUsd: 0, minEdgeCents: 0 },
    calls: [], orders: [], pulse: [], ok: 0, refused: 0, error: 0, suppressed: 0,
    lastAt: null, connectAt: null, client: null, model: null, market: null,
    score: null, forecasts: [], rev: 0,
  };
}

function unrenamed(a: McpAgent): boolean {
  return a.id === DEFAULT_AGENT_ID && (!a.name || a.name === 'Default');
}

export class DeskBook {
  private desks = new Map<string, Desk>();
  private shown: string[] = [];
  private seen = new Set<number>();
  private agents: McpAgent[] | null = null;
  private config: TraderConfig | null = null;
  private board: HubScoreboard | null = null;
  private live = new Map<string, HubForecastRow[]>();
  roster = 0;

  sync(config: TraderConfig | null | undefined): boolean {
    this.config = config ?? null;
    const agents = configuredAgents(config ?? null);
    this.agents = agents;
    if (!agents) return false;
    const live = new Set(agents.map((a) => a.id));
    for (const id of [...this.desks.keys()]) if (!live.has(id)) this.desks.delete(id);
    for (const a of agents) {
      let d = this.desks.get(a.id);
      if (!d) { d = deskFor(a); this.desks.set(a.id, d); }
      d.agent = a;
      this.refresh(d);
    }
    return this.reorder();
  }

  call(rec: ActivityRecord): Desk | null {
    if (rec.ev.kind !== 'agentCall' || this.seen.has(rec.seq)) return null;
    this.seen.add(rec.seq);
    if (this.seen.size > 4000) this.seen = new Set([...this.seen].slice(-2000));
    const ev: AgentCallActivity = rec.ev;
    const id = isValidAgentId(ev.agentId) ? ev.agentId : DEFAULT_AGENT_ID;
    const d = this.desks.get(id);
    if (!d) return null;
    if (ev.client) d.client = ev.client;
    if (ev.model) d.model = ev.model;
    if (ev.call === 'connect') {
      d.connectAt = Math.max(d.connectAt ?? 0, rec.at);
      this.refresh(d);
      this.reorder();
      return d;
    }
    const c: DeskCall = {
      seq: rec.seq, at: rec.at, connect: false, tool: ev.tool, group: toolGroup(ev.tool),
      summary: ev.summary, outcome: ev.outcome, reason: ev.reason, mode: ev.mode,
      conditionId: ev.conditionId, title: ev.title, fairCents: ev.fairCents, midCents: ev.midCents,
      edgeCents: ev.edgeCents, side: ev.side, suppressed: ev.suppressed, durationMs: ev.durationMs,
    };
    insertNewestFirst(d.calls, c, CALLS_KEPT);
    if (isOrderTool(c.tool)) insertNewestFirst(d.orders, c, ORDERS_KEPT);
    d.pulse.push({ at: c.at, outcome: c.outcome });
    d.pulse.sort((a, b) => a.at - b.at);
    const cut = Math.max(...d.pulse.map((p) => p.at)) - PULSE_WINDOW_MS;
    while (d.pulse.length && d.pulse[0].at < cut) d.pulse.shift();
    if (d.pulse.length > 1200) d.pulse.splice(0, d.pulse.length - 1200);
    d[c.outcome]++;
    d.suppressed += Math.max(0, c.suppressed);
    d.lastAt = Math.max(d.lastAt ?? 0, c.at);
    if (c.conditionId && c.outcome === 'ok' && (!d.market || c.at >= d.market.at)) {
      d.market = nextMarket(d.market, c);
    }
    if (c.tool === 'record_forecast' && c.outcome === 'ok' && c.conditionId && c.fairCents !== null) {
      const rows = this.live.get(d.id) ?? [];
      rows.unshift({
        createdAt: new Date(c.at).toISOString(), conditionId: c.conditionId, title: c.title,
        model: d.model, client: d.client, agentId: d.id, fairValueCents: c.fairCents,
        marketMidCents: c.midCents, outcome: null,
      });
      this.live.set(d.id, rows.slice(0, FORECASTS_KEPT));
    }
    this.refresh(d);
    this.reorder();
    return d;
  }

  scoreboard(b: HubScoreboard | null | undefined): void {
    if (b === undefined) return;
    this.board = b;
    for (const d of this.desks.values()) this.score(d);
    this.reorder();
  }

  list(): Desk[] {
    return this.shown.map((id) => this.desks.get(id)).filter((d): d is Desk => !!d);
  }

  get(id: string): Desk | undefined {
    return this.desks.get(id);
  }

  stream(limit: number): (DeskCall & { desk: Desk })[] {
    const out: (DeskCall & { desk: Desk })[] = [];
    for (const d of this.list()) for (const c of d.calls) out.push({ ...c, desk: d });
    return out.sort((a, b) => b.at - a.at || b.seq - a.seq).slice(0, limit);
  }

  private refresh(d: Desk): void {
    const cfg = this.config;
    const a = d.agent;
    d.enabled = a.enabled !== false;
    const global = cfg?.mcpTradeMode;
    d.mode = !d.enabled || cfg?.mcpEnabled === false
      ? 'off' : effectiveAgentMode(global, a, cfg?.accountMode);
    d.rules = rulesInWords(a.rules);
    const caps = effectiveCaps(cfg, a.rules);
    d.caps = { maxOrderUsd: caps.maxOrderUsd, dailySpendUsd: caps.dailySpendUsd, minEdgeCents: caps.minEdgeCents };
    d.who = unrenamed(a) && d.client
      ? hubAgentIdentity({ agentId: a.id, client: d.client, model: d.model }, this.agents)
      : namedAgentIdentity({ ...a, name: a.name || 'Default' }, d.model);
    this.score(d);
    d.rev++;
  }

  private score(d: Desk): void {
    const b = this.board;
    const board = !b ? [] : (b.recent ?? [])
      .filter((r) => (r.agentId ? r.agentId === d.id : d.id === DEFAULT_AGENT_ID));
    if (b) d.score = b.byAgent?.find((s) => s.agentId === d.id) ?? null;
    const at = (r: HubForecastRow) => Date.parse(r.createdAt ?? '') || 0;
    const fresh = (this.live.get(d.id) ?? []).filter((l) => !board.some((r) => r.conditionId === l.conditionId
      && r.fairValueCents === l.fairValueCents && Math.abs(at(r) - at(l)) < 180_000));
    this.live.set(d.id, fresh);
    d.forecasts = [...fresh, ...board].sort((x, y) => at(y) - at(x)).slice(0, FORECASTS_KEPT);
    if (d.market && !d.market.title) {
      d.market.title = d.forecasts.find((r) => r.conditionId === d.market!.conditionId && r.title)?.title ?? null;
    }
    d.rev++;
  }

  private reorder(): boolean {
    const agents = this.agents ?? [];
    const ids = agents.filter((a) => {
      if (!unrenamed(a)) return true;
      const d = this.desks.get(a.id);
      const active = !!d && (d.calls.length > 0 || d.connectAt !== null || d.forecasts.length > 0 || (d.score?.total ?? d.score?.n ?? 0) > 0);
      return active || agents.length === 1;
    }).map((a) => a.id);
    const changed = ids.length !== this.shown.length || ids.some((id, i) => this.shown[i] !== id);
    if (changed) { this.shown = ids; this.roster++; }
    return changed;
  }
}

function insertNewestFirst(list: DeskCall[], c: DeskCall, cap: number): void {
  let i = 0;
  while (i < list.length && (list[i].at > c.at || (list[i].at === c.at && list[i].seq > c.seq))) i++;
  list.splice(i, 0, c);
  if (list.length > cap) list.length = cap;
}

function nextMarket(prev: DeskMarket | null, c: DeskCall): DeskMarket {
  const same = prev?.conditionId === c.conditionId;
  const forecast = c.tool === 'record_forecast' && c.fairCents !== null;
  return {
    conditionId: c.conditionId as string,
    title: c.title ?? (same ? prev!.title : null),
    fairCents: forecast ? c.fairCents : same ? prev!.fairCents : null,
    midCents: c.midCents ?? (same ? prev!.midCents : null),
    edgeCents: forecast ? c.edgeCents : same ? prev!.edgeCents : null,
    side: forecast ? c.side : same ? prev!.side : null,
    at: c.at,
    tool: c.tool,
    forecastAt: forecast ? c.at : same ? prev!.forecastAt : null,
  };
}

export function pulseBuckets(
  pulse: Desk['pulse'], now: number, minutes = 30,
): { ok: number; refused: number; error: number }[] {
  const out = Array.from({ length: minutes }, () => ({ ok: 0, refused: 0, error: 0 }));
  for (const p of pulse) {
    const k = Math.floor((now - p.at) / 60_000);
    if (k < 0 || k >= minutes) continue;
    out[minutes - 1 - k][p.outcome]++;
  }
  return out;
}

export function agoShort(at: number | null, now: number): string {
  if (at === null) return '—';
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

export function verdictText(v: HubAgentScore['verdict'] | undefined): { text: string; tone: 'win' | 'loss' | 'flat' } {
  switch (v) {
    case 'agent-better': return { text: 'BEATING THE MARKET', tone: 'win' };
    case 'market-better': return { text: 'MARKET IS AHEAD', tone: 'loss' };
    case 'indistinguishable': return { text: 'TOO CLOSE TO CALL', tone: 'flat' };
    default: return { text: 'NEEDS MORE SETTLED', tone: 'flat' };
  }
}
