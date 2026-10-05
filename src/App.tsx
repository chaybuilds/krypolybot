import { useCallback, useEffect, useRef, useState } from 'react';
import { TitleBar } from './components/TitleBar';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { AppStateProvider, useApp } from './state/AppStateProvider';
import { ToastProvider } from './state/ToastProvider';
import { OnboardingModal } from './pages/Onboarding';
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
import { CommandPalette, useTerminalHotkeys } from './components/terminal/CommandPalette';

export type PageId =
  | 'dashboard' | 'main' | 'positions' | 'signals' | 'history'
  | 'profiles' | 'settings' | 'api' | 'logs' | 'guide' | 'about'
  | 'visualizer' | 'crypto15m' | 'perps' | 'copy' | 'accounts' | 'backtest'
  | 'terminal' | 'scripts' | 'strategies'
  | 'discover' | 'market' | 'track' | 'remote' | 'agents';

export const SIMPLE_PAGES: readonly PageId[] = [
  'dashboard', 'strategies', 'positions', 'api', 'settings',
  'discover', 'market', 'track', 'remote', 'agents', 'guide',
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
  const [page, setPage] = useState<PageId>('dashboard');
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { state } = useApp();

  const openPalette = useCallback(() => setPaletteOpen(true), []);
  useTerminalHotkeys({ onPalette: openPalette, onNav: setPage });

  const showOnboarding = state ? !state.acceptedDisclaimer : false;

  useEffect(() => {
    if (!state) return;
    if (!state.acceptedDisclaimer) return;
  }, [state]);

  const uiMode = state?.uiMode ?? 'advanced';
  const prevMode = useRef(uiMode);
  useEffect(() => {
    if (uiMode === 'simple' && prevMode.current === 'advanced'
        && !SIMPLE_PAGES.includes(page)) {
      setPage('dashboard');
    }
    prevMode.current = uiMode;
  }, [uiMode]);

  return (
    <div className="flex h-full w-full flex-col bg-krypt-radial bg-krypt-void">
      <TitleBar />
      <div className="flex h-[calc(100%-2.25rem)] w-full">
        <Sidebar page={page} setPage={setPage} />
        <main className="relative flex flex-1 flex-col overflow-hidden">
          <TopBar />
          <div className="flex-1 overflow-hidden bg-krypt-radial-r">
            <PageRouter page={page} setPage={setPage} />
          </div>
        </main>
      </div>
      {showOnboarding && <OnboardingModal onDone={() => setPage('api')} />}
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNav={setPage}
      />
    </div>
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
    case 'settings': return <SettingsPage />;
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
    case 'remote': return <RemotePage />;
    case 'agents': return <AiAgentsPage />;
    default: return <DashboardPage onNav={setPage} />;
  }
}
