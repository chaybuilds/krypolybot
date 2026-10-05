export interface SeriesPoint {
  t: number;
  p: number;
}

export interface GapPoint {
  t: number;
  p: number | null;
}

export const GAP_FACTOR = 3;

export function breakGaps(points: SeriesPoint[]): GapPoint[] {
  if (points.length < 3) return points.map((p) => ({ t: p.t, p: p.p }));

  const deltas: number[] = [];
  for (let i = 1; i < points.length; i++) deltas.push(points[i].t - points[i - 1].t);

  const median = [...deltas].sort((a, b) => a - b)[Math.floor(deltas.length / 2)] ?? 0;
  if (median <= 0) return points.map((p) => ({ t: p.t, p: p.p }));

  const out: GapPoint[] = [{ t: points[0].t, p: points[0].p }];
  for (let i = 1; i < points.length; i++) {
    const gap = points[i].t - points[i - 1].t;
    if (gap > median * GAP_FACTOR) {
      out.push({ t: points[i - 1].t + Math.floor(gap / 2), p: null });
    }
    out.push({ t: points[i].t, p: points[i].p });
  }
  return out;
}

export function countGaps(points: GapPoint[]): number {
  let n = 0;
  for (const p of points) if (p.p === null) n++;
  return n;
}
