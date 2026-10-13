import type { CineKind, CineShot } from '../cine';
import type { HubViewId } from '../views';

export type TrailerLength = 30 | 60;
export type TrailerFormat = 'landscape' | 'reel';

export const FORMAT_SIZE: Record<TrailerFormat, { w: number; h: number; label: string }> = {
  landscape: { w: 1920, h: 1080, label: '16:9' },
  reel: { w: 1080, h: 1920, label: '9:16 Reels' },
};

export type TrailerStep =
  | { kind: 'card'; dur: number; title: string; sub?: string; end?: boolean }
  | { kind: 'shot'; dur: number; view: HubViewId; shot: CineShot; caption?: string };

interface SceneBeat {
  view: HubViewId;
  card: string;
  moves: { kind: CineKind; dur: number; caption?: string }[];
}

const SHORT: SceneBeat[] = [
  {
    view: 'casino', card: 'Your AI agents, at work',
    moves: [
      { kind: 'establish', dur: 3.2, caption: 'Every real tool call, staged live' },
      { kind: 'push', dur: 3.0 },
    ],
  },
  {
    view: 'penthouse', card: 'Each one at its own desk',
    moves: [
      { kind: 'establish', dur: 3.0, caption: 'Claude, Cursor, Codex: any MCP client' },
      { kind: 'close', dur: 2.8, caption: 'A forecast before every buy' },
      { kind: 'orbit', dur: 3.0 },
    ],
  },
  {
    view: 'poker', card: 'Scored against the market',
    moves: [
      { kind: 'push', dur: 3.0 },
      { kind: 'close', dur: 2.6, caption: 'Fair value vs the price, settled for real' },
    ],
  },
  {
    view: 'racetrack', card: 'Every trade, a race to settlement',
    moves: [
      { kind: 'establish', dur: 3.0 },
      { kind: 'orbit', dur: 2.8, caption: 'Live on Polymarket' },
    ],
  },
];

const LONG: SceneBeat[] = [
  {
    view: 'casino', card: 'Your AI agents, at work',
    moves: [
      { kind: 'establish', dur: 4.0, caption: 'Every real tool call, staged live' },
      { kind: 'push', dur: 3.6 },
      { kind: 'close', dur: 3.2, caption: 'Signals, orders and wins, as they happen' },
      { kind: 'reveal', dur: 3.6 },
    ],
  },
  {
    view: 'penthouse', card: 'Each one at its own desk',
    moves: [
      { kind: 'establish', dur: 4.0, caption: 'Claude, Cursor, Codex: any MCP client' },
      { kind: 'push', dur: 3.4 },
      { kind: 'close', dur: 3.4, caption: 'A forecast before every buy' },
      { kind: 'orbit', dur: 4.0, caption: 'Inside your caps, on paper or live' },
    ],
  },
  {
    view: 'poker', card: 'Scored against the market',
    moves: [
      { kind: 'establish', dur: 3.4 },
      { kind: 'push', dur: 3.4, caption: 'Fair value vs the price' },
      { kind: 'close', dur: 3.2, caption: 'Brier-scored when it settles' },
    ],
  },
  {
    view: 'racetrack', card: 'Every trade, a race to settlement',
    moves: [
      { kind: 'establish', dur: 3.6 },
      { kind: 'push', dur: 3.2 },
      { kind: 'orbit', dur: 3.6, caption: 'Live on Polymarket' },
    ],
  },
];

const OPEN = { title: 'KRYPT POLYBOT', sub: 'AI agents that trade Polymarket' };
const CLOSE = { title: 'KRYPT POLYBOT', sub: 'Free · krypt.cc' };

export function buildPlan(length: TrailerLength, seed = 0): TrailerStep[] {
  const beats = length === 60 ? LONG : SHORT;
  const cardDur = length === 60 ? 2.2 : 1.7;
  const steps: TrailerStep[] = [{ kind: 'card', dur: length === 60 ? 2.8 : 2.2, ...OPEN }];
  let n = seed;
  for (const b of beats) {
    steps.push({ kind: 'card', dur: cardDur, title: b.card });
    for (const m of b.moves) {
      steps.push({ kind: 'shot', dur: m.dur, view: b.view, shot: { kind: m.kind, dur: m.dur, seed: n++ }, caption: m.caption });
    }
  }
  steps.push({ kind: 'card', dur: length === 60 ? 3.4 : 2.8, end: true, ...CLOSE });
  return steps;
}

export function planDuration(steps: TrailerStep[]): number {
  return steps.reduce((s, x) => s + x.dur, 0);
}

export function stepAt(steps: TrailerStep[], t: number): { step: TrailerStep; index: number; local: number } | null {
  let acc = 0;
  for (let i = 0; i < steps.length; i++) {
    if (t < acc + steps[i].dur) return { step: steps[i], index: i, local: t - acc };
    acc += steps[i].dur;
  }
  return null;
}

export function viewFor(steps: TrailerStep[], index: number): HubViewId | null {
  for (let i = index; i < steps.length; i++) {
    const s = steps[i];
    if (s.kind === 'shot') return s.view;
  }
  for (let i = index; i >= 0; i--) {
    const s = steps[i];
    if (s.kind === 'shot') return s.view;
  }
  return null;
}
