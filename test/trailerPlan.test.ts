import { describe, expect, it } from 'vitest';
import { buildPlan, planDuration, stepAt, viewFor } from '../src/hub/trailer/plan';

describe('trailer plan', () => {
  it('runs about 30 or 60 seconds, opening and closing on a card', () => {
    const short = buildPlan(30);
    const long = buildPlan(60);
    expect(planDuration(short)).toBeGreaterThan(28);
    expect(planDuration(short)).toBeLessThan(40);
    expect(planDuration(long)).toBeGreaterThan(55);
    expect(planDuration(long)).toBeLessThan(70);
    for (const p of [short, long]) {
      expect(p[0].kind).toBe('card');
      const last = p[p.length - 1];
      expect(last.kind === 'card' && last.end).toBe(true);
    }
  });

  it('puts a card before every change of scene, so loading never shows', () => {
    const p = buildPlan(30);
    let view: string | null = null;
    for (let i = 0; i < p.length; i++) {
      const s = p[i];
      if (s.kind !== 'shot') continue;
      if (s.view !== view) expect(p[i - 1].kind).toBe('card');
      view = s.view;
    }
  });

  it('visits all four scenes', () => {
    const views = new Set(buildPlan(30).flatMap((s) => (s.kind === 'shot' ? [s.view] : [])));
    expect([...views].sort()).toEqual(['casino', 'penthouse', 'poker', 'racetrack']);
  });

  it('finds the step at a time, and nothing after the end', () => {
    const p = buildPlan(30);
    expect(stepAt(p, 0)?.index).toBe(0);
    expect(stepAt(p, p[0].dur + 0.01)?.index).toBe(1);
    expect(stepAt(p, planDuration(p) + 0.1)).toBeNull();
  });

  it('loads the next shot\'s scene under a card, and keeps the last one under the end card', () => {
    const p = buildPlan(30);
    expect(viewFor(p, 0)).toBe('casino');
    expect(viewFor(p, p.length - 1)).toBe('racetrack');
  });

  it('varies the moves between takes', () => {
    const a = buildPlan(30, 0).flatMap((s) => (s.kind === 'shot' ? [s.shot.seed] : []));
    const b = buildPlan(30, 1).flatMap((s) => (s.kind === 'shot' ? [s.shot.seed] : []));
    expect(a).not.toEqual(b);
  });
});
