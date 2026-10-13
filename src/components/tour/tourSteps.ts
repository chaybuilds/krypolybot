import type { PageId } from '../../App';

export type TourSide = 'right' | 'left' | 'bottom' | 'top';

export interface TourStep {
  id: string;
  group: string;
  title: string;
  body: string;
  page?: PageId;
  prefer?: TourSide[];
}

export const navTourId = (page: PageId): string => `nav-${page}`;

const nav = (page: PageId, group: string, title: string, body: string): TourStep => ({
  id: navTourId(page), group, title, body, page,
});

export const TOUR_STEPS: TourStep[] = [
  nav('agents', 'Agents', 'AI Agents',
    'Connect the AI agent you already use (Claude Desktop, Claude Code, Cursor, Codex, Windsurf or VS Code), or the in-app Autopilot. '
    + 'Agents start on paper, and a scoreboard checks whether their forecasts beat the market price.'),
  nav('visualizer', 'Agents', 'Agent Hub',
    'A live view of what your agents and the bot are doing: tool calls, forecasts, signals and trades. '
    + 'It only shows activity; nothing on it places an order.'),

  nav('dashboard', 'Overview', 'Dashboard',
    'A snapshot of the account and the engines: balance, P&L, recent signals and bot health.'),

  nav('discover', 'Terminal', 'Discover',
    'Browse and search every Polymarket market by volume, closing time or newness. '
    + 'A good first stop if you want to look around before automating anything.'),
  nav('market', 'Terminal', 'Market',
    'One market in depth: chart, order book, tape and resolution rules, and a ticket to trade it by hand.'),
  nav('track', 'Terminal', 'Track',
    'Your own positions, resting orders and fills, whoever placed them: the paper book in Paper, your wallet in Live.'),

  nav('strategies', 'Automation', 'Strategies',
    'Built-in presets for the engines. None has a proven edge, so try anything on paper or in Backtest first.'),
  nav('main', 'Automation', 'Main Engine',
    'The whale and momentum engine: its gates, sizing and risk limits, with its own on/off switch.'),
  nav('crypto15m', 'Automation', 'Crypto',
    'Polymarket\'s short crypto up/down markets, with their own engine and their own switch, off by default.'),
  nav('perps', 'Automation', 'Perps',
    'A market maker for Polymarket Perps that quotes both sides for liquidity rewards. It has its own switch and loss limits.'),
  nav('copy', 'Automation', 'Copy Trading',
    'Follow wallets you pick and mirror their new entries at your own size. Off until you switch it on.'),
  nav('scripts', 'Automation', 'Scripts',
    'Write your own strategies in Python, or have an AI draft them, then backtest them and run them under money rails. '
    + 'Live scripts have their own switch, off by default.'),
  nav('positions', 'Automation', 'Bot positions',
    'The engines\' own ledger of open and recent positions, separate from what you trade by hand.'),
  nav('signals', 'Automation', 'Signals',
    'Whale and momentum signals as the scanners find them. '
    + 'A signal is a reason to look at a market, not a promise it will pay.'),
  nav('backtest', 'Automation', 'Backtest',
    'Replay a strategy against the ticks and signals this app has recorded, with real Polymarket fees.'),
  nav('terminal', 'Automation', 'Cockpit HUD',
    'A full-screen heads-up view of the engines while they run.'),

  nav('api', 'Account', 'Wallet',
    'Connect your Polymarket wallet, needed only to trade Live; Paper needs none. The key stays on this computer.'),
  nav('history', 'Account', 'History',
    'The bot\'s session diary and the full ledger of its settled trades.'),
  nav('profiles', 'Account', 'Profiles',
    'Save, export and import snapshots of each engine\'s settings. A profile never carries your agents, keys or Paper/Live mode.'),
  nav('accounts', 'Account', 'Accounts',
    'Run a second, fully separate copy of the app with its own wallet and settings.'),

  nav('remote', 'App', 'Remote',
    'Pair a Discord or Telegram bot to check on the app from your phone. '
    + 'Trading from the phone is a separate permission, off by default.'),
  nav('settings', 'App', 'Settings',
    'Paper or Live for the whole app, your AI provider, and the app\'s own preferences. Changes apply immediately.'),
  nav('logs', 'App', 'Logs',
    'A live tail of the backend\'s log. The first place to look when something is not doing what you expected.'),
  nav('guide', 'App', 'Guide',
    'How the app works and what each setting does, in more depth than this tour.'),
  nav('about', 'App', 'About',
    'Version, links, support and credits.'),

  {
    id: 'wallet', group: 'Status', title: 'Wallet',
    body: 'Your balance: cash plus positions. The pill says which book the app trades: PAPER is imaginary money on real order books, LIVE is your real wallet.',
  },
  {
    id: 'tour-info', group: 'Status', title: 'Replay this tour',
    body: 'Click this any time to take the tour again.',
  },
];
