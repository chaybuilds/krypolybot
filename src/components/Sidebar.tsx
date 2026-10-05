import { useEffect, useRef, useState } from 'react';
import {
  Activity, ArrowLeftRight, BarChart3, Bitcoin, Bot, BookOpen, Briefcase, Code2, Copy, FlaskConical, Folder,
  Compass, Info, LayoutDashboard, LineChart, ListChecks, Orbit, Receipt, Settings,
  Share2, Smartphone, Sparkles, SquareTerminal, Target, Users, Wallet,
} from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';
import { KryptSprite } from './KryptSprite';
import { FlexStatsCard } from './FlexStatsCard';
import type { PageId } from '../App';

type Tier = 'simple' | 'advanced';
type NavItem = {
  id: PageId;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  tier: Tier;
};

const NAV_GROUPS: { title: string | null; hint?: string; items: NavItem[] }[] = [
  {
    title: null,
    items: [
      { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, tier: 'simple' },
    ],
  },
  {
    title: 'Terminal',
    hint: 'You decide, and you place the order',
    items: [
      { id: 'discover', label: 'Discover', icon: Compass, tier: 'simple' },
      { id: 'market', label: 'Market', icon: LineChart, tier: 'simple' },
      { id: 'track', label: 'Track', icon: Receipt, tier: 'simple' },
    ],
  },
  {
    title: 'Automation',
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
    items: [
      { id: 'api', label: 'Wallet', icon: Wallet, tier: 'simple' },
      { id: 'history', label: 'History', icon: BarChart3, tier: 'advanced' },
      { id: 'profiles', label: 'Profiles', icon: Folder, tier: 'advanced' },
      { id: 'accounts', label: 'Accounts', icon: Users, tier: 'advanced' },
    ],
  },
  {
    title: 'App',
    items: [
      { id: 'agents', label: 'AI Agents', icon: Bot, tier: 'simple' },
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
  const [acct, setAcct] = useState('Default');
  const [showStats, setShowStats] = useState(false);
  useEffect(() => { window.krypt.accounts.current().then(setAcct).catch(() => {}); }, []);

  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    navRef.current
      ?.querySelector(`[data-page="${page}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [page]);
  const simple = state?.uiMode === 'simple';
  const groups = NAV_GROUPS
    .map((g) => ({ ...g, items: simple ? g.items.filter((n) => n.tier === 'simple') : g.items }))
    .filter((g) => g.items.length > 0);

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-krypt-border bg-krypt-void/40">
      <div className="px-4 py-4">
        <div className="flex items-center gap-2">
          <KryptSprite size={38} title="Krypt" />
          <div>
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
        </div>
      </div>

      <nav ref={navRef} className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">
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
                const active = page === id;
                return (
                  <button
                    key={id}
                    data-page={id}
                    onClick={() => setPage(id)}
                    className={cls(
                      'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors',
                      active
                        ? 'bg-white/[0.06] text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.06)]'
                        : 'text-krypt-muted hover:bg-white/[0.03] hover:text-white',
                    )}
                  >
                    <Icon className={cls('h-4 w-4', active && 'text-krypt-purple')} />
                    <span>{label}</span>
                    {active && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-krypt-purple shadow-[0_0_8px_currentColor]" />}
                  </button>
                );
              })}
            </div>
          </div>
        ))}

        <div className="relative flex min-h-0 shrink select-none items-end justify-center overflow-hidden pb-3 pt-4">
          <div className="pointer-events-none absolute bottom-2 h-3 w-16 rounded-[100%] bg-krypt-pink/25 blur-md" />
          <KryptSprite size={64} pet title="krypt" className="relative" />
        </div>
      </nav>

      {!simple && (
      <div className="border-t border-krypt-border px-3 pt-3">
        <button
          onClick={() => setPage('visualizer')}
          className={cls(
            'group relative flex w-full items-center gap-2 overflow-hidden rounded-lg border px-3 py-2 text-sm transition-all',
            page === 'visualizer'
              ? 'border-krypt-purple/60 bg-krypt-purple/15 text-white shadow-[0_0_18px_rgba(59,130,246,0.25)]'
              : 'border-krypt-border bg-gradient-to-r from-krypt-purple/10 via-krypt-pink/5 to-transparent text-white hover:border-krypt-purple/40',
          )}
          title="Open the live trade visualizer"
        >
          <span className="relative grid h-7 w-7 place-items-center">
            <span className="absolute inset-0 rounded-full bg-krypt-purple/30 blur-md transition-opacity group-hover:opacity-80" />
            <Orbit className="relative h-4 w-4 text-krypt-purple animate-[spin_20s_linear_infinite]" />
          </span>
          <span className="text-xs font-medium uppercase tracking-wider">Live Visualizer</span>
          <span className="ml-auto h-1.5 w-1.5 rounded-full bg-krypt-purple shadow-[0_0_8px_currentColor]" />
        </button>
      </div>
      )}

      <div className="px-3 py-3">
        <div className="rounded-lg border border-krypt-border bg-krypt-surface p-3">
          <div className="flex items-center gap-2 text-xs text-krypt-muted">
            <Wallet className="h-3.5 w-3.5" />
            <span className="uppercase tracking-wider">Wallet</span>
            <span className="ml-auto rounded-full border border-krypt-purple/40 bg-krypt-purple/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-krypt-purple">
              {config?.network ?? 'mainnet'}
            </span>
          </div>
          <div className="mt-1 font-mono text-lg text-white">
            {fmtUsd(account?.totalUsd)}
          </div>
          <div className="text-[11px] text-krypt-dim">
            cash {fmtUsd(account?.cashUsd)} · port {fmtUsd(account?.portfolioUsd)}
          </div>
          <div className="mt-2 flex items-center justify-between text-[11px] text-krypt-muted">
            <span>{config?.enableTrading ? 'LIVE' : 'PAUSED'}</span>
            <span className={cls(
              'h-1.5 w-1.5 rounded-full',
              backend.status === 'running' ? 'bg-krypt-win' : 'bg-krypt-warn',
            )} />
          </div>
        </div>
      </div>

      {showStats && <FlexStatsCard onClose={() => setShowStats(false)} />}
    </aside>
  );
}
