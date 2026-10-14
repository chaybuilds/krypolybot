import { useCallback, useEffect, useRef, useState } from 'react';
import { TitleBar } from './components/TitleBar';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { GlassBackdrop } from './components/glass/GlassBackdrop';
import { AppStateProvider, useApp } from './state/AppStateProvider';
import { ToastProvider, useToast } from './state/ToastProvider';
import { GuideHost } from './components/tour/GuideHost';
import { PageGuard, usePersistedPage } from './components/PageGuard';
import { autoOnboarding } from '@shared/onboarding';
import { agentCallEvent, agentEvent, publishActivity } from './state/activity';
import { agentOrderMessage, agentOrderToast } from '@shared/agentOrderNotice';
import { useWindowVisibility } from './state/visibility';
import { startAmbientClock } from './state/ambient';
import { kickGlassWatchdog, startGlassWatchdog } from './components/glass/glassQuality';
import { DashboardPage } from './pages/Dashboard';
import { MainEnginePage } from './pages/MainEngine';
import { SettingsPage } from './pages/Settings';
import { PositionsPage } from './pages/Positions';
import { SignalsPage } from './pages/Signals';
import { HistoryPage } from './pages/History';
import { ProfilesPage } from './pages/Profiles';
import { LogsPage } from './pages/Logs';
import { ApiKeysPage } from './pages/ApiKeys';
import { AboutPage } from './pages/About';
import { GuidePage } from './pages/Guide';
import { VisualizerPage } from './pages/Visualizer';
import { Crypto15mPage } from './pages/Crypto15m';
import { PerpsPage } from './pages/Perps';
import { CopyTradingPage } from './pages/CopyTrading';
import { AccountsPage } from './pages/Accounts';
import { BacktestPage } from './pages/Backtest';
import { TerminalPage } from './pages/Terminal';
import { ScriptsPage } from './pages/Scripts';
import { StrategiesPage } from './pages/Strategies';
import { DiscoverPage } from './pages/Discover';
import { MarketPage } from './pages/Market';
import { TrackPage } from './pages/Track';
import { RemotePage } from './pages/Remote';
import { AiAgentsPage } from './pages/AiAgents';
import { USDeskPage } from './pages/USDesk';
import { CommandPalette, useTerminalHotkeys } from './components/terminal/CommandPalette';

export type PageId =
  | 'dashboard' | 'main' | 'positions' | 'signals' | 'history'
  | 'profiles' | 'settings' | 'api' | 'logs' | 'guide' | 'about'
  | 'visualizer' | 'crypto15m' | 'perps' | 'copy' | 'accounts' | 'backtest'
  | 'terminal' | 'scripts' | 'strategies'
  | 'discover' | 'market' | 'track' | 'remote' | 'agents' | 'usdesk';

export const SIMPLE_PAGES: readonly PageId[] = [
  'agents', 'visualizer',
  'dashboard', 'strategies', 'positions', 'api', 'settings',
  'discover', 'market', 'track', 'remote', 'guide', 'usdesk',
];

export default function App() {
  return (
    <ToastProvider>
      <AppStateProvider>
        <Shell />
      </AppStateProvider>
    </ToastProvider>
  );
}

function Shell() {
  const [page, setPage, onPageCrash] = usePersistedPage();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { state } = useApp();
  const toast = useToast();
  useWindowVisibility();
  useEffect(() => {
    startAmbientClock();
    startGlassWatchdog();
    const onDemote = (): void => toast.info(
      'Glass effects switched to Frost to keep the app smooth on this machine. '
      + 'Change it in Settings → Appearance.');
    window.addEventListener('krypt:glass-demoted', onDemote);
    return () => window.removeEventListener('krypt:glass-demoted', onDemote);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { kickGlassWatchdog(); }, [page]);

  const openPalette = useCallback(() => setPaletteOpen(true), []);
  useTerminalHotkeys({ onPalette: openPalette, onNav: setPage });

  useEffect(() => {
    const offOrder = window.krypt.mcp?.onOrder?.((d) => {
      const message = agentOrderMessage(d);
      publishActivity(() => agentEvent(message ? { ...d, message } : d));
      const t = agentOrderToast(d);
      if (t) toast.push(t.text, t.tone, t.ms);
    });
    const offCalls = window.krypt.mcp?.onToolCall?.((d) => publishActivity(() => agentCallEvent(d)));
    return () => { offOrder?.(); offCalls?.(); };
  }, [toast]);

  const uiMode = state?.uiMode ?? 'advanced';
  const prevMode = useRef(uiMode);
  useEffect(() => {
    if (uiMode === 'simple' && prevMode.current === 'advanced'
        && !SIMPLE_PAGES.includes(page)) {
      setPage('agents');
    }
    prevMode.current = uiMode;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- transition-only
  }, [uiMode]);

  const auto = autoOnboarding(state);

  return (
    <GuideHost needsOnboarding={auto === 'first'} needsUpdateOnboarding={auto === 'update'} setPage={setPage}>
      <div className="flex h-full w-full flex-col">
        <GlassBackdrop />
        <TitleBar />
        <div className="relative flex h-[calc(100%-2.25rem)] w-full">
          <Sidebar page={page} setPage={setPage} />
          <main className="relative flex flex-1 flex-col overflow-hidden">
            <TopBar onNav={setPage} />
            <div className="flex-1 overflow-hidden">
              <PageGuard page={page} setPage={setPage} onCrash={onPageCrash}>
                <PageRouter page={page} setPage={setPage} />
              </PageGuard>
            </div>
          </main>
        </div>
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          onNav={setPage}
        />
      </div>
    </GuideHost>
  );
}

function PageRouter({ page, setPage }: { page: PageId; setPage: (p: PageId) => void }) {
  switch (page) {
    case 'dashboard': return <DashboardPage onNav={setPage} />;
    case 'main': return <MainEnginePage />;
    case 'positions': return <PositionsPage />;
    case 'signals': return <SignalsPage />;
    case 'history': return <HistoryPage />;
    case 'profiles': return <ProfilesPage />;
    case 'settings': return <SettingsPage onNav={setPage} />;
    case 'api': return <ApiKeysPage />;
    case 'logs': return <LogsPage />;
    case 'guide': return <GuidePage />;
    case 'about': return <AboutPage />;
    case 'visualizer': return <VisualizerPage />;
    case 'terminal': return <TerminalPage />;
    case 'crypto15m': return <Crypto15mPage />;
    case 'perps': return <PerpsPage />;
    case 'copy': return <CopyTradingPage />;
    case 'accounts': return <AccountsPage />;
    case 'backtest': return <BacktestPage />;
    case 'scripts': return <ScriptsPage />;
    case 'strategies': return <StrategiesPage onNav={setPage} />;
    case 'discover': return <DiscoverPage onNav={setPage} />;
    case 'market': return <MarketPage onNav={setPage} />;
    case 'track': return <TrackPage onNav={setPage} />;
    case 'usdesk': return <USDeskPage />;
    case 'remote': return <RemotePage />;
    case 'agents': return <AiAgentsPage onNav={setPage} />;
    default: return <AiAgentsPage onNav={setPage} />;
  }
}
