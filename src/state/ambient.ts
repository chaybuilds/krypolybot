export const AMBIENT_FPS = 15;
const AMBIENT_SELECTOR = '.glass-aurora, .krypt-sprite, [data-ambient]';

type Tick = (now: number) => void;
const ticks = new Set<Tick>();
const origins = new WeakMap<Animation, number>();
let timer: ReturnType<typeof setInterval> | null = null;

function hidden(): boolean {
  return document.hidden || document.documentElement.hasAttribute('data-window-hidden');
}

function isAmbient(a: Animation): boolean {
  const effect = a.effect as KeyframeEffect | null;
  const target = effect?.target as Element | null | undefined;
  if (!effect || !target || typeof target.closest !== 'function') return false;
  if (effect.getTiming().iterations !== Infinity) return false;
  return !!target.closest(AMBIENT_SELECTOR);
}

function adopt(now: number): Animation[] {
  const out: Animation[] = [];
  for (const a of document.getAnimations()) {
    if (!isAmbient(a)) continue;
    if (!origins.has(a)) {
      const ct = typeof a.currentTime === 'number' ? a.currentTime : 0;
      origins.set(a, now - ct);
      a.pause();
    }
    out.push(a);
  }
  return out;
}

function step(): void {
  if (hidden()) return;
  const now = performance.now();
  for (const a of adopt(now)) {
    const origin = origins.get(a) ?? now;
    a.currentTime = now - origin;
  }
  ticks.forEach((t) => {
    try { t(now); } catch { }
  });
}

export function startAmbientClock(): void {
  if (timer !== null || typeof document === 'undefined') return;
  if (typeof document.getAnimations !== 'function') return;
  step();
  timer = setInterval(step, Math.round(1000 / AMBIENT_FPS));
}

export function stopAmbientClock(): void {
  if (timer !== null) clearInterval(timer);
  timer = null;
}

export function onAmbientTick(cb: Tick): () => void {
  if (timer === null) {
    const own = setInterval(() => { if (!hidden()) cb(performance.now()); },
      Math.round(1000 / AMBIENT_FPS));
    return () => { clearInterval(own); };
  }
  ticks.add(cb);
  return () => { ticks.delete(cb); };
}
