import { useSyncExternalStore } from 'react';

export type GlassQuality = 'auto' | 'live' | 'frost';

const KEY = 'krypt.glass';
const DEMOTED_KEY = 'krypt.glass.autoFrost';

function read(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function write(key: string, v: string | null): void {
  try {
    if (v === null) localStorage.removeItem(key);
    else localStorage.setItem(key, v);
  } catch { }
}

function storedQuality(): GlassQuality {
  const v = read(KEY);
  return v === 'live' || v === 'frost' ? v : 'auto';
}

let canRunLive = typeof ResizeObserver !== 'undefined';

function reducedTransparency(): boolean {
  try {
    return typeof matchMedia === 'function'
      && matchMedia('(prefers-reduced-transparency: reduce)').matches;
  } catch {
    return false;
  }
}

let quality: GlassQuality = storedQuality();
let demoted = read(DEMOTED_KEY) === '1';
let reduced = reducedTransparency();
const listeners = new Set<() => void>();

function effective(): boolean {
  if (!canRunLive || reduced) return false;
  if (quality === 'frost') return false;
  if (quality === 'live') return true;
  return !demoted;
}
let snapshot = effective();

function emit(): void {
  const next = effective();
  if (next === snapshot) {
    listeners.forEach((l) => l());
    return;
  }
  snapshot = next;
  listeners.forEach((l) => l());
}

try {
  matchMedia('(prefers-reduced-transparency: reduce)')
    .addEventListener('change', () => { reduced = reducedTransparency(); emit(); });
} catch { }

export function liveGlassEnabled(): boolean { return snapshot; }

export function useLiveGlass(): boolean {
  return useSyncExternalStore(subscribe, liveGlassEnabled, liveGlassEnabled);
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

export interface GlassQualityState {
  quality: GlassQuality;
  autoDemoted: boolean;
  reducedTransparency: boolean;
  live: boolean;
}

export function glassQualityState(): GlassQualityState {
  return { quality, autoDemoted: demoted, reducedTransparency: reduced, live: snapshot };
}

export function setGlassQuality(q: GlassQuality): void {
  quality = q;
  write(KEY, q === 'auto' ? null : q);
  if (q === 'auto') {
    demoted = false;
    write(DEMOTED_KEY, null);
  }
  emit();
}

function demote(): void {
  if (quality !== 'auto' || demoted) return;
  demoted = true;
  write(DEMOTED_KEY, '1');
  emit();
  try { window.dispatchEvent(new CustomEvent('krypt:glass-demoted')); } catch { }
}

const WINDOW_MS = 1500;
const PERIOD_MS = 20_000;
const SLOW_RATIO = 1.5;
const SLOW_WINDOWS = 2;

let started = false;
let bestMedian = Infinity;
let slowStreak = 0;
let sampling = false;
let timer: ReturnType<typeof setTimeout> | null = null;

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)] ?? 0;
}

function sampleWindow(): Promise<number[]> {
  return new Promise((done) => {
    const d: number[] = [];
    let last = 0;
    let t0 = 0;
    const tick = (t: number): void => {
      if (t0 === 0) { t0 = t; last = t; requestAnimationFrame(tick); return; }
      d.push(t - last);
      last = t;
      if (t - t0 < WINDOW_MS) requestAnimationFrame(tick);
      else done(d);
    };
    requestAnimationFrame(tick);
  });
}

async function runWindow(): Promise<void> {
  if (sampling || document.hidden || quality !== 'auto' || demoted || !snapshot) return;
  if (document.documentElement.hasAttribute('data-window-hidden')) return;
  sampling = true;
  try {
    const d = await sampleWindow();
    if (d.length < 20 || document.hidden) return;
    judge(median(d));
  } finally {
    sampling = false;
  }
}

function judge(m: number): void {
  bestMedian = Math.min(bestMedian, m);
  const refresh = Math.max(bestMedian, 4);
  if (m > refresh * SLOW_RATIO && m > 12) {
    slowStreak += 1;
    if (slowStreak >= SLOW_WINDOWS) demote();
  } else {
    slowStreak = 0;
  }
}

function schedule(ms: number): void {
  if (timer) clearTimeout(timer);
  timer = setTimeout(async () => {
    await runWindow();
    schedule(PERIOD_MS);
  }, ms);
}

export function startGlassWatchdog(): void {
  if (started || typeof window === 'undefined' || typeof requestAnimationFrame !== 'function') return;
  started = true;
  schedule(4000);
}

export function kickGlassWatchdog(): void {
  if (!started) return;
  schedule(1200);
}

export const __glassQualityTest = {
  reset(): void {
    quality = storedQuality(); demoted = read(DEMOTED_KEY) === '1';
    reduced = reducedTransparency(); bestMedian = Infinity; slowStreak = 0;
    snapshot = effective();
  },
  judge(medians: number[]): void { medians.forEach(judge); },
  canRunLive(on: boolean): void { canRunLive = on; snapshot = effective(); },
};
