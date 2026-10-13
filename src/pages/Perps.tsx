import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeftRight, Ban, FolderPlus, Link2, Radio, RefreshCw, RotateCcw, SlidersHorizontal, Trophy, Unlink,
  XCircle,
} from 'lucide-react';
import { PERPS_PRESETS } from '@shared/perpsPresets';
import type {
  PerpsBoard, PerpsCategory, PerpsEngineMarket, PerpsFill, PerpsMarketRow, PerpsStatus, TraderConfig,
} from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Empty, NameDialog, Page, Switch } from '../components/common';
import { PerpsBacktestPanel } from '../components/PerpsBacktestPanel';
import { cls, fmtUsd } from '../utils/format';
import { NumField, SelectField } from './Crypto15m';

const POLL_MS = 2500;

let cachedBoard: PerpsBoard | null = null;
let cachedStatus: PerpsStatus | null = null;

const CATEGORIES: { id: PerpsCategory; label: string }[] = [
  { id: 'crypto', label: 'Crypto' },
  { id: 'index', label: 'Indices' },
  { id: 'commodity', label: 'Commodities' },
  { id: 'equity', label: 'Stocks' },
];

const DEFAULTS = {
  perpsSelectMode: 'rewards' as const,
  perpsMarketCount: 4,
  perpsQuoteBps: 3,
  perpsLevels: 1,
  perpsLevelStepBps: 3,
  perpsSizeUsd: 100,
  perpsMaxInventoryUsd: 300,
  perpsSkewBps: 4,
  perpsRequoteBps: 1.5,
  perpsVolPullBps: 20,
  perpsVolWindowSec: 10,
  perpsPullCooldownSec: 20,
  perpsMaxIndexDevBps: 40,
  perpsLeverage: 3,
  perpsMarkoutKillBps: 15,
  perpsMarkoutMinFills: 20,
  perpsPollSec: 2,
};

function fmtPx(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const dp = v >= 1000 ? 1 : v >= 10 ? 3 : v >= 1 ? 4 : 6;
  return v.toLocaleString(undefined, { maximumFractionDigits: dp });
}

function fmtBps(v: number | null | undefined, dp = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(dp)}`;
}

function fmtK(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

export function PerpsPage() {
  const { config } = useApp();
  const toast = useToast();
  const [board, setBoard] = useState<PerpsBoard | null>(cachedBoard);
  const [status, setStatus] = useState<PerpsStatus | null>(cachedStatus);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const inFlight = useRef(false);

  const [uiMode, setUiMode] = useState<'simple' | 'advanced'>(
    () => (localStorage.getItem('perpsUiMode') === 'advanced' ? 'advanced' : 'simple'),
  );
  const setMode = (m: 'simple' | 'advanced') => {
    setUiMode(m);
    localStorage.setItem('perpsUiMode', m);
  };

  async function load() {
    const api = window.krypt?.perps;
    if (!api) {
      setErr('Perps API unavailable (restart the app after this update).');
      return;
    }
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const [b, s] = await Promise.all([api.board(), api.status()]);
      cachedBoard = b;
      cachedStatus = s;
      setBoard(b);
      setStatus(s);
      setErr(b && !b.ok && b.error ? b.error : null);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      inFlight.current = false;
    }
  }

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(t);
  }, []);

  const update = async (patch: Partial<TraderConfig>) => {
    try { await window.krypt.config.update(patch); } catch { }
  };

  async function action(id: string, fn: () => Promise<{ ok: boolean; message: string }>) {
    setBusy(id);
    try {
      const r = await fn();
      if (r.ok) toast.success(r.message);
      else toast.error(r.message);
      await load();
    } catch (e: any) {
      toast.error(e?.message || String(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleEnabled(next: boolean) {
    if (next && !window.confirm(
      'Enable the Perps market maker?\n\nIt will rest REAL post-only orders on Polymarket Perps '
      + 'using your Perps pUSD balance, and fills open leveraged positions. It runs independently '
      + 'of every other engine. Positions are never closed automatically — use Flatten.',
    )) return;
    await update({ perpsEnabled: next });
    await load();
  }

  const enabled = !!config?.perpsEnabled;
  const linked = !!status?.link?.linked;
  const mode: 'OFF' | 'WAITING' | 'LIVE' = !enabled ? 'OFF' : status?.trading ? 'LIVE' : 'WAITING';
  const boardBySym = useMemo(() => {
    const m = new Map<number, PerpsMarketRow>();
    for (const r of board?.markets ?? []) m.set(r.instrumentId, r);
    return m;
  }, [board]);
  const today = status?.today;
  const resting = (status?.markets ?? []).reduce((n, m) => n + m.resting.length, 0);

  return (
    <Page
      title="Perps"
      subtitle="Polymarket Perps market maker. Rests post-only quotes around the mid to build maker volume and earn the $75k/day liquidity-rewards pool — tune where it quotes, how big, and how it handles inventory below."
      actions={
        <>
          <div className="inline-flex overflow-hidden rounded-md border border-krypt-border">
            {(['simple', 'advanced'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={cls(
                  'px-3 py-1.5 text-xs capitalize transition-colors',
                  uiMode === m ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
                )}
              >
                {m}
              </button>
            ))}
          </div>
          <button
            onClick={() => void load()}
            className="inline-flex items-center gap-2 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </>
      }
    >
      <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="min-w-[260px] flex-1">
            <Switch
              checked={enabled}
              disabled={busy !== null}
              onChange={(v) => void toggleEnabled(v)}
              label="Enable Perps market maker"
              description={
                mode === 'LIVE'
                  ? 'LIVE — resting real quotes on Polymarket Perps.'
                  : mode === 'WAITING'
                    ? (linked ? 'Enabled — paused (see below).' : 'Enabled — link your Perps account to start.')
                    : 'Off — preview only. The cards show what it would quote.'
              }
            />
          </div>
          <ModePill mode={mode} />
          <FeedChip status={status} realtime={config?.perpsRealtime ?? true} />
          <div className="flex flex-wrap items-center gap-4 text-xs">
            <KV label="Volume today" value={fmtK(today?.volumeUsd)} />
            <KV label="Fees" value={fmtUsd(today?.feesUsd ?? 0)} />
            <KV
              label="Net P&L"
              value={fmtUsd(today?.netPnlUsd ?? 0, { sign: true })}
              accent={(today?.netPnlUsd ?? 0) >= 0 ? 'good' : 'bad'}
            />
            <KV label="Quotes" value={`${resting}`} />
          </div>
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <button
            disabled={busy !== null || !linked}
            onClick={() => void action('cancel', () => window.krypt.perps.cancelAll())}
            className="inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-warn/40 hover:text-krypt-warn disabled:opacity-40"
          >
            <Ban className="h-3 w-3" /> Cancel my quotes
          </button>
          <button
            disabled={busy !== null || !linked}
            onClick={() => {
              if (!window.confirm('Close EVERY open Perps position now with reduce-only market orders (up to 0.5% through the touch)? Turn the engine off first, or it will start quoting again next tick.')) return;
              void action('flatten', () => window.krypt.perps.flatten());
            }}
            className="inline-flex items-center gap-1 rounded-md border border-krypt-loss/40 bg-krypt-loss/10 px-2.5 py-1 text-[11px] text-krypt-loss transition-colors hover:bg-krypt-loss/20 disabled:opacity-40"
          >
            <XCircle className="h-3 w-3" /> Flatten all positions
          </button>
        </div>
      </div>

      <LinkCard status={status} busy={busy} onAction={action} />

      {status?.haltReason && (
        <div className="mb-4 rounded-lg border border-krypt-warn/50 bg-krypt-warn/10 px-3 py-2 text-xs text-krypt-warn">
          <span className="font-semibold">Quoting paused by a breaker: </span>{status.haltReason}
        </div>
      )}
      {status?.account?.inLiquidation && (
        <div className="mb-4 rounded-lg border border-krypt-loss/50 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          ⛔ <span className="font-semibold">Your Perps account is in liquidation.</span> New orders are blocked by the exchange.
        </div>
      )}
      {status?.lastError && (
        <div className="mb-4 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          {status.lastError}
        </div>
      )}
      {err && (
        <div className="mb-4 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          {err}
        </div>
      )}

      <RewardsPanel board={board} status={status} />

      <StrategySettings config={config} uiMode={uiMode} board={board} />

      {uiMode === 'advanced' && <PerpsBacktestPanel config={config} />}

      <MarketControls config={config} onPatch={update} />

      {!board && !status ? (
        <Empty title="Loading Perps markets…" description="Fetching the Polymarket Perps board." />
      ) : (status?.markets?.length ?? 0) === 0 ? (
        <Empty
          title={linked ? 'No markets selected yet' : 'Link your Perps account to see live quotes'}
          description={linked
            ? 'The engine picks markets from the board below on its next tick.'
            : 'The board below works without an account. Linking lets the engine read your balance and positions.'}
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {status!.markets.map((m) => (
            <MarketCard key={m.instrumentId} m={m} row={boardBySym.get(m.instrumentId)} />
          ))}
        </div>
      )}

      <BoardTable board={board} config={config} onPatch={update} uiMode={uiMode} />

      {(status?.recentFills?.length ?? 0) > 0 && <FillsTable rows={status!.recentFills} />}
    </Page>
  );
}

function LinkCard({ status, busy, onAction }: {
  status: PerpsStatus | null;
  busy: string | null;
  onAction: (id: string, fn: () => Promise<{ ok: boolean; message: string }>) => Promise<void>;
}) {
  const link = status?.link;
  const acct = status?.account;
  if (!status) return null;
  if (!link?.linked) {
    return (
      <div className="mb-4 rounded-xl border border-krypt-purple/40 bg-krypt-purple/5 p-3 text-xs">
        <div className="flex flex-wrap items-center gap-3">
          <Link2 className="h-4 w-4 text-krypt-purple" />
          <div className="min-w-[240px] flex-1 leading-relaxed text-krypt-muted">
            <span className="font-semibold text-white">Link your Perps account.</span>{' '}
            Your wallet signs once to authorise a trading key for Polymarket Perps (a separate exchange
            with its own pUSD balance — deposit on polymarket.com/perps). The key is stored encrypted next to
            your wallet key and expires after 7 days.{' '}
            {link?.expired && <span className="text-krypt-warn">Your previous link expired. </span>}
            {!link?.walletPresent && <span className="text-krypt-warn">Connect your wallet on the Wallet page first. </span>}
            Not available from the US, Canada and some other regions.
          </div>
          <button
            disabled={busy !== null || !link?.walletPresent}
            onClick={() => void onAction('link', () => window.krypt.perps.link())}
            className="rounded-md border border-krypt-accent/50 bg-krypt-accent/10 px-3 py-1.5 text-xs font-semibold text-krypt-accent hover:bg-krypt-accent/20 disabled:opacity-40"
          >
            {busy === 'link' ? 'Linking…' : 'Link Perps account'}
          </button>
        </div>
      </div>
    );
  }
  const exp = link.expiresAt ? new Date(link.expiresAt) : null;
  const unfunded = acct && (acct.equityUsd ?? 0) <= 0;
  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <span className="inline-flex items-center gap-1.5 font-semibold uppercase tracking-wider text-krypt-dim">
          <ArrowLeftRight className="h-3.5 w-3.5" /> Perps account
        </span>
        <KV label="Equity" value={fmtUsd(acct?.equityUsd ?? 0)} />
        <KV label="Free margin" value={fmtUsd(acct?.availableMarginUsd ?? 0)} />
        <KV label="Positions" value={fmtUsd(acct?.positionValueUsd ?? 0)} />
        <KV label="Fee tier" value={`${acct?.feeTier ?? 0}`} />
        <span className="text-krypt-dim">
          signer <span className="font-mono">{link.owner ? `${link.owner.slice(0, 6)}…${link.owner.slice(-4)}` : '—'}</span>
          {exp && <> · key expires {exp.toLocaleString()} (renewed automatically while the engine runs)</>}
        </span>
        <button
          disabled={busy !== null}
          onClick={() => {
            if (!window.confirm('Unlink the Perps account? Your quotes are cancelled first; positions stay open.')) return;
            void onAction('unlink', () => window.krypt.perps.unlink());
          }}
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-0.5 text-[11px] text-krypt-dim hover:text-white disabled:opacity-40"
        >
          <Unlink className="h-3 w-3" /> Unlink
        </button>
      </div>
      {unfunded && (
        <div className="mt-2 text-[11px] text-krypt-warn">
          Your Perps balance is empty — deposit pUSD to Perps on polymarket.com before enabling the engine.
        </div>
      )}
      {link.renewError && (
        <div className="mt-2 text-[11px] text-krypt-warn">
          Couldn't renew the Perps trading key: {link.renewError}. The engine retries every 10 minutes and pulls
          its quotes 15 minutes before the key expires — unlink and link again to renew it yourself.
        </div>
      )}
    </div>
  );
}

function RewardsPanel({ board, status }: { board: PerpsBoard | null; status: PerpsStatus | null }) {
  const need = board?.makerVol7dNeededUsd ?? 0;
  const mine = status?.stats7d?.makerVolumeUsd ?? status?.localWeek?.makerVolumeUsd ?? 0;
  const share = status?.stats7d?.makerShare;
  const pct = need > 0 ? Math.min(1, mine / need) : 0;
  const rewards = status?.rewards ?? [];
  const rewardSum = rewards.reduce((s, r) => s + Number((r as any).amount ?? (r as any).reward ?? 0), 0);
  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="inline-flex items-center gap-1.5 font-semibold uppercase tracking-wider text-krypt-muted">
          <Trophy className="h-3.5 w-3.5" /> Liquidity rewards
        </span>
        <KV label="Pool / market / day" value={fmtUsd(board?.poolPerMarketUsd ?? 0)} />
        <KV label="Markets" value={`${board?.activeMarkets ?? 0}`} />
        <KV label="Exchange 7d volume" value={fmtK(board?.exchangeVol7dUsd)} />
        {rewards.length > 0 && <KV label="Rewards (recent)" value={fmtUsd(rewardSum)} accent="good" />}
      </div>
      <div className="flex items-center gap-3 text-[11px]">
        <span className="w-28 shrink-0 text-krypt-dim">Maker share 7d</span>
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-krypt-surface2">
          <div
            className={cls('h-full rounded-full', pct >= 1 ? 'bg-krypt-win' : 'bg-krypt-purple')}
            style={{ width: `${Math.max(1, pct * 100)}%` }}
          />
        </div>
        <span className="w-64 shrink-0 text-right font-mono text-krypt-muted">
          {fmtK(mine)} / {fmtK(need)}
          {share !== null && share !== undefined && ` · ${(share * 100).toFixed(2)}%`}
        </span>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
        Rewards need ≥1% of the exchange&apos;s trailing 7-day maker volume
        {(board?.volumeCoverage ?? 1) < 0.95 && ' (total still sampling)'}.
        Below it you earn nothing, whatever you quote. Above it, each market&apos;s pool splits by resting size
        within 5/10/20 bps of mid (weights 1 / 0.25 / 0.1, $100k cap per tier) × uptime. Estimates on the cards
        assume one competitor with your maker score; treat them as a ranking, not a payout.
      </p>
    </div>
  );
}

function StrategySettings({ config, uiMode, board }: {
  config: TraderConfig | null;
  uiMode: 'simple' | 'advanced';
  board: PerpsBoard | null;
}) {
  const [applying, setApplying] = useState<string | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const toast = useToast();
  const update = async (patch: Partial<TraderConfig>) => {
    try { await window.krypt.config.update(patch); } catch { }
  };
  const saveProfile = async (name: string): Promise<void> => {
    setSaveOpen(false);
    const r = await window.krypt.profiles.save(name, undefined, 'perps');
    if (r.ok) toast.success(r.message || 'Saved perps profile');
    else toast.error(r.message || 'Failed to save profile');
  };
  const num = (k: keyof TraderConfig, d: number) => {
    const v = config?.[k] as number | undefined;
    return typeof v === 'number' && !Number.isNaN(v) ? v : d;
  };
  const apply = async (p: typeof PERPS_PRESETS[number]) => {
    setApplying(p.id);
    try { await update(p.patch); } finally { setApplying(null); }
  };
  const reset = () => void update({
    perpsQuoteBps: DEFAULTS.perpsQuoteBps, perpsLevels: DEFAULTS.perpsLevels,
    perpsLevelStepBps: DEFAULTS.perpsLevelStepBps, perpsSizeUsd: DEFAULTS.perpsSizeUsd,
    perpsMaxInventoryUsd: DEFAULTS.perpsMaxInventoryUsd, perpsSkewBps: DEFAULTS.perpsSkewBps,
    perpsRequoteBps: DEFAULTS.perpsRequoteBps, perpsVolPullBps: DEFAULTS.perpsVolPullBps,
    perpsVolWindowSec: DEFAULTS.perpsVolWindowSec, perpsPullCooldownSec: DEFAULTS.perpsPullCooldownSec,
    perpsMaxIndexDevBps: DEFAULTS.perpsMaxIndexDevBps, perpsLeverage: DEFAULTS.perpsLeverage,
    perpsMarkoutKillBps: DEFAULTS.perpsMarkoutKillBps, perpsMarkoutMinFills: DEFAULTS.perpsMarkoutMinFills,
    perpsPollSec: DEFAULTS.perpsPollSec, perpsRealtime: true, perpsBookRefreshSec: 10,
  });
  const size = num('perpsSizeUsd', DEFAULTS.perpsSizeUsd);
  const levels = num('perpsLevels', DEFAULTS.perpsLevels);
  const count = num('perpsMarketCount', DEFAULTS.perpsMarketCount);
  const lev = num('perpsLeverage', DEFAULTS.perpsLeverage);
  const marginNeed = (count * (size * levels + num('perpsMaxInventoryUsd', DEFAULTS.perpsMaxInventoryUsd))) / Math.max(1, lev);

  const fields = (
    <>
      <NumField
        label="Quote distance" suffix="bps" min={0} max={100} step={0.5}
        value={num('perpsQuoteBps', DEFAULTS.perpsQuoteBps)}
        hint="How far each quote sits from the (inventory-skewed) mid. ≤5 bps earns full reward weight; 0 = join the touch for maximum fills/volume. Wider = fewer fills, less adverse selection, less reward score."
        onCommit={(v) => void update({ perpsQuoteBps: v })}
      />
      <NumField
        label="Size per quote" suffix="$" min={10} max={100000} step={10}
        value={size}
        hint="Notional of each resting quote (per side, per level). Rewards cap credited size at $100k per tier; Perps minimum is $10."
        onCommit={(v) => void update({ perpsSizeUsd: v })}
      />
      <NumField
        label="Max inventory" suffix="$" min={10} max={1000000} step={10}
        value={num('perpsMaxInventoryUsd', DEFAULTS.perpsMaxInventoryUsd)}
        hint="Per market. At this position size the side that would add to it stops quoting, and the skew leans hard toward unloading."
        onCommit={(v) => void update({ perpsMaxInventoryUsd: v })}
      />
      <NumField
        label="Daily loss stop" suffix="$" min={0} max={1000000} step={5}
        value={Math.abs(num('perpsDailyLossLimit', -50))}
        hint="Stop quoting for the UTC day once today's realized P&L after fees plus open unrealized is down this much. Positions are left for you. 0 = off."
        onCommit={(v) => void update({ perpsDailyLossLimit: -Math.abs(v) })}
      />
      <NumField
        label="Volume target / day" suffix="$" min={0} max={1e12} step={1000}
        value={num('perpsDailyVolumeTargetUsd', 0)}
        hint="Stop quoting for the UTC day once today's traded volume reaches this. 0 = off."
        onCommit={(v) => void update({ perpsDailyVolumeTargetUsd: v })}
      />
      <NumField
        label="Leverage" suffix="×" min={1} max={50} step={1}
        value={lev}
        hint="Set per market before its first quote (capped at the market's max). Lower leverage = more margin per quote, further liquidation."
        onCommit={(v) => void update({ perpsLeverage: Math.round(v) })}
      />
    </>
  );

  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-4">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-krypt-muted">
          <SlidersHorizontal className="h-3.5 w-3.5" /> Strategy settings
        </span>
        <span className="text-[11px] text-krypt-dim">changes apply live</span>
        <span className="ml-auto text-[11px] text-krypt-dim">
          needs ≈ {fmtUsd(marginNeed)} margin at {lev}× · your quote scores {fmtK(board?.myQuoteScore ?? 0)}/side
        </span>
      </div>

      <div className="mb-3 flex flex-wrap gap-2">
        {PERPS_PRESETS.map((p) => (
          <button
            key={p.id}
            onClick={() => void apply(p)}
            disabled={applying !== null}
            title={p.hint}
            className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white disabled:opacity-50"
          >
            {p.name}
          </button>
        ))}
        <button
          onClick={() => setSaveOpen(true)}
          title="Save these Perps settings as a profile (Profiles → Perps)"
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white"
        >
          <FolderPlus className="h-3 w-3" /> Save as profile
        </button>
        <button
          onClick={reset}
          className="inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-dim transition-colors hover:border-krypt-warn/40 hover:text-krypt-warn"
        >
          <RotateCcw className="h-3 w-3" /> Reset
        </button>
      </div>

      <NameDialog
        open={saveOpen}
        title="Save perps profile"
        label="Saves your current Perps settings — quoting, markets, risk — as a profile (Profiles → Perps). Applying a profile never switches the engine on."
        placeholder="Profile name"
        confirmLabel="Save"
        onSubmit={(name) => void saveProfile(name)}
        onClose={() => setSaveOpen(false)}
      />

      {uiMode === 'simple' ? (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">{fields}</div>
          <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
            Pick a preset, choose where it quotes below, set size and the loss stop, link your account and
            flip it on. Ladders, skew, pull triggers and the markout kill switch live in{' '}
            <span className="font-semibold text-krypt-muted">Advanced</span>.
          </p>
        </>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {fields}
          <NumField
            label="Levels per side" min={1} max={3} step={1}
            value={levels}
            hint="Quotes per side. Extra levels sit further out by the step below."
            onCommit={(v) => void update({ perpsLevels: Math.round(v) })}
          />
          <NumField
            label="Level step" suffix="bps" min={0.5} max={50} step={0.5}
            value={num('perpsLevelStepBps', DEFAULTS.perpsLevelStepBps)}
            hint="Extra distance for each additional level."
            onCommit={(v) => void update({ perpsLevelStepBps: v })}
          />
          <NumField
            label="Inventory skew" suffix="bps" min={0} max={100} step={0.5}
            value={num('perpsSkewBps', DEFAULTS.perpsSkewBps)}
            hint="At full inventory the mid we quote around shifts this far against the position — long → both quotes lower (sell sooner, buy later). Scales linearly with position / max inventory."
            onCommit={(v) => void update({ perpsSkewBps: v })}
          />
          <NumField
            label="Requote band" suffix="bps" min={0.1} max={50} step={0.1}
            value={num('perpsRequoteBps', DEFAULTS.perpsRequoteBps)}
            hint="A resting quote within this many bps of where it should be is left alone (keeps queue priority). Lower = tracks mid tighter, more cancel/replace traffic."
            onCommit={(v) => void update({ perpsRequoteBps: v })}
          />
          <NumField
            label="Pull on move" suffix="bps" min={0} max={1000} step={1}
            value={num('perpsVolPullBps', DEFAULTS.perpsVolPullBps)}
            hint="Cancel a market's quotes when its mid moves more than this inside the window below — fast moves are where makers get picked off. 0 = off."
            onCommit={(v) => void update({ perpsVolPullBps: v })}
          />
          <NumField
            label="Move window" suffix="s" min={2} max={300} step={1}
            value={num('perpsVolWindowSec', DEFAULTS.perpsVolWindowSec)}
            hint="Window for the pull-on-move check."
            onCommit={(v) => void update({ perpsVolWindowSec: Math.round(v) })}
          />
          <NumField
            label="Pull cooldown" suffix="s" min={0} max={3600} step={1}
            value={num('perpsPullCooldownSec', DEFAULTS.perpsPullCooldownSec)}
            hint="How long a pulled market stays out before quoting again."
            onCommit={(v) => void update({ perpsPullCooldownSec: Math.round(v) })}
          />
          <NumField
            label="Max index gap" suffix="bps" min={0} max={1000} step={1}
            value={num('perpsMaxIndexDevBps', DEFAULTS.perpsMaxIndexDevBps)}
            hint="Pull a market whose book mid is this far from the exchange's index price — a dislocated book is usually about to snap back through your quotes. 0 = off."
            onCommit={(v) => void update({ perpsMaxIndexDevBps: v })}
          />
          <NumField
            label="Markout kill" suffix="bps" min={0} max={1000} step={0.5}
            value={num('perpsMarkoutKillBps', DEFAULTS.perpsMarkoutKillBps)}
            hint="Turn a market off for the UTC day when its recent maker fills average worse than −this many bps one minute later (you're being picked off). 0 = off."
            onCommit={(v) => void update({ perpsMarkoutKillBps: v })}
          />
          <NumField
            label="…over fills" min={5} max={500} step={1}
            value={num('perpsMarkoutMinFills', DEFAULTS.perpsMarkoutMinFills)}
            hint="How many recent maker fills the markout kill averages."
            onCommit={(v) => void update({ perpsMarkoutMinFills: Math.round(v) })}
          />
          <NumField
            label="Lifetime loss stop" suffix="$" min={0} max={1e9} step={10}
            value={num('perpsLifetimeLossLimitUsd', 0)}
            hint="Stop quoting once all imported Perps fills (after fees) plus open unrealized are down this much. 0 = off."
            onCommit={(v) => void update({ perpsLifetimeLossLimitUsd: Math.abs(v) })}
          />
          <SelectField
            label="Margin mode" value={config?.perpsCrossMargin ? 'cross' : 'isolated'}
            options={[['isolated', 'Isolated'], ['cross', 'Cross']]}
            hint="Isolated keeps each market's losses to its own margin. Cross shares your whole Perps balance (fewer liquidations, bigger blast radius)."
            onCommit={(v) => void update({ perpsCrossMargin: v === 'cross' })}
          />
          <SelectField
            label="Live prices" value={(config?.perpsRealtime ?? true) ? 'on' : 'off'}
            options={[['on', 'On (WebSocket)'], ['off', 'Off (poll only)']]}
            hint="On: a live price feed re-centres quotes within milliseconds of a move, between full ticks. Off: quotes only move on each tick. REST is always the fallback."
            onCommit={(v) => void update({ perpsRealtime: v === 'on' })}
          />
          <NumField
            label="Depth refresh" suffix="s" min={2} max={300} step={1}
            value={num('perpsBookRefreshSec', 10)}
            hint="With live prices on, how often each market's full order book is re-read for the competition score."
            onCommit={(v) => void update({ perpsBookRefreshSec: Math.round(v) })}
          />
          <NumField
            label="Tick every" suffix="s" min={1} max={30} step={1}
            value={num('perpsPollSec', DEFAULTS.perpsPollSec)}
            hint="Engine cadence. Faster tracks the mid tighter (and uses more API calls)."
            onCommit={(v) => void update({ perpsPollSec: Math.round(v) })}
          />
        </div>
      )}
    </div>
  );
}

function MarketControls({ config, onPatch }: {
  config: TraderConfig | null;
  onPatch: (p: Partial<TraderConfig>) => Promise<void>;
}) {
  const mode = config?.perpsSelectMode ?? 'rewards';
  const cats = config?.perpsCategories ?? ['crypto'];
  const toggleCat = (c: PerpsCategory) => {
    const next = cats.includes(c) ? cats.filter((x) => x !== c) : [...cats, c];
    void onPatch({ perpsCategories: next });
  };
  const modes: { id: 'rewards' | 'volume' | 'manual'; label: string; hint: string }[] = [
    { id: 'rewards', label: 'Best rewards', hint: 'Auto-pick the markets where your quote earns the biggest share of the reward pool (thin books).' },
    { id: 'volume', label: 'Most volume', hint: 'Auto-pick the deepest, busiest markets — the most fills per dollar quoted.' },
    { id: 'manual', label: 'Manual', hint: 'Quote exactly the markets you tick in the board below.' },
  ];
  return (
    <div className="mb-4 flex flex-col gap-3 rounded-xl border border-krypt-border bg-krypt-surface p-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-wider text-krypt-dim">Markets</span>
        <div className="inline-flex overflow-hidden rounded-lg border border-krypt-border">
          {modes.map((m) => (
            <button
              key={m.id}
              title={m.hint}
              onClick={() => void onPatch({ perpsSelectMode: m.id })}
              className={cls(
                'px-3 py-1 text-xs transition-colors',
                mode === m.id ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
              )}
            >
              {m.label}
            </button>
          ))}
        </div>
        {mode !== 'manual' && (
          <label className="inline-flex items-center gap-1.5 text-[11px] text-krypt-dim">
            top
            <input
              type="number" min={1} max={30}
              value={config?.perpsMarketCount ?? 4}
              onChange={(e) => void onPatch({ perpsMarketCount: Math.max(1, Math.min(30, Number(e.target.value) || 1)) })}
              className="w-12 rounded border border-krypt-border bg-krypt-surface2 px-1.5 py-0.5 font-mono text-xs text-white outline-none"
            />
          </label>
        )}
        {mode === 'manual' && (
          <span className="text-[11px] text-krypt-dim">
            {(config?.perpsMarkets ?? []).length} ticked — tick markets in the board below
          </span>
        )}
      </div>
      {mode !== 'manual' && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[11px] uppercase tracking-wider text-krypt-dim">From</span>
          {CATEGORIES.map((c) => (
            <button
              key={c.id}
              onClick={() => toggleCat(c.id)}
              className={cls(
                'rounded-md border px-2 py-0.5 text-[11px] font-semibold transition-colors',
                cats.includes(c.id)
                  ? 'border-krypt-purple/40 bg-krypt-purple/10 text-white'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-dim line-through',
              )}
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function MarketCard({ m, row }: { m: PerpsEngineMarket; row?: PerpsMarketRow }) {
  const quotes = m.resting.length > 0 ? m.resting : m.desired;
  const live = m.resting.length > 0;
  const bids = quotes.filter((q) => q.buy).sort((a, b) => b.price - a.price);
  const asks = quotes.filter((q) => !q.buy).sort((a, b) => a.price - b.price);
  const mid = m.mid ?? row?.midPrice ?? null;
  const dist = (px: number) => (mid ? ((px - mid) / mid) * 1e4 : null);
  const ring = cls(
    !m.selected && 'opacity-70',
    m.state === 'quoting' ? 'border-krypt-win/50'
      : m.state === 'pulled' || m.state === 'blocked' ? 'border-krypt-warn/50'
        : m.state === 'killed' || m.state === 'error' || m.state === 'halted' ? 'border-krypt-loss/50'
          : 'border-krypt-border',
  );
  return (
    <div className={cls('krypt-card flex flex-col gap-2.5 border', ring)}>
      <div className="flex items-center gap-2">
        <div className="leading-tight">
          <div className="text-sm font-semibold text-white">{row?.displaySymbol ?? m.symbol}</div>
          <div className="font-mono text-[10px] text-krypt-dim">
            {row?.category ?? ''}{row ? ` · ${row.maxLeverage}× max` : ''}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          {m.priceSource && (
            <span
              title={m.priceSource === 'live' ? 'Quoting off the live WebSocket touch' : 'Quoting off a REST book read'}
              className={cls('rounded px-1.5 py-0.5 font-mono text-[9px] uppercase',
                m.priceSource === 'live' ? 'bg-krypt-purple/15 text-krypt-purple' : 'bg-krypt-surface2 text-krypt-dim')}
            >
              {m.priceSource}
            </span>
          )}
          <StatePill state={m.state} />
        </div>
      </div>
      {m.note && <div className="text-[11px] leading-snug text-krypt-warn">{m.note}</div>}

      <div className="grid grid-cols-2 gap-2">
        <QuoteBox label={live ? 'BIDS (resting)' : 'BIDS (planned)'} quotes={bids} dist={dist} good />
        <QuoteBox label={live ? 'ASKS (resting)' : 'ASKS (planned)'} quotes={asks} dist={dist} />
      </div>

      <div className="grid grid-cols-3 gap-x-2 gap-y-1 text-[11px]">
        <Foot label="Mid" value={fmtPx(mid)} />
        <Foot label="Spread" value={row?.spreadBps != null ? `${row.spreadBps.toFixed(1)} bps` : '—'} />
        <Foot label="Index gap" value={row?.indexDevBps != null ? `${fmtBps(row.indexDevBps)} bps` : '—'} />
        <Foot label="Est. reward" value={row?.estRewardUsd != null ? `${fmtUsd(row.estRewardUsd)}/d` : '—'} />
        <Foot label="Competing" value={fmtK(m.competingScore ?? row?.competingScore)} />
        <Foot label="Funding APR" value={row?.fundingAprPct != null ? `${row.fundingAprPct.toFixed(1)}%` : '—'} />
        <Foot
          label="Position"
          value={m.positionQty ? `${m.positionQty > 0 ? '+' : ''}${m.positionQty} (${fmtUsd(m.positionValueUsd ?? 0)})` : 'flat'}
        />
        <Foot
          label="uPnL"
          value={m.unrealizedPnlUsd != null ? fmtUsd(m.unrealizedPnlUsd, { sign: true }) : '—'}
          accent={(m.unrealizedPnlUsd ?? 0) >= 0 ? 'good' : 'bad'}
        />
        <Foot
          label="Markout 1m"
          value={m.avgMarkoutBps != null ? `${fmtBps(m.avgMarkoutBps)} bps (${m.markoutN})` : '—'}
          accent={m.avgMarkoutBps == null ? undefined : m.avgMarkoutBps >= 0 ? 'good' : 'bad'}
        />
        <Foot label="Today vol" value={fmtK(m.todayVolumeUsd)} />
        <Foot label="Today fills" value={`${m.todayFills}`} />
        <Foot
          label="Today P&L"
          value={fmtUsd(m.todayPnlUsd, { sign: true })}
          accent={m.todayPnlUsd >= 0 ? 'good' : 'bad'}
        />
      </div>
    </div>
  );
}

function QuoteBox({ label, quotes, dist, good }: {
  label: string;
  quotes: { price: number; qty: number }[];
  dist: (px: number) => number | null;
  good?: boolean;
}) {
  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-surface2 px-2.5 py-2">
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      {quotes.length === 0 ? (
        <div className="mt-1 font-mono text-xs text-krypt-dim">none</div>
      ) : quotes.map((q, i) => (
        <div key={i} className="mt-0.5 flex items-baseline justify-between gap-2 font-mono text-xs">
          <span className={good ? 'text-krypt-win' : 'text-krypt-loss'}>{fmtPx(q.price)}</span>
          <span className="text-krypt-dim">{fmtBps(dist(q.price))}bp</span>
        </div>
      ))}
    </div>
  );
}

function StatePill({ state }: { state: PerpsEngineMarket['state'] }) {
  const sty: Record<string, string> = {
    quoting: 'bg-krypt-win/15 text-krypt-win',
    pulled: 'bg-krypt-warn/15 text-krypt-warn',
    blocked: 'bg-krypt-warn/15 text-krypt-warn',
    killed: 'bg-krypt-loss/15 text-krypt-loss',
    halted: 'bg-krypt-loss/15 text-krypt-loss',
    error: 'bg-krypt-loss/15 text-krypt-loss',
    off: 'bg-krypt-surface2 text-krypt-muted',
    idle: 'bg-krypt-surface2 text-krypt-dim',
  };
  return (
    <span className={cls('rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider', sty[state] ?? sty.idle)}>
      {state === 'off' ? 'preview' : state}
    </span>
  );
}

type SortKey = 'estRewardUsd' | 'avgDailyVolUsd' | 'competingScore' | 'spreadBps' | 'fundingAprPct' | 'openInterestUsd';

function BoardTable({ board, config, onPatch, uiMode }: {
  board: PerpsBoard | null;
  config: TraderConfig | null;
  onPatch: (p: Partial<TraderConfig>) => Promise<void>;
  uiMode: 'simple' | 'advanced';
}) {
  const [sort, setSort] = useState<SortKey>('estRewardUsd');
  const [showAll, setShowAll] = useState(false);
  const manual = (config?.perpsSelectMode ?? 'rewards') === 'manual';
  const picked = new Set((config?.perpsMarkets ?? []).map((s) => s.toUpperCase()));
  const rows = useMemo(() => {
    const r = [...(board?.markets ?? [])].filter((x) => !x.closeOnly);
    const asc = sort === 'competingScore' || sort === 'spreadBps';
    r.sort((a, b) => {
      const va = a[sort] as number | null;
      const vb = b[sort] as number | null;
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return asc ? va - vb : vb - va;
    });
    return r;
  }, [board, sort]);
  if (!board || rows.length === 0) return null;
  const shown = showAll || uiMode === 'advanced' ? rows : rows.slice(0, 20);
  const toggle = (sym: string) => {
    const cur = config?.perpsMarkets ?? [];
    const next = picked.has(sym.toUpperCase()) ? cur.filter((s) => s.toUpperCase() !== sym.toUpperCase()) : [...cur, sym];
    void onPatch({ perpsMarkets: next });
  };
  const H = ({ k, children }: { k: SortKey; children: React.ReactNode }) => (
    <th
      onClick={() => setSort(k)}
      className={cls('cursor-pointer px-2 py-1.5 text-right font-medium hover:text-white', sort === k && 'text-white')}
    >
      {children}
    </th>
  );
  return (
    <div className="mt-6 rounded-xl border border-krypt-border bg-krypt-surface">
      <div className="flex items-center justify-between px-3 py-2 text-xs">
        <span className="font-semibold uppercase tracking-wider text-krypt-muted">Market board</span>
        <span className="text-[11px] text-krypt-dim">
          click a column to sort · competing = everyone else&apos;s reward-weighted depth near mid
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead className="text-krypt-dim">
            <tr className="border-t border-krypt-border">
              {manual && <th className="px-2 py-1.5" />}
              <th className="px-2 py-1.5 text-left font-medium">Market</th>
              <H k="estRewardUsd">Est. reward/d</H>
              <H k="competingScore">Competing</H>
              <H k="spreadBps">Spread</H>
              <H k="avgDailyVolUsd">Avg vol/d</H>
              <H k="openInterestUsd">OI</H>
              <H k="fundingAprPct">Funding APR</H>
              <th className="px-2 py-1.5 text-right font-medium">Index gap</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.instrumentId} className="border-t border-krypt-border/60 font-mono text-krypt-muted hover:bg-krypt-surface2/50">
                {manual && (
                  <td className="px-2 py-1">
                    <input type="checkbox" checked={picked.has(r.symbol.toUpperCase())} onChange={() => toggle(r.symbol)} />
                  </td>
                )}
                <td className="px-2 py-1 text-left font-sans text-white">
                  {r.displaySymbol} <span className="text-krypt-dim">{r.category}</span>
                </td>
                <td className="px-2 py-1 text-right text-krypt-win">{r.estRewardUsd != null ? fmtUsd(r.estRewardUsd) : '…'}</td>
                <td className="px-2 py-1 text-right">{r.competingScore != null ? fmtK(r.competingScore) : '…'}</td>
                <td className="px-2 py-1 text-right">{r.spreadBps != null ? `${r.spreadBps.toFixed(1)}` : '…'}</td>
                <td className="px-2 py-1 text-right">{fmtK(r.avgDailyVolUsd)}</td>
                <td className="px-2 py-1 text-right">{fmtK(r.openInterestUsd)}</td>
                <td className={cls('px-2 py-1 text-right', (r.fundingAprPct ?? 0) < 0 && 'text-krypt-loss')}>
                  {r.fundingAprPct != null ? `${r.fundingAprPct.toFixed(1)}%` : '—'}
                </td>
                <td className="px-2 py-1 text-right">{r.indexDevBps != null ? fmtBps(r.indexDevBps) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {uiMode === 'simple' && rows.length > 20 && (
        <button
          onClick={() => setShowAll((v) => !v)}
          className="w-full border-t border-krypt-border px-3 py-1.5 text-[11px] text-krypt-dim hover:text-white"
        >
          {showAll ? 'Show top 20' : `Show all ${rows.length} markets`}
        </button>
      )}
      <div className="border-t border-krypt-border px-3 py-1.5 text-[10px] text-krypt-dim">
        “…” = depth not sampled yet (the board samples a few books every refresh). Estimates use your current size and distance.
      </div>
    </div>
  );
}

function FillsTable({ rows }: { rows: PerpsFill[] }) {
  return (
    <div className="mt-6 rounded-xl border border-krypt-border bg-krypt-surface">
      <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wider text-krypt-muted">Recent fills</div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead className="text-krypt-dim">
            <tr className="border-t border-krypt-border">
              <th className="px-2 py-1.5 text-left font-medium">Time</th>
              <th className="px-2 py-1.5 text-left font-medium">Market</th>
              <th className="px-2 py-1.5 text-left font-medium">Side</th>
              <th className="px-2 py-1.5 text-right font-medium">Price</th>
              <th className="px-2 py-1.5 text-right font-medium">Notional</th>
              <th className="px-2 py-1.5 text-right font-medium">Fee</th>
              <th className="px-2 py-1.5 text-right font-medium">P&L</th>
              <th className="px-2 py-1.5 text-right font-medium">Markout 1m</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((f) => (
              <tr key={f.tradeId} className="border-t border-krypt-border/60 font-mono text-krypt-muted">
                <td className="px-2 py-1">{new Date(f.ts).toLocaleTimeString()}</td>
                <td className="px-2 py-1 font-sans text-white">{f.symbol}</td>
                <td className={cls('px-2 py-1', f.side === 'long' ? 'text-krypt-win' : 'text-krypt-loss')}>
                  {f.side === 'long' ? 'buy' : 'sell'}{f.taker ? ' · taker' : ''}
                </td>
                <td className="px-2 py-1 text-right">{fmtPx(f.price)}</td>
                <td className="px-2 py-1 text-right">{fmtUsd(f.notional)}</td>
                <td className="px-2 py-1 text-right">{fmtUsd(f.fee)}</td>
                <td className={cls('px-2 py-1 text-right', f.pnl >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                  {f.pnl ? fmtUsd(f.pnl, { sign: true }) : '—'}
                </td>
                <td className={cls('px-2 py-1 text-right', (f.markoutBps ?? 0) >= 0 ? 'text-krypt-muted' : 'text-krypt-loss')}>
                  {f.markoutBps != null ? fmtBps(f.markoutBps) : f.taker ? '—' : '…'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FeedChip({ status, realtime }: { status: PerpsStatus | null; realtime: boolean }) {
  const f = status?.feed;
  if (!realtime) {
    return <span className="text-[11px] text-krypt-dim">live prices off</span>;
  }
  const live = !!f?.connected;
  const idle = !f || f.subscribed.length === 0;
  const ago = f?.lastMessageAgoSec;
  return (
    <span
      title={live
        ? `WebSocket connected · ${f!.subscribed.length} market(s) · ${f!.messages.toLocaleString()} updates`
        : idle ? 'Connects when the engine quotes a market' : 'Reconnecting — quoting falls back to REST'}
      className={cls('inline-flex items-center gap-1.5 text-[11px]',
        live ? 'text-krypt-muted' : idle ? 'text-krypt-dim' : 'text-krypt-warn')}
    >
      <Radio className={cls('h-3.5 w-3.5', live ? 'text-krypt-win' : idle ? 'text-krypt-dim' : 'text-krypt-warn')} />
      {live
        ? `live · ${f!.subscribed.length} mkt${ago != null ? ` · ${ago < 1 ? '<1' : ago.toFixed(0)}s` : ''}`
        : idle ? 'live prices idle' : 'live prices reconnecting'}
    </span>
  );
}

function ModePill({ mode }: { mode: 'OFF' | 'WAITING' | 'LIVE' }) {
  const sty =
    mode === 'LIVE'
      ? 'border-krypt-loss/50 bg-krypt-loss/15 text-krypt-loss'
      : mode === 'WAITING'
        ? 'border-krypt-warn/50 bg-krypt-warn/15 text-krypt-warn'
        : 'border-krypt-border bg-krypt-surface2 text-krypt-muted';
  return (
    <span className={cls('rounded-full border px-3 py-1 text-[11px] font-bold uppercase tracking-wider', sty)}>
      {mode}
    </span>
  );
}

function KV({ label, value, accent }: { label: string; value: string; accent?: 'good' | 'bad' }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-krypt-dim">
      {label}
      <span className={cls('font-mono', accent === 'good' ? 'text-krypt-win' : accent === 'bad' ? 'text-krypt-loss' : 'text-white')}>
        {value}
      </span>
    </span>
  );
}

function Foot({ label, value, accent }: { label: string; value: string; accent?: 'good' | 'bad' }) {
  return (
    <div className="flex flex-col">
      <span className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</span>
      <span className={cls('font-mono', accent === 'good' ? 'text-krypt-win' : accent === 'bad' ? 'text-krypt-loss' : 'text-white')}>
        {value}
      </span>
    </div>
  );
}
