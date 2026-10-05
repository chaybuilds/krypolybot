import { useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, FlaskConical, Play, Wand2 } from 'lucide-react';
import { PERPS_PRESETS } from '@shared/perpsPresets';
import type {
  PerpsBacktestArgs, PerpsBacktestMarket, PerpsBacktestResult, PerpsBacktestRun, TraderConfig,
} from '@shared/types';
import { useToast } from '../state/ToastProvider';
import { cls, fmtUsd } from '../utils/format';

const DAY_OPTIONS = [1, 3, 7, 14, 30];
const FEE_TIERS = ['0 — 0.0125% maker', '1 — 0.0100%', '2 — 0.0080%', '3 — 0.0050%', '4 — 0.0020%', '5 — 0.0000%', '6 — −0.0050% rebate'];

const DEFAULT_GRID = { dist: '1, 3, 5, 8', size: '100, 250, 500', skew: '2, 5, 10' };

function parseList(s: string): number[] {
  return s.split(/[,\s]+/).map((x) => Number(x)).filter((x) => Number.isFinite(x));
}

function score(r: PerpsBacktestRun): number {
  return r.totals.netPerDayUsd + (r.totals.estRewardPerDayUsd ?? 0);
}

export function PerpsBacktestPanel({ config }: { config: TraderConfig | null }) {
  const toast = useToast();
  const [mode, setMode] = useState<'backtest' | 'optimize'>('backtest');
  const [days, setDays] = useState(3);
  const [markets, setMarkets] = useState('');
  const [comparePresets, setComparePresets] = useState(true);
  const [grid, setGrid] = useState(DEFAULT_GRID);
  const [feeTier, setFeeTier] = useState(0);
  const [queueShare, setQueueShare] = useState(0.5);
  const [latencyMs, setLatencyMs] = useState(250);
  const [running, setRunning] = useState(false);
  const [res, setRes] = useState<PerpsBacktestResult | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const startedAt = useRef(0);

  const gridSize = parseList(grid.dist).length * parseList(grid.size).length * parseList(grid.skew).length;

  async function run() {
    const args: PerpsBacktestArgs = {
      days, feeTier, queueShare, latencyMs,
      markets: markets.trim() ? markets.split(/[,\s]+/).filter(Boolean).map((s) => s.toUpperCase()) : undefined,
    };
    if (mode === 'optimize') {
      args.grid = {
        perpsQuoteBps: parseList(grid.dist),
        perpsSizeUsd: parseList(grid.size),
        perpsSkewBps: parseList(grid.skew),
      };
    } else if (comparePresets) {
      args.variants = PERPS_PRESETS.map((p) => ({ name: p.name, config: p.patch }));
    }
    setRunning(true);
    startedAt.current = Date.now();
    try {
      const r = await window.krypt.perps.backtest(args);
      setRes(r);
      setOpen(null);
      if (!r.ok) toast.error(r.message || 'Backtest failed');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    } finally {
      setRunning(false);
    }
  }

  async function apply(r: PerpsBacktestRun) {
    if (!r.config || Object.keys(r.config).length === 0) return;
    const { perpsSelectMode: _m, perpsMarketCount: _n, perpsCategories: _c, ...knobs } = r.config;
    await window.krypt.config.update(knobs);
    toast.success(`Applied "${r.name}"`);
  }

  const runs = useMemo(
    () => [...(res?.runs ?? [])].sort((a, b) => score(b) - score(a)),
    [res],
  );

  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-4">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-krypt-muted">
          <FlaskConical className="h-3.5 w-3.5" /> Backtest
        </span>
        <div className="inline-flex overflow-hidden rounded-md border border-krypt-border">
          {(['backtest', 'optimize'] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={cls(
                'px-2.5 py-1 text-[11px] capitalize transition-colors',
                mode === m ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
              )}
            >
              {m}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-krypt-dim">
          replays the exchange&apos;s real trade tape (up to 30 days) through the engine&apos;s own quoting logic
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Field label="Window">
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} className={inputCls}>
            {DAY_OPTIONS.map((d) => <option key={d} value={d} className="bg-krypt-surface">{d} day{d > 1 ? 's' : ''}</option>)}
          </select>
        </Field>
        <Field label="Markets" hint="Blank = what the engine would pick with your current selection settings. Or list symbols: BTC-USD, ETH-USD">
          <input
            value={markets}
            onChange={(e) => setMarkets(e.target.value)}
            placeholder="engine's picks"
            className={inputCls}
          />
        </Field>
        <Field label="Fee tier" hint="Your 30-day volume tier. Most accounts start at 0.">
          <select value={feeTier} onChange={(e) => setFeeTier(Number(e.target.value))} className={inputCls}>
            {FEE_TIERS.map((f, i) => <option key={i} value={i} className="bg-krypt-surface">{f}</option>)}
          </select>
        </Field>
        <Field label="Queue share" hint="When a print hits exactly our price and we weren't first in line, the share of it we're assumed to get. Lower = more pessimistic.">
          <input type="number" min={0} max={1} step={0.1} value={queueShare}
            onChange={(e) => setQueueShare(Math.max(0, Math.min(1, Number(e.target.value))))} className={inputCls} />
        </Field>
        <Field label="Latency" hint="Milliseconds between deciding a quote and it resting on the book.">
          <input type="number" min={0} max={10000} step={50} value={latencyMs}
            onChange={(e) => setLatencyMs(Math.max(0, Number(e.target.value)))} className={inputCls} />
        </Field>
        <div className="flex items-end">
          <button
            onClick={() => void run()}
            disabled={running || (mode === 'optimize' && gridSize === 0)}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-krypt-accent/50 bg-krypt-accent/10 px-3 py-2 text-xs font-semibold text-krypt-accent hover:bg-krypt-accent/20 disabled:opacity-50"
          >
            {mode === 'optimize' ? <Wand2 className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            {running ? 'Running…' : mode === 'optimize' ? `Optimize (${Math.min(gridSize, 36)} runs)` : 'Run backtest'}
          </button>
        </div>
      </div>

      {mode === 'optimize' ? (
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
          <Field label="Quote distance (bps)"><input value={grid.dist} onChange={(e) => setGrid({ ...grid, dist: e.target.value })} className={inputCls} /></Field>
          <Field label="Size per quote ($)"><input value={grid.size} onChange={(e) => setGrid({ ...grid, size: e.target.value })} className={inputCls} /></Field>
          <Field label="Inventory skew (bps)"><input value={grid.skew} onChange={(e) => setGrid({ ...grid, skew: e.target.value })} className={inputCls} /></Field>
          <p className="text-[11px] text-krypt-dim sm:col-span-3">
            Every combination runs against your other current settings (max 36). Results rank by net P&amp;L per day plus the
            estimated reward — Apply copies a row&apos;s knobs into your settings.
            {gridSize > 36 && <span className="text-krypt-warn"> {gridSize} combinations — only the first 36 run.</span>}
          </p>
        </div>
      ) : (
        <label className="mt-2 inline-flex items-center gap-2 text-[11px] text-krypt-muted">
          <input type="checkbox" checked={comparePresets} onChange={(e) => setComparePresets(e.target.checked)} />
          Also run every preset for comparison
        </label>
      )}

      {running && (
        <div className="mt-3 text-[11px] text-krypt-dim">
          Fetching trade history (paced to stay under the exchange&apos;s rate limit) and replaying… a first run over
          many days can take a few minutes; the tape is cached, so reruns are seconds.
        </div>
      )}

      {res?.ok && runs.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-[11px] text-krypt-dim">
            {res.markets?.join(', ')} · {res.days} day{res.days === 1 ? '' : 's'} · maker fee {res.makerFeeBps?.toFixed(4)} bps
            · queue {res.queueShare} · latency {res.latencyMs}ms · {res.elapsedSec}s
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead className="text-krypt-dim">
                <tr className="border-b border-krypt-border">
                  <th className="px-2 py-1.5 text-left font-medium">Run</th>
                  <th className="px-2 py-1.5 text-right font-medium">Net / day</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="Estimated liquidity reward if you meet the 1% maker-share bar">Est. reward / day</th>
                  <th className="px-2 py-1.5 text-right font-medium">Volume / day</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="Net trading P&L per $1M of volume, before rewards — what volume costs (negative) or earns with these settings">Per $1M vol</th>
                  <th className="px-2 py-1.5 text-right font-medium">Fills</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="Average move one minute after each fill, from our side">Markout 1m</th>
                  <th className="px-2 py-1.5 text-right font-medium">Fees</th>
                  <th className="px-2 py-1.5 text-right font-medium">Funding</th>
                  <th className="px-2 py-1.5" />
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <RunRows key={r.name} r={r} open={open === r.name}
                    onToggle={() => setOpen(open === r.name ? null : r.name)} onApply={() => void apply(r)} />
                ))}
              </tbody>
            </table>
          </div>
          <ul className="mt-3 list-disc space-y-0.5 pl-5 text-[10px] leading-relaxed text-krypt-dim">
            {(res.assumptions ?? []).map((a) => <li key={a}>{a}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function RunRows({ r, open, onToggle, onApply }: {
  r: PerpsBacktestRun; open: boolean; onToggle: () => void; onApply: () => void;
}) {
  const t = r.totals;
  const canApply = !!r.config && Object.keys(r.config).length > 0;
  const perM = t.volumeUsd > 0 ? (t.netPnlUsd / t.volumeUsd) * 1e6 : null;
  return (
    <>
      <tr className="cursor-pointer border-b border-krypt-border/60 font-mono text-krypt-muted hover:bg-krypt-surface2/50" onClick={onToggle}>
        <td className="px-2 py-1.5 text-left font-sans text-white">
          {open ? <ChevronDown className="mr-1 inline h-3 w-3" /> : <ChevronRight className="mr-1 inline h-3 w-3" />}
          {r.name}
        </td>
        <td className={cls('px-2 py-1.5 text-right', t.netPerDayUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
          {fmtUsd(t.netPerDayUsd, { sign: true })}
        </td>
        <td className="px-2 py-1.5 text-right">{t.estRewardPerDayUsd != null ? fmtUsd(t.estRewardPerDayUsd) : '—'}</td>
        <td className="px-2 py-1.5 text-right">{fmtK(t.volumePerDayUsd)}</td>
        <td className={cls('px-2 py-1.5 text-right', perM != null && perM < 0 ? 'text-krypt-loss' : '')}>
          {perM != null ? fmtUsd(perM, { sign: true }) : '—'}
        </td>
        <td className="px-2 py-1.5 text-right">{t.fills}</td>
        <td className="px-2 py-1.5 text-right">{t.avgMarkoutBps != null ? `${t.avgMarkoutBps.toFixed(1)} bps` : '—'}</td>
        <td className="px-2 py-1.5 text-right">{fmtUsd(t.feesUsd)}</td>
        <td className="px-2 py-1.5 text-right">{fmtUsd(t.fundingUsd, { sign: true })}</td>
        <td className="px-2 py-1.5 text-right">
          {canApply && (
            <button
              onClick={(e) => { e.stopPropagation(); onApply(); }}
              className="rounded border border-krypt-accent/50 px-2 py-0.5 font-sans text-[10px] font-semibold text-krypt-accent hover:bg-krypt-accent/10"
            >
              Apply
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-b border-krypt-border/60 bg-krypt-surface2/30">
          <td colSpan={10} className="px-2 py-2">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
              {r.markets.map((m) => <MarketResult key={m.symbol} m={m} />)}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function MarketResult({ m }: { m: PerpsBacktestMarket }) {
  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-surface p-2.5 text-[11px]">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="font-semibold text-white">{m.symbol}</span>
        <span className={cls('font-mono', m.netPerDayUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
          {fmtUsd(m.netPerDayUsd, { sign: true })}/day
        </span>
      </div>
      <Sparkline points={m.equity} label={`${m.symbol} equity`} />
      <div className="mt-1.5 grid grid-cols-3 gap-x-2 gap-y-0.5 font-mono text-krypt-muted">
        <Kv k="Fills" v={`${m.fills}`} />
        <Kv k="Vol/day" v={fmtK(m.volumePerDayUsd)} />
        <Kv k="Markout" v={m.avgMarkoutBps != null ? `${m.avgMarkoutBps.toFixed(1)}bp` : '—'} />
        <Kv k="Uptime" v={`${m.uptimePct.toFixed(0)}%`} />
        <Kv k="Max inv" v={fmtUsd(m.maxInventoryUsd)} />
        <Kv k="Max DD" v={fmtUsd(m.maxDrawdownUsd)} />
        <Kv k="Realized" v={fmtUsd(m.realizedPnlUsd, { sign: true })} />
        <Kv k="Open P&L" v={fmtUsd(m.unrealizedPnlUsd, { sign: true })} />
        <Kv k="Reward/d" v={m.estRewardPerDayUsd != null ? fmtUsd(m.estRewardPerDayUsd) : '—'} />
      </div>
      <div className="mt-1 text-[10px] text-krypt-dim">
        {m.trades.toLocaleString()} prints · real touch {m.realTouchPct.toFixed(0)}%
        {!m.dataComplete && <span className="text-krypt-warn"> · history incomplete{m.dataError ? ` (${m.dataError})` : ''}</span>}
      </div>
    </div>
  );
}

function Sparkline({ points, label }: { points: [number, number][]; label: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 260;
  const H = 54;
  if (points.length < 2) return <div className="h-[54px] text-[10px] text-krypt-dim">not enough data</div>;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x0 = xs[0];
  const x1 = xs[xs.length - 1];
  const lo = Math.min(0, ...ys);
  const hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const px = (x: number) => ((x - x0) / (x1 - x0 || 1)) * (W - 4) + 2;
  const py = (y: number) => H - 3 - ((y - lo) / span) * (H - 6);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(' ');
  const h = hover != null ? points[hover] : null;
  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-[54px] w-full"
        role="img"
        aria-label={`${label}: ends at ${fmtUsd(ys[ys.length - 1], { sign: true })}`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const fx = ((e.clientX - r.left) / r.width) * W;
          let best = 0;
          for (let i = 1; i < points.length; i++) {
            if (Math.abs(px(points[i][0]) - fx) < Math.abs(px(points[best][0]) - fx)) best = i;
          }
          setHover(best);
        }}
      >
        <line x1={0} x2={W} y1={py(0)} y2={py(0)} className="stroke-krypt-border" strokeWidth={1} strokeDasharray="2 3" />
        <path d={d} fill="none" className="stroke-krypt-purple" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {h && (
          <>
            <line x1={px(h[0])} x2={px(h[0])} y1={0} y2={H} className="stroke-krypt-muted" strokeWidth={1} />
            <circle cx={px(h[0])} cy={py(h[1])} r={4} className="fill-krypt-purple stroke-krypt-surface" strokeWidth={2} />
          </>
        )}
      </svg>
      {h && (
        <div className="pointer-events-none absolute right-0 top-0 rounded border border-krypt-border bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-white">
          {new Date(h[0]).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
          {' · '}{fmtUsd(h[1], { sign: true })}
        </div>
      )}
    </div>
  );
}

function Kv({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex flex-col">
      <span className="font-sans text-[9px] uppercase tracking-wider text-krypt-dim">{k}</span>
      <span className="text-white">{v}</span>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block rounded-lg border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5" title={hint}>
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      {children}
    </label>
  );
}

const inputCls = 'mt-0.5 w-full bg-transparent font-mono text-sm text-white outline-none';

function fmtK(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}
