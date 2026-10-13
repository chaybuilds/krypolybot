import { createElement, lazy, type ComponentType } from 'react';
import { Building2, Dices, Flag, Spade } from 'lucide-react';
import { ErrorBoundary, SceneCrash } from '../components/ErrorBoundary';

export type HubViewId = 'casino' | 'poker' | 'racetrack' | 'penthouse';

const LEGACY: Record<string, HubViewId> = { spaceship: 'casino', council: 'poker', orbital: 'racetrack' };

export interface HubView {
  id: HubViewId;
  label: string;
  icon: ComponentType<{ className?: string }>;
  blurb: string;
  scene: ComponentType;
}

export function guarded(load: () => Promise<{ default: ComponentType }>): ComponentType {
  const Scene = lazy(load);
  return function GuardedScene() {
    return createElement(ErrorBoundary, {
      fallback: (error: Error, reset: () => void) => createElement(SceneCrash, { error, onRetry: reset }),
      children: createElement(Scene),
    });
  };
}

export const HUB_VIEWS: HubView[] = [
  {
    id: 'casino', label: 'Casino', icon: Dices, scene: guarded(() => import('./CasinoView')),
    blurb: 'What really happens, staged live: the house crew works your signals, orders and wins, and each AI agent crosses the floor to the table of every real tool call it makes. Between calls your agents play the tables for play chips. Walk in to play blackjack, roulette and slots, or bet the sportsbook on Polymarket’s live sports odds.',
  },
  {
    id: 'poker', label: 'High Stakes', icon: Spade, scene: guarded(() => import('./PokerView')),
    blurb: 'Your real forecasters take a seat: each one’s own fair value on every market it called, bet against the price and scored when it settles. Under two, six trading emotions play the bot’s signals instead.',
  },
  {
    id: 'racetrack', label: 'Race Track', icon: Flag, scene: guarded(() => import('./RaceTrackView')),
    blurb: 'Every signal the bot sees rolls up to the starting gate; every trade it takes races in its price lane until it settles at the finish.',
  },
  {
    id: 'penthouse', label: 'Penthouse', icon: Building2, scene: guarded(() => import('./PenthouseView')),
    blurb: 'Only the agents you set up, each at its own desk of monitors over the city: its live tool calls, the last market it touched with its fair value against the price, its forecast record and its rules. The video wall carries your account, positions, signals and every agent call. Click a desk to sit in.',
  },
];

const KEY = 'krypt.agentHub.view';

export function loadHubView(): HubViewId {
  try {
    const v = window.localStorage.getItem(KEY);
    if (v && HUB_VIEWS.some((x) => x.id === v)) return v as HubViewId;
    if (v && LEGACY[v]) return LEGACY[v];
  } catch { }
  return 'casino';
}

export function saveHubView(id: HubViewId): void {
  try { window.localStorage.setItem(KEY, id); } catch { }
}
