import { useState } from 'react';
import { Bot, Gauge, Globe, SlidersHorizontal } from 'lucide-react';
import type { PageId } from '../App';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { ConfirmDialog, ShareButton } from './common';
import { BossWidget } from './BossFight';
import { cls, fmtPct, fmtUsd } from '../utils/format';
import { PAPER_BADGE_TITLE } from '../utils/balance';
import { GlassPanel } from './glass/GlassPanel';
import { GlassSegmented } from './glass/GlassSegmented';
import { GLASS_TINTS } from './glass/glassPresets';
import { GoLiveAccountDialog, useBackToPaper } from './AccountModePanel';
import { agentsLiveOnLive, backToPaperPatch, liveAgents } from '../utils/goLive';
import { userMessage } from '../utils/errors';

const ENGINES: { key: 'main' | 'crypto' | 'perps' | 'copy' | 'scripts'; label: string }[] = [
  { key: 'main', label: 'Main' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'perps', label: 'Perps' },
  { key: 'copy', label: 'Copy' },
  { key: 'scripts', label: 'Scripts' },
];

export function TopBar({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { config, account, backend, state, refresh } = useApp();
  const uiMode = state?.uiMode ?? 'advanced';
  const [goLiveOpen, setGoLiveOpen] = useState(false);
  const toPaper = useBackToPaper();
  const toast = useToast();
  const [stopAsk, setStopAsk] = useState(false);
  const [stopping, setStopping] = useState(false);

  const live: Record<string, boolean> = {
    main: !!config?.enableTrading,
    crypto: !!config?.crypto15mEnabled,
    perps: !!config?.perpsEnabled,
    copy: !!config?.copyEnabled && !!(config?.copyWallets || []).length,
    scripts: !!config?.scriptsLiveEnabled,
  };
  const paperAccount = config?.accountMode !== 'live';
  const switchedOn = ENGINES.filter((e) => live[e.key]).length;
  const liveCount = paperAccount ? 0 : switchedOn;

  const agentsReal = agentsLiveOnLive(config);
  const agentsPaper = !agentsReal && !!config && config.mcpTradeMode !== 'off'
    && (!!config.mcpEnabled || !!config.autopilotEnabled);
  const agentNames = liveAgents(config).map((a) => a.name);
  const agentsTitle = agentsReal
    ? `AI agents LIVE on real money: ${agentNames.join(', ')}`
      + `${config?.mcpLiveApproval === false ? ' — approvals OFF, they order without asking' : ' — each order waits for your approval'}`
      + `${config?.autopilotEnabled ? '. Autopilot is on.' : '.'} Click to stop them.`
    : agentsPaper
    ? `AI agents trade on paper${config?.autopilotEnabled ? ' (Autopilot on)' : ''} — no real money. Go live for them on the AI Agents page.`
    : 'AI agents: trading off.';

  const stopAgents = async (): Promise<void> => {
    setStopping(true);
    try {
      await window.krypt.config.update(backToPaperPatch());
      await refresh?.state?.().catch(() => undefined);
      toast.success('AI agents are back on paper: no more real-money orders from them.');
      setStopAsk(false);
    } catch (e) {
      toast.error(`Could not stop the agents: ${userMessage(e)}`);
    } finally {
      setStopping(false);
    }
  };

  const measured = !!account && !account.backendDown;
  const num = (v: number | null | undefined): number | null =>
    (measured && typeof v === 'number' && !Number.isNaN(v) ? v : null);
  const balance = num(account?.totalUsd);
  const pnl = num(account?.sessionPnlUsd);
  const roi = num(account?.sessionRoiPct ?? account?.roiPct);

  return (
    <GlassPanel
      preset="chrome"
      display="flex"
      tint={GLASS_TINTS.chrome}
      className="relative z-20 shrink-0 items-center gap-4 border-b border-white/[0.06] px-6 py-3"
    >
      <GlassSegmented
        className="shrink-0"
        value={uiMode}
        onChange={(m) => void window.krypt.state.setUiMode(m)}
        options={[
          { value: 'simple', label: 'Simple', icon: <Gauge className="h-3.5 w-3.5" />,
            title: 'Just Home, Strategies, Positions, Wallet and Settings' },
          { value: 'advanced', label: 'Advanced', icon: <SlidersHorizontal className="h-3.5 w-3.5" />,
            title: 'Every page and engine the app has' },
        ]}
      />

      <div className="flex min-w-0 items-center gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <h1 className="glass-title truncate whitespace-nowrap text-lg font-semibold">
            {paperAccount && switchedOn > 0
              ? 'Paper: engines send nothing'
              : agentsReal
              ? (liveCount === 0
                ? 'AI agents live'
                : `${liveCount} engine${liveCount === 1 ? '' : 's'} + AI agents live`)
              : liveCount === 0
              ? 'All engines paused'
              : `${liveCount} engine${liveCount === 1 ? '' : 's'} live`}
          </h1>
          <span
            className={cls(
              'hidden shrink-0 whitespace-nowrap text-xs xl:inline',
              backend.status === 'running' ? 'text-krypt-muted' : 'text-krypt-warn',
            )}
          >
            · {backend.status === 'running' ? 'Backend online' : `Backend ${backend.status}`}
          </span>
        </div>

        {agentsReal && (
          <button
            type="button"
            data-testid="topbar-stop-agents"
            title={agentsTitle}
            disabled={stopping}
            onClick={() => setStopAsk(true)}
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-loss/60 bg-krypt-loss/15 px-2.5 py-1 text-[11px] font-semibold text-krypt-loss transition-colors hover:border-krypt-loss hover:bg-krypt-loss/25"
          >
            <Bot className="h-3.5 w-3.5" />
            Stop agents
          </button>
        )}

        {account?.tradingRestriction && (
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2.5 py-1 text-[11px] font-medium text-krypt-warn"
            title={account.tradingRestriction.message}
          >
            <Globe className="h-3.5 w-3.5" />
            {account.tradingRestriction.kind === 'restarting' ? 'Exchange restarting' : 'Post-only mode'}
          </span>
        )}
        {account?.tradingRegion?.status === 'close_only' ? (
          <span
            data-testid="topbar-region-chip"
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2.5 py-1 text-[11px] font-medium text-krypt-warn"
            title={
              'From the region this connection is in, Polymarket only lets you '
              + 'close existing positions: new buys are refused, sells of what '
              + 'you already hold still work.'}
          >
            <Globe className="h-3.5 w-3.5" />
            Close-only region
          </span>
        ) : (account?.tradingGeoblocked || account?.tradingRegion?.status === 'blocked') && (
          <span
            data-testid="topbar-region-chip"
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-loss/50 bg-krypt-loss/10 px-2.5 py-1 text-[11px] font-medium text-krypt-loss"
            title={
              "Polymarket doesn't allow trading from the region this connection "
              + 'is in, so live orders are refused. Market data still works, so '
              + 'everything else looks normal.'}
          >
            <Globe className="h-3.5 w-3.5" />
            Orders geoblocked
          </span>
        )}

        <div className="hidden items-center gap-1.5 min-[1760px]:flex">
          {ENGINES.map((e) => (
            <span
              key={e.key}
              title={live[e.key] && paperAccount
                ? `${e.label} engine is switched on, but the app is in Paper, so it sends nothing. Go live in Settings → Paper | Live.`
                : `${e.label} engine ${live[e.key] ? 'live' : 'off'} — switch it on its own page`}
              className={cls(
                'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                live[e.key] && paperAccount
                  ? 'bg-krypt-warn/15 text-krypt-warn'
                  : live[e.key]
                  ? 'bg-krypt-win/15 text-krypt-win'
                  : 'bg-krypt-surface2 text-krypt-dim',
              )}
            >
              {e.label}
            </span>
          ))}
          <button
            type="button"
            data-testid="topbar-agents-chip"
            title={agentsTitle}
            disabled={!agentsReal}
            onClick={() => setStopAsk(true)}
            className={cls(
              'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
              agentsReal
                ? 'bg-krypt-win/15 text-krypt-win hover:bg-krypt-win/25'
                : agentsPaper
                ? 'cursor-default bg-krypt-warn/15 text-krypt-warn'
                : 'cursor-default bg-krypt-surface2 text-krypt-dim',
            )}
          >
            Agents
          </button>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <BossWidget />
        <div className="hidden items-center gap-3 px-3 md:flex">
          {config && (paperAccount ? (
            <button
              type="button"
              data-testid="paper-pill"
              title={`${PAPER_BADGE_TITLE}\n\nClick to take the app live. Your AI agents go live separately, on the AI Agents page.`}
              onClick={() => setGoLiveOpen(true)}
              className="rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-warn transition-colors hover:border-krypt-warn hover:bg-krypt-warn/20"
            >
              Paper
            </button>
          ) : (
            <button
              type="button"
              data-testid="live-pill"
              title={'LIVE: orders spend the real USDC in your wallet.\n\nClick to switch back to Paper.'}
              disabled={toPaper.busy}
              onClick={() => void toPaper.ask()}
              className="rounded-full border border-krypt-loss/50 bg-krypt-loss/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-loss transition-colors hover:border-krypt-loss hover:bg-krypt-loss/20"
            >
              Live
            </button>
          ))}
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
      {toPaper.dialog}
      {goLiveOpen && <GoLiveAccountDialog onClose={() => setGoLiveOpen(false)} onNav={onNav} />}
      <ConfirmDialog
        open={stopAsk}
        danger
        title="Stop your AI agents trading real money?"
        body={(
          <>
            <p>
              Puts every AI agent back on paper{config?.autopilotEnabled ? ', Autopilot included' : ''}: no
              more real-money orders from {agentNames.length ? agentNames.join(', ') : 'any agent'}.
            </p>
            <p className="mt-2">
              The app stays Live for you and the engines. Positions the agents opened and any
              orders still resting stay as they are — the stops already set on them still fire.
              Go live for agents again on the AI Agents page.
            </p>
          </>
        )}
        confirmLabel={stopping ? 'Stopping…' : 'Stop agents'}
        onConfirm={() => { if (!stopping) void stopAgents(); }}
        onClose={() => setStopAsk(false)}
      />
    </GlassPanel>
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
