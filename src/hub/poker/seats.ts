import type { AgentCallActivity, ActivityEvent } from '../../state/activity';
import type { HubAgentScore, HubScoreboard, HubVerdict } from '../data';
import { isValidAgentId, type McpAgent } from '@shared/agents';
import { agentIdentity, hubAgentIdentity, type AgentIdentity } from '../../utils/agents';
import { MEMBER_IDS, PERSONAS, type MemberId } from './roster';

export const MIN_REAL_FORECASTERS = 2;

export interface Call {
  fair: number;
  mid: number | null;
  at: number;
}

export interface Forecaster {
  who: AgentIdentity;
  calls: Map<string, Call>;
  score: Pick<HubAgentScore, 'n' | 'nPaired' | 'brierAgent' | 'brierAgentPaired' | 'brierMarket' | 'skill' | 'verdict'> | null;
  total: number;
  lastAt: number;
}

export interface Seat {
  member: MemberId;
  f: Forecaster;
  label: string;
}

function ts(v: string | null | undefined): number {
  if (!v) return 0;
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : `${v.replace(' ', 'T')}Z`);
  return Number.isFinite(t) ? t : 0;
}

export const OPEN_CALL_MS = 48 * 3600_000;

export function seatKey(who: AgentIdentity, agentId: string | null | undefined): string {
  return isValidAgentId(agentId) ? `agent|${agentId}` : who.id;
}

export function forecastersFrom(
  board: HubScoreboard | null | undefined, agents: readonly McpAgent[] | null = null, now = Date.now(),
): Map<string, Forecaster> {
  const out = new Map<string, Forecaster>();
  const scores = new Map<string, HubAgentScore>();
  for (const r of board?.byAgent ?? []) if (r && typeof r.agentId === 'string') scores.set(r.agentId, r);
  const scoreFor = (aid: string | null | undefined): Forecaster['score'] => {
    const r = aid ? scores.get(aid) : undefined;
    return r && r.n > 0 ? {
      n: r.n, nPaired: r.nPaired, brierAgent: r.brierAgent, brierAgentPaired: r.brierAgentPaired ?? null,
      brierMarket: r.brierMarket, skill: r.skill, verdict: r.verdict,
    } : null;
  };
  const counted = new Set<string>();
  for (const r of board?.recent ?? []) {
    if (!r || typeof r.conditionId !== 'string') continue;
    const who = hubAgentIdentity({ agentId: r.agentId, client: r.client, model: r.model, source: 'mcp' }, agents);
    const key = seatKey(who, r.agentId);
    let f = out.get(key);
    if (!f) {
      const sc = r.agentId ? scores.get(r.agentId) : undefined;
      f = { who, calls: new Map(), total: sc?.total ?? 0, lastAt: ts(sc?.lastAt ?? r.createdAt), score: scoreFor(r.agentId) };
      out.set(key, f);
      if (typeof sc?.total === 'number') counted.add(key);
    }
    if (!counted.has(key)) f.total++;
    const at = ts(r.createdAt);
    f.lastAt = Math.max(f.lastAt, at);
    if (r.outcome !== null && r.outcome !== undefined) continue;
    if (at && now - at > OPEN_CALL_MS) continue;
    if (f.calls.has(r.conditionId)) continue;
    const fair = r.fairValueCents;
    if (typeof fair !== 'number' || !(fair >= 1 && fair <= 99)) continue;
    const mid = typeof r.marketMidCents === 'number' && r.marketMidCents >= 1 && r.marketMidCents <= 99 ? r.marketMidCents : null;
    f.calls.set(r.conditionId, { fair, mid, at });
  }
  for (const r of scores.values()) {
    const who = hubAgentIdentity({ agentId: r.agentId, client: r.clients?.[0] ?? null, model: r.models?.[0] ?? null, source: 'mcp' }, agents);
    const key = seatKey(who, r.agentId);
    if (out.has(key)) continue;
    out.set(key, { who, calls: new Map(), total: r.total ?? r.n, lastAt: ts(r.lastAt), score: scoreFor(r.agentId) });
  }
  return out;
}

export function noteLiveForecast(
  roster: Map<string, Forecaster>, ev: ActivityEvent, at: number, agents: readonly McpAgent[] | null = null,
): { ticker: string; seatId: string } | null {
  let who: AgentIdentity;
  let agentId: string | null = null;
  let ticker: string | null;
  let fair: number | null;
  let mid: number | null = null;
  if (ev.kind === 'agentCall') {
    const c = ev as AgentCallActivity;
    if (c.call !== 'call' || c.tool !== 'record_forecast' || c.outcome !== 'ok') return null;
    who = hubAgentIdentity(c, agents);
    agentId = c.agentId ?? null;
    ticker = c.conditionId;
    fair = c.fairCents;
    mid = c.midCents;
  } else if (ev.kind === 'aiAnalysis') {
    who = agentIdentity({ source: 'panel', model: ev.provider === 'openai' ? 'gpt' : ev.provider === 'anthropic' ? 'claude' : ev.provider });
    ticker = ev.conditionId;
    fair = ev.fairCents;
  } else {
    return null;
  }
  if (!ticker || fair === null || !(fair >= 1 && fair <= 99)) return null;
  const key = seatKey(who, agentId);
  let f = roster.get(key);
  if (!f) {
    f = { who, calls: new Map(), score: null, total: 0, lastAt: at };
    roster.set(key, f);
  } else if (!f.who.model && who.model) {
    f.who = who;
  }
  f.total++;
  f.lastAt = Math.max(f.lastAt, at);
  const prev = f.calls.get(ticker);
  if (!prev || prev.at <= at) f.calls.set(ticker, { fair, mid, at });
  return { ticker, seatId: key };
}

const SEAT_ORDER = [2, 3, 1, 4, 0, 5];

export function assignSeats(roster: Map<string, Forecaster>, max = MEMBER_IDS.length): Seat[] {
  const fs = [...roster.values()]
    .filter((f) => f.total > 0 || f.calls.size > 0)
    .sort((a, b) => b.lastAt - a.lastAt || b.total - a.total)
    .slice(0, Math.min(max, MEMBER_IDS.length));
  const seats = fs.map((f, i) => ({ member: MEMBER_IDS[SEAT_ORDER[i]], f, label: f.who.label }));
  const dup = new Map<string, number>();
  for (const s of seats) dup.set(s.label, (dup.get(s.label) ?? 0) + 1);
  for (const s of seats) {
    if ((dup.get(s.label) ?? 0) > 1) s.label = `${s.label} (${s.f.who.seatId.split('|').pop() || '?'})`;
  }
  return seats;
}

export type TableChoice = 'personas' | 'agents';

export function tableMode(choice: TableChoice | null, realCount: number): TableChoice {
  if (realCount < MIN_REAL_FORECASTERS) return 'personas';
  return choice ?? 'agents';
}

export function seatValue(seat: Seat | undefined, ticker: string | null): number | null {
  if (!seat || !ticker) return null;
  return seat.f.calls.get(ticker)?.fair ?? null;
}

export function cents(v: number): string {
  return `${Math.round(v)}¢`;
}

export function plateText(seat: Seat, ticker: string | null): string {
  const v = seatValue(seat, ticker);
  const name = seat.f.who.emoji ? `${seat.f.who.emoji} ${seat.label}` : seat.label;
  return `${name} · ${v === null ? '—' : cents(v)}`;
}

export function seatVote(seat: Seat | undefined, ticker: string | null): boolean | null {
  if (!seat || !ticker) return null;
  const c = seat.f.calls.get(ticker);
  if (!c || c.mid === null || c.fair === c.mid) return null;
  return c.fair > c.mid;
}

export function seatLine(seat: Seat, ticker: string): string | null {
  const c = seat.f.calls.get(ticker);
  if (!c) return null;
  const line = c.mid === null ? `fair ${cents(c.fair)}` : `fair ${cents(c.fair)} · mid was ${cents(c.mid)}`;
  return seat.f.who.motto ? `${line} · “${seat.f.who.motto}”` : line;
}

export interface ScoreLine {
  name: string;
  color: string;
  value: string;
  sub?: string;
  bar: number | null;
  signed?: boolean;
  tone: 'good' | 'bad' | 'neutral' | 'none';
}

export interface ScoreView { title: string; caption: string; lines: ScoreLine[] }

const TONE_OF: Record<HubVerdict, ScoreLine['tone']> = {
  'agent-better': 'good', 'market-better': 'bad', indistinguishable: 'neutral', 'too-few': 'neutral',
};

export function agentScoreView(seats: Seat[]): ScoreView {
  const lines = seats.map((s): ScoreLine & { k: number } => {
    const sc = s.f.score;
    const skill = sc?.skill ?? null;
    const brier = sc ? (sc.brierAgentPaired ?? sc.brierAgent) : null;
    const open = s.f.calls.size;
    const sub = sc && brier !== null
      ? `Brier ${brier.toFixed(3)}${sc.brierMarket !== null ? ` vs mkt ${sc.brierMarket.toFixed(3)}` : ''} · ${sc.nPaired} settled`
      : `nothing settled yet · ${open} open`;
    return {
      name: s.label, color: s.f.who.color, sub,
      value: skill === null ? '—' : `${skill > 0 ? '+' : skill < 0 ? '−' : ''}${Math.abs(skill * 100).toFixed(0)}%`,
      bar: skill === null ? null : Math.max(-1, Math.min(1, skill * 2)),
      signed: true,
      tone: skill === null || !sc ? 'none' : TONE_OF[sc.verdict],
      k: skill === null ? -Infinity : skill,
    };
  });
  lines.sort((a, b) => b.k - a.k);
  return { title: 'WHO CALLED IT?', caption: 'skill vs market · settled', lines: lines.map(({ k: _k, ...l }) => l) };
}

export function personaScoreView(rows: { id: MemberId; right: number; n: number }[]): ScoreView {
  const sorted = [...rows].sort((a, b) => (b.n ? b.right / b.n : -1) - (a.n ? a.right / a.n : -1));
  return {
    title: 'WHO CALLED IT?',
    caption: 'call accuracy · settled trades',
    lines: sorted.map((r) => {
      const pct = r.n ? (r.right / r.n) * 100 : null;
      return {
        name: PERSONAS[r.id].name, color: PERSONAS[r.id].color,
        value: pct === null ? '—' : `${pct.toFixed(0)}%`,
        bar: pct === null ? null : pct / 100,
        tone: pct === null ? 'none' : pct >= 55 ? 'good' : pct < 45 ? 'bad' : 'neutral',
      };
    }),
  };
}

export function recentTickers(seats: Seat[], max = 3): string[] {
  const latest = new Map<string, number>();
  for (const s of seats) {
    for (const [t, c] of s.f.calls) latest.set(t, Math.max(latest.get(t) ?? 0, c.at));
  }
  return [...latest.entries()].sort((a, b) => b[1] - a[1]).slice(0, max).map(([t]) => t).reverse();
}
