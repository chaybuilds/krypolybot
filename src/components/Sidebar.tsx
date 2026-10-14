import { startTransition, useEffect, useRef, useState } from 'react';
import {
  Activity, ArrowLeftRight, BarChart3, Bitcoin, Bot, BookOpen, Briefcase, Code2, Copy, FlaskConical, Folder,
  Compass, Info, LayoutDashboard, LineChart, ListChecks, Orbit, Receipt, Settings,
  Share2, Smartphone, Sparkles, SquareTerminal, Target, Users, Wallet,
} from 'lucide-react';
import { navTourId } from './tour/tourSteps';
import { useGuide } from './tour/useTour';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';
import { KryptSprite } from './KryptSprite';
import { FlexStatsCard } from './FlexStatsCard';
import { GlassPanel } from './glass/GlassPanel';
import { GlassLens } from './glass/GlassSegmented';
import { GLASS_TINTS } from './glass/glassPresets';
import { useGlideLens } from './glass/useGlideLens';
import type { PageId } from '../App';

type Tier = 'simple' | 'advanced';
type NavItem = {
  id: PageId;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  tier: Tier;
};

export const NAV_GROUPS: { title: string | null; label: string; hint?: string; items: NavItem[] }[] = [
  {
    title: 'Agents',
    label: 'Agents',
    hint: 'Your own AI agents: forecast first, paper by default',
    items: [
      { id: 'agents', label: 'AI Agents', icon: Bot, tier: 'simple' },
      { id: 'visualizer', label: 'Agent Hub', icon: Orbit, tier: 'simple' },
    ],
  },
  {
    title: null,
    label: 'Overview',
    items: [
      { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, tier: 'simple' },
    ],
  },
  {
    title: 'Terminal',
    label: 'Terminal',
    hint: 'You decide, and you place the order',
    items: [
      { id: 'discover', label: 'Discover', icon: Compass, tier: 'simple' },
      { id: 'usdesk', label: 'Polymarket US', icon: Bitcoin, tier: 'simple' },
      { id: 'market', label: 'Market', icon: LineChart, tier: 'simple' },
      { id: 'track', label: 'Track', icon: Receipt, tier: 'simple' },
    ],
  },
  {
    title: 'Automation',
    label: 'Automation',
    hint: 'Engines that trade while you are away',
    items: [
      { id: 'strategies', label: 'Strategies', icon: Target, tier: 'simple' },
      { id: 'main', label: 'Main Engine', icon: Sparkles, tier: 'advanced' },
      { id: 'crypto15m', label: 'Crypto', icon: Bitcoin, tier: 'advanced' },
      { id: 'perps', label: 'Perps', icon: ArrowLeftRight, tier: 'advanced' },
      { id: 'copy', label: 'Copy Trading', icon: Copy, tier: 'advanced' },
      { id: 'scripts', label: 'Scripts', icon: Code2, tier: 'advanced' },
      { id: 'positions', label: 'Bot positions', icon: Briefcase, tier: 'simple' },
      { id: 'signals', label: 'Signals', icon: Activity, tier: 'advanced' },
      { id: 'backtest', label: 'Backtest', icon: FlaskConical, tier: 'advanced' },
      { id: 'terminal', label: 'Cockpit HUD', icon: SquareTerminal, tier: 'advanced' },
    ],
  },
  {
    title: 'Account',
    label: 'Account',
    items: [
      { id: 'api', label: 'Wallet', icon: Wallet, tier: 'simple' },
      { id: 'history', label: 'History', icon: BarChart3, tier: 'advanced' },
      { id: 'profiles', label: 'Profiles', icon: Folder, tier: 'advanced' },
      { id: 'accounts', label: 'Accounts', icon: Users, tier: 'advanced' },
    ],
  },
  {
    title: 'App',
    label: 'App',
    items: [
      { id: 'remote', label: 'Remote', icon: Smartphone, tier: 'simple' },
      { id: 'settings', label: 'Settings', icon: Settings, tier: 'simple' },
      { id: 'logs', label: 'Logs', icon: ListChecks, tier: 'advanced' },
      { id: 'guide', label: 'Guide', icon: BookOpen, tier: 'simple' },
      { id: 'about', label: 'About', icon: Info, tier: 'advanced' },
    ],
  },
];

interface SidebarProps {
  page: PageId;
  setPage: (p: PageId) => void;
}

export function Sidebar({ page, setPage }: SidebarProps) {
  const { config, account, backend, state } = useApp();
  const { startTour } = useGuide();
  const live = config?.accountMode === 'live';
  const [acct, setAcct] = useState('Default');
  const [showStats, setShowStats] = useState(false);
  useEffect(() => { window.krypt.accounts.current().then(setAcct).catch(() => {}); }, []);

  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    navRef.current
      ?.querySelector(`[data-page="${page}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [page]);
  const simple = state?.uiMode === 'simple';
  const groups = NAV_GROUPS
    .map((g) => ({ ...g, items: simple ? g.items.filter((n) => n.tier === 'simple') : g.items }))
    .filter((g) => g.items.length > 0);

  const pillRef = useRef<HTMLDivElement | null>(null);
  const [lensKey, setLensKey] = useState<PageId>(page);
  useEffect(() => { setLensKey(page); }, [page]);
  const go = (id: PageId): void => {
    setLensKey(id);
    startTransition(() => setPage(id));
  };
  useGlideLens(navRef, pillRef, lensKey, 'y', [simple]);

  return (
    <>
    <GlassPanel
      preset="chrome"
      display="block"
      tint={GLASS_TINTS.chrome}
      className="h-full w-60 shrink-0 border-r border-white/[0.06]"
    >
    <aside className="flex h-full w-full flex-col">
      <div className="px-4 py-4">
        <div className="flex items-center gap-2">
          <KryptSprite size={38} title="Krypt" />
          <div className="min-w-0">
            <div className="font-pixel text-[10px] uppercase tracking-[0.18em] text-white/90">
              Krypt
            </div>
            <button
              onClick={() => setShowStats(true)}
              className="group flex items-center gap-1 text-xs text-krypt-muted transition-colors hover:text-white"
              title="Open your shareable stats card"
            >
              <Share2 className="h-3 w-3 text-krypt-purple opacity-80 transition-opacity group-hover:opacity-100" />
              <span className="underline-offset-2 group-hover:underline">
                {acct === 'Default' ? 'Krypt Stats' : `${acct} · Stats`}
              </span>
            </button>
          </div>
          <button
            type="button"
            onClick={startTour}
            data-tour="tour-info"
            aria-label="Take the app tour"
            title="Take the app tour"
            className="ml-auto grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-white/10 text-krypt-muted transition-colors hover:border-krypt-purple/50 hover:text-white"
          >
            <Compass className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <nav ref={navRef} className="relative flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">
        {groups.map((group, gi) => (
          <div key={group.title ?? `g${gi}`} className={cls(gi > 0 && 'mt-3')}>
            {group.title && (
              <div
                className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-krypt-dim"
                title={group.hint}
              >
                {group.title}
              </div>
            )}
            <div className="flex flex-col gap-0.5">
              {group.items.map(({ id, label, icon: Icon }) => {
                const active = lensKey === id;
                return (
                  <button
                    key={id}
                    data-page={id}
                    data-glass-key={id}
                    data-tour={navTourId(id)}
                    onClick={() => go(id)}
                    aria-current={active ? 'page' : undefined}
                    className={cls(
                      'group relative z-[1] flex items-center gap-3 rounded-[10px] px-3 py-2 text-sm transition-colors duration-200',
                      active
                        ? 'text-white'
                        : 'text-krypt-muted hover:bg-white/[0.035] hover:text-white',
                    )}
                  >
                    <Icon className={cls('h-4 w-4 transition-colors duration-200', active && 'text-sky-300 drop-shadow-[0_0_6px_rgba(56,189,248,0.6)]')} />
                    <span>{label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}

        <div className="relative hidden min-h-0 flex-1 select-none items-end justify-center pb-3 pt-4 [@media(min-height:960px)]:flex">
          <div className="pointer-events-none absolute bottom-2 h-3 w-16 rounded-[100%] bg-krypt-pink/25 blur-md" />
          <KryptSprite size={64} pet title="krypt" className="relative" />
        </div>
        <GlassLens lensRef={pillRef} />
      </nav>

      <div className="shrink-0 border-t border-white/[0.06] px-3 py-3" data-tour="wallet">
        <GlassPanel preset="card" tint={GLASS_TINTS.card} className="rounded-xl p-3">
          <div className="flex items-center gap-2 text-xs text-krypt-muted">
            <Wallet className="h-3.5 w-3.5" />
            <span className="uppercase tracking-wider">Wallet</span>
            <span
              data-testid="account-mode-pill"
              title={live
                ? 'LIVE: your real Polymarket wallet. Orders spend real money.'
                : 'PAPER: real Polymarket order books, imaginary money. The terminal, phone and agents trade paper, and the engines send nothing.'}
              className={cls(
                'ml-auto rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
                live
                  ? 'border-krypt-loss/50 bg-krypt-loss/15 text-krypt-loss'
                  : 'border-krypt-warn/50 bg-krypt-warn/15 text-krypt-warn',
              )}
            >
              {live ? 'live' : 'paper'}
            </span>
          </div>
          <div className="mt-1 font-mono text-lg tabular-nums text-white [text-shadow:0_0_18px_rgba(59,130,246,0.35)]">
            {fmtUsd(account?.totalUsd)}
          </div>
          <div className="text-[11px] text-krypt-dim">
            cash {fmtUsd(account?.cashUsd)} · port {fmtUsd(account?.portfolioUsd)}
          </div>
          <div className="mt-2 flex items-center justify-between text-[11px] text-krypt-muted">
            <span>{config?.enableTrading ? (live ? 'LIVE' : 'PAPER · engine held') : 'PAUSED'}</span>
            <span className={cls(
              'h-1.5 w-1.5 rounded-full',
              backend.status === 'running' ? 'bg-krypt-win text-krypt-win shadow-[0_0_8px_currentColor]' : 'bg-krypt-warn',
            )} />
          </div>
        </GlassPanel>
      </div>
    </aside>
    </GlassPanel>

    {showStats && <FlexStatsCard onClose={() => setShowStats(false)} />}
    </>
  );
}
