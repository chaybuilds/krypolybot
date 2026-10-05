import { useEffect, useMemo, useState } from 'react';
import { Clock, Trophy, UserPlus, Users } from 'lucide-react';
import type { BiggestWinner, MarketHolder, OfficialPnl, ResolutionState } from '@shared/types';
import { cls, fmtUsd } from '../utils/format';

const signed = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : fmtUsd(v, { sign: true });

export function OfficialPnlCard() {
  const [interval, setIntervalSel] = useState<'1d' | '1w' | '1m' | 'max'>('1w');
  const [data, setData] = useState<OfficialPnl | null>(null);
  useEffect(() => {
    let live = true;
    void window.krypt.polydata?.userPnl(interval).then((r) => { if (live) setData(r); }).catch(() => {});
    return () => { live = false; };
  }, [interval]);
  if (data && !data.ok) {
    return (
      <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface px-3 py-2 text-[11px] text-krypt-dim">
        Polymarket&apos;s own P&amp;L: {data.message}
      </div>
    );
  }
  const rebates = (data?.maker_rebate ?? 0) + (data?.taker_rebate ?? 0);
  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold uppercase tracking-wider text-krypt-muted">Polymarket&apos;s books</span>
        <span className="text-[11px] text-krypt-dim">the wallet&apos;s P&amp;L as Polymarket computes it — including rebates and rewards it pays separately</span>
        <div className="ml-auto inline-flex overflow-hidden rounded-md border border-krypt-border">
          {(['1d', '1w', '1m', 'max'] as const).map((iv) => (
            <button
              key={iv}
              onClick={() => setIntervalSel(iv)}
              className={cls('px-2 py-0.5 text-[11px]',
                interval === iv ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white')}
            >
              {iv === 'max' ? 'All' : iv}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Kv label="Economic P&L" value={signed(data?.economic_pnl)} tone={data?.economic_pnl} big />
        <Kv label="Realized" value={signed(data?.realized_pnl)} tone={data?.realized_pnl} />
        <Kv label="Unrealized (now)" value={signed(data?.unrealized_pnl)} tone={data?.unrealized_pnl} />
        <Kv label="Fees paid" value={data?.fees_paid != null ? fmtUsd(data.fees_paid) : '—'} />
        <Kv label="Rebates" value={signed(rebates)} tone={rebates} title={`maker ${signed(data?.maker_rebate)} · taker ${signed(data?.taker_rebate)}`} />
        <Kv label="Rewards + referrals" value={signed((data?.reward_income ?? 0) + (data?.referral_income ?? 0))} />
        <Kv label="Volume" value={data?.volume_usdc != null ? fmtUsd(data.volume_usdc) : '—'} />
        <div className="ml-auto min-w-[180px] flex-1">
          <Spark points={data?.series ?? []} />
        </div>
      </div>
    </div>
  );
}

function Spark({ points }: { points: [number, number][] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return null;
  const W = 240, H = 40;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const px = (x: number) => ((x - xs[0]) / ((xs[xs.length - 1] - xs[0]) || 1)) * (W - 4) + 2;
  const py = (y: number) => H - 3 - ((y - lo) / span) * (H - 6);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(' ');
  const h = hover != null ? points[hover] : null;
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-10 w-full" role="img"
        aria-label={`Economic P&L over the window, ending ${signed(ys[ys.length - 1])}`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const fx = ((e.clientX - r.left) / r.width) * W;
          let best = 0;
          for (let i = 1; i < points.length; i++) if (Math.abs(px(points[i][0]) - fx) < Math.abs(px(points[best][0]) - fx)) best = i;
          setHover(best);
        }}>
        <line x1={0} x2={W} y1={py(0)} y2={py(0)} className="stroke-krypt-border" strokeDasharray="2 3" />
        <path d={d} fill="none" className="stroke-krypt-purple" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {h && <circle cx={px(h[0])} cy={py(h[1])} r={4} className="fill-krypt-purple stroke-krypt-surface" strokeWidth={2} />}
      </svg>
      {h && (
        <div className="pointer-events-none absolute -top-5 right-0 rounded border border-krypt-border bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-white">
          {new Date(h[0] * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit' })} · {signed(h[1])}
        </div>
      )}
    </div>
  );
}

function Kv({ label, value, tone, big, title }: {
  label: string; value: string; tone?: number | null; big?: boolean; title?: string;
}) {
  return (
    <div className="flex flex-col" title={title}>
      <span className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</span>
      <span className={cls('font-mono', big ? 'text-base' : 'text-sm',
        tone == null || tone === 0 ? 'text-white' : tone > 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
        {value}
      </span>
    </div>
  );
}

export function useResolutions(conditionIds: string[]): Record<string, ResolutionState> {
  const key = useMemo(() => [...new Set(conditionIds)].sort().join(','), [conditionIds]);
  const [res, setRes] = useState<Record<string, ResolutionState>>({});
  useEffect(() => {
    if (!key) return;
    let live = true;
    const load = () => window.krypt.polydata?.resolutions(key.split(','))
      .then((r) => { if (live && r?.ok) setRes(r.resolutions); }).catch(() => {});
    void load();
    const t = window.setInterval(() => void load(), 60_000);
    return () => { live = false; window.clearInterval(t); };
  }, [key]);
  return res;
}

const BASIS_LABEL: Record<string, string> = {
  managed_proposal_expiration: 'challenge window ends',
  proposal_expiration: 'challenge window ends',
  liveness: 'proposal window ends',
  dvm_round_estimate: 'earliest UMA vote',
};

export function ResolutionBadge({ r }: { r?: ResolutionState }) {
  if (!r || !r.status) return null;
  const st = r.status.toLowerCase();
  const IN_FLIGHT = ['proposed', 'challenged', 'reproposed', 'disputed', 'arbitration'];
  if (!IN_FLIGHT.includes(st)) return null;
  const eta = r.expectedSettlementTime ? new Date(r.expectedSettlementTime) : null;
  const basis = r.settlementTimeBasis ? (BASIS_LABEL[r.settlementTimeBasis] ?? r.settlementTimeBasis) : '';
  const disputed = r.wasDisputed || st === 'disputed' || st === 'challenged' || st === 'arbitration';
  return (
    <span
      className={cls('flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider',
        disputed ? 'border-krypt-warn/50 text-krypt-warn' : 'border-krypt-border text-krypt-muted')}
      title={[
        `Resolution: ${r.status}${r.wasDisputed ? ' (disputed)' : ''}${r.extendedReview ? ' — under extended review' : ''}.`,
        eta ? `Polymarket estimates settlement around ${eta.toLocaleString()} (${basis}). An estimate, not a deadline: a disputed vote can roll into a later round.` : '',
      ].filter(Boolean).join(' ')}
    >
      <Clock className="h-3 w-3" />
      {disputed ? 'disputed' : 'resolving'}
      {eta && ` · ~${eta.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`}
    </span>
  );
}

export function TopHoldersPanel({ conditionId, outcomes }: { conditionId: string; outcomes?: string[] }) {
  const [rows, setRows] = useState<MarketHolder[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setRows(null);
    void window.krypt.polydata?.holders(conditionId, 8).then((r) => {
      if (!live) return;
      if (r.ok) setRows(r.holders); else setErr(r.message ?? 'unavailable');
    }).catch((e) => setErr(String(e)));
    return () => { live = false; };
  }, [conditionId]);
  const byOutcome = useMemo(() => {
    const m = new Map<number, MarketHolder[]>();
    for (const h of rows ?? []) {
      const k = h.outcomeIndex ?? 0;
      m.set(k, [...(m.get(k) ?? []), h]);
    }
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [rows]);
  return (
    <div className="rounded-xl border border-krypt-border bg-krypt-surface p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-krypt-muted">
        <Users className="h-3.5 w-3.5" /> Top holders
        <span className="ml-auto text-[10px] font-normal normal-case tracking-normal text-krypt-dim">P&amp;L on this position</span>
      </div>
      {err && <div className="text-[11px] text-krypt-dim">{err}</div>}
      {!rows && !err && <div className="text-[11px] text-krypt-dim">Loading…</div>}
      {rows && rows.length === 0 && <div className="text-[11px] text-krypt-dim">No holders reported.</div>}
      {byOutcome.map(([idx, list]) => (
        <div key={idx} className="mb-2 last:mb-0">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-krypt-dim">{outcomes?.[idx] ?? `Outcome ${idx}`}</div>
          <table className="w-full text-[11px]">
            <tbody>
              {list.slice(0, 5).map((h) => (
                <tr key={h.wallet + idx} className="border-t border-krypt-border/50 font-mono text-krypt-muted">
                  <td className="py-0.5 pr-2 font-sans text-white" title={h.wallet}>
                    {h.name || `${h.wallet.slice(0, 6)}…${h.wallet.slice(-4)}`}
                  </td>
                  <td className="py-0.5 pr-2 text-right">{h.currentValue != null ? fmtUsd(h.currentValue) : '—'}</td>
                  <td className="py-0.5 pr-2 text-right">{h.avgPrice != null ? `@${(h.avgPrice * 100).toFixed(1)}¢` : ''}</td>
                  <td className={cls('py-0.5 text-right', (h.totalPnl ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {signed(h.totalPnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

export function BiggestWinnersPanel({ following, onFollow }: {
  following: string[];
  onFollow: (wallet: string) => void;
}) {
  const [period, setPeriod] = useState<'day' | 'week' | 'month'>('week');
  const [rows, setRows] = useState<BiggestWinner[] | null>(null);
  useEffect(() => {
    let live = true;
    setRows(null);
    void window.krypt.polydata?.biggestWinners(period).then((r) => { if (live) setRows(r.ok ? r.winners : []); })
      .catch(() => setRows([]));
    return () => { live = false; };
  }, [period]);
  const followed = new Set(following.map((w) => w.toLowerCase()));
  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="inline-flex items-center gap-1.5 font-semibold uppercase tracking-wider text-krypt-muted">
          <Trophy className="h-3.5 w-3.5" /> Biggest winners
        </span>
        <span className="text-[11px] text-krypt-dim">largest resolved wins on Polymarket — one big win is not a track record; check a wallet before following it</span>
        <div className="ml-auto inline-flex overflow-hidden rounded-md border border-krypt-border">
          {(['day', 'week', 'month'] as const).map((p) => (
            <button key={p} onClick={() => setPeriod(p)}
              className={cls('px-2 py-0.5 text-[11px] capitalize',
                period === p ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white')}>
              {p}
            </button>
          ))}
        </div>
      </div>
      {!rows ? (
        <div className="text-[11px] text-krypt-dim">Loading…</div>
      ) : (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-[11px]">
            <tbody>
              {rows.slice(0, 20).map((w) => (
                <tr key={`${w.rank}-${w.wallet}`} className="border-t border-krypt-border/50 text-krypt-muted">
                  <td className="py-1 pr-2 font-mono text-krypt-dim">#{w.rank}</td>
                  <td className="py-1 pr-2 text-white" title={w.wallet}>{w.name || `${w.wallet.slice(0, 6)}…${w.wallet.slice(-4)}`}</td>
                  <td className="max-w-[16rem] truncate py-1 pr-2" title={w.eventTitle}>{w.eventTitle}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-win">{signed(w.pnl)}</td>
                  <td className="py-1 text-right">
                    {followed.has(w.wallet.toLowerCase()) ? (
                      <span className="text-[10px] text-krypt-dim">following</span>
                    ) : (
                      <button onClick={() => onFollow(w.wallet)}
                        className="inline-flex items-center gap-1 rounded border border-krypt-accent/50 px-1.5 py-0.5 text-[10px] font-semibold text-krypt-accent hover:bg-krypt-accent/10">
                        <UserPlus className="h-3 w-3" /> Follow
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
