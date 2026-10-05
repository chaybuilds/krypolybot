import { Gauge, Globe, SlidersHorizontal } from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { ShareButton } from './common';
import { BossWidget } from './BossFight';
import { cls, fmtPct, fmtUsd } from '../utils/format';

const ENGINES: { key: 'main' | 'crypto' | 'perps' | 'copy' | 'scripts'; label: string }[] = [
  { key: 'main', label: 'Main' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'perps', label: 'Perps' },
  { key: 'copy', label: 'Copy' },
  { key: 'scripts', label: 'Scripts' },
];

export function TopBar() {
  const { config, account, backend, state } = useApp();
  const uiMode = state?.uiMode ?? 'advanced';

  const live: Record<string, boolean> = {
    main: !!config?.enableTrading,
    crypto: !!config?.crypto15mEnabled,
    perps: !!config?.perpsEnabled,
    copy: !!config?.copyEnabled && !!(config?.copyWallets || []).length,
    scripts: !!config?.scriptsLiveEnabled,
  };
  const liveCount = ENGINES.filter((e) => live[e.key]).length;

  const measured = !!account && !account.backendDown;
  const num = (v: number | null | undefined): number | null =>
    (measured && typeof v === 'number' && !Number.isNaN(v) ? v : null);
  const balance = num(account?.totalUsd);
  const pnl = num(account?.sessionPnlUsd);
  const roi = num(account?.sessionRoiPct ?? account?.roiPct);

  return (
    <header className="z-20 flex items-center gap-4 border-b border-krypt-border bg-krypt-void/70 px-6 py-3 backdrop-blur">
      <div className="inline-flex shrink-0 overflow-hidden rounded-lg border border-krypt-purple/40 shadow-[0_0_16px_rgba(59,130,246,0.18)]">
        {([
          { m: 'simple' as const, label: 'Simple', Icon: Gauge,
            tip: 'Just Home, Strategies, Positions, Wallet and Settings' },
          { m: 'advanced' as const, label: 'Advanced', Icon: SlidersHorizontal,
            tip: 'Every page and engine the app has' },
        ]).map(({ m, label, Icon }, i, arr) => (
          <button
            key={m}
            onClick={() => void window.krypt.state.setUiMode(m)}
            title={arr[i].tip}
            className={cls(
              'flex items-center gap-2 px-4 py-2 text-sm font-semibold uppercase tracking-wider transition-colors',
              uiMode === m
                ? 'bg-krypt-purple text-white shadow-[inset_0_0_12px_rgba(255,255,255,0.15)]'
                : 'bg-krypt-surface2 text-krypt-muted hover:bg-krypt-surface hover:text-white',
            )}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      <div className="flex min-w-0 items-center gap-3">
        <div className="flex items-baseline gap-2">
          <h1 className="text-lg font-semibold text-white">
            {liveCount === 0
              ? 'All engines paused'
              : `${liveCount} engine${liveCount === 1 ? '' : 's'} live`}
          </h1>
          <span
            className={cls(
              'text-xs',
              backend.status === 'running' ? 'text-krypt-muted' : 'text-krypt-warn',
            )}
          >
            · {backend.status === 'running' ? 'Backend online' : `Backend ${backend.status}`}
          </span>
        </div>

        {account?.tradingRestriction && (
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2.5 py-1 text-[11px] font-medium text-krypt-warn"
            title={account.tradingRestriction.message}
          >
            <Globe className="h-3.5 w-3.5" />
            {account.tradingRestriction.kind === 'restarting' ? 'Exchange restarting' : 'Post-only mode'}
          </span>
        )}
        {account?.tradingGeoblocked && (
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-loss/50 bg-krypt-loss/10 px-2.5 py-1 text-[11px] font-medium text-krypt-loss"
            title={
              'Polymarket refused a recent order because of the region this '
              + 'connection appears to come from. Market data still works and '
              + 'the app looks healthy, but no order can fill until the '
              + 'connection leaves the blocked region.'}
          >
            <Globe className="h-3.5 w-3.5" />
            Orders geoblocked
          </span>
        )}

        <div className="hidden items-center gap-1.5 lg:flex">
          {ENGINES.map((e) => (
            <span
              key={e.key}
              title={`${e.label} engine ${live[e.key] ? 'live' : 'off'} — switch it on its own page`}
              className={cls(
                'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                live[e.key]
                  ? 'bg-krypt-win/15 text-krypt-win'
                  : 'bg-krypt-surface2 text-krypt-dim',
              )}
            >
              {e.label}
            </span>
          ))}
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <BossWidget />
        <div className="hidden items-center gap-3 px-3 md:flex">
          <Stat label="Balance" value={balance === null ? DASH : fmtUsd(balance)}
            title={balance === null ? UNKNOWN_HINT : undefined} />
          <Stat
            label="Session P&L"
            value={pnl === null ? DASH : fmtUsd(pnl, { sign: true })}
            title={pnl === null ? UNKNOWN_HINT : undefined}
            color={pnl === null ? 'text-krypt-dim' : pnl >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}
          />
          <Stat
            label="ROI"
            value={roi === null ? DASH : fmtPct(roi)}
            title={roi === null ? UNKNOWN_HINT : undefined}
            color={roi === null ? 'text-krypt-dim' : roi >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}
          />
          {balance !== null && pnl !== null && roi !== null && (
            <ShareButton
              size="xs"
              text={
                `Krypt PolyBot: ${fmtUsd(balance)} balance · `
                + `${fmtUsd(pnl, { sign: true })} this session · `
                + `${fmtPct(roi)} ROI. `
                + `Free Polymarket auto-trader by @YuhgoSlavia · krypt.cc/tools/polybot`
              }
            />
          )}
        </div>
      </div>
    </header>
  );
}

const DASH = '—';
const UNKNOWN_HINT =
  'Nobody could produce this value — the backend is not running, so no balance was read.';

function Stat({
  label, value, color, title,
}: { label: string; value: string; color?: string; title?: string }) {
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="text-[10px] uppercase tracking-wider text-krypt-muted">
        {label}
      </span>
      <span className={cls('font-mono text-sm', color || 'text-white')} title={title}>
        {value}
      </span>
    </div>
  );
}
