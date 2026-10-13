import type {
  AccountMode, AgentPaper, AiAnalysis, AiProvider, AiProviderCheck, AiStatus,
  AutopilotStatus, ForecastScoreboard, HealthReport,
  McpActivity, McpAgentStatus, McpLossToday, McpOrderEvent, McpOrderRow, McpRails, McpSeen,
  McpToolCallEvent, McpTradeMode, PaperStatus,
  BookSnapshot, DiscoverColumn, DiscoverFilters, DiscoverResult,
  FlattenPreview, FlattenResult,
  ManualOrderPreview, ManualOrderRequest, ManualOrderResult, MarketSummary,
  CrossVenueComparison,
  PaperOrder, PaperOrderResult, PaperPortfolio, RelatedMarkets,
  PriceSeries, ResolutionReport, RestingOrder,
  SellPositionRequest, SellPreview, SellResult,
  SettledRecord, StandingInstruction,
  TerminalFill, TerminalHealth, TerminalInterval, TerminalPortfolio,
  WatchEntry,
} from './market';
import type { McpAgent } from './agents';

export type Network = 'mainnet';

export interface MicroSeries {
  conditionId: string;
  side: 'yes' | 'no';
  points: { t: number; bid: number | null; ask: number | null; spread: number | null }[];
  medianSpreadCents: number | null;
  sampleCount: number;
  source: 'clob-ws';
  note: string | null;
}

export interface TapeResult {
  conditionId: string;
  trades: import('./market').TapeTrade[];
  fetchedAt: string;
  note: string | null;
}

export interface TerminalOrderLogRow {
  localId: number;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  orderType: string;
  requestedSize: number;
  requestedPriceCents: number;
  sentSize: number | null;
  sentPriceCents: number | null;
  orderId: string | null;
  status: string;
  error: string | null;
  filledSize: number | null;
  avgFillPriceCents: number | null;
  feesUsd: number | null;
  reconciled: boolean;
  origin: string;
  standingId: string | null;
  createdAt: string;
}

export interface RemotePairedAccount {
  id: string;
  label: string | null;
  pairedAt: string;
}

export interface RemoteAuditRow {
  at: string;
  platform: string;
  sender: string;
  command: string;
  outcome: string;
  detail: string;
  count?: number;
  firstAt?: string;
}

export interface RemotePlatformStatus {
  connected: boolean;
  lastError: string | null;
  paired: RemotePairedAccount[];
  hasToken: boolean;
  tokenMasked?: string | null;
  botUsername?: string | null;
}

export interface RemoteStatus {
  discord: RemotePlatformStatus;
  telegram: RemotePlatformStatus;
  audit: RemoteAuditRow[];
  fetchedAt: string;
}

export interface McpSession {
  id: string;
  client: string;
  version: string | null;
  protocol: string | null;
  connectedAt: string;
  lastSeen: string;
  reads: number;
  writes: number;
  refused: number;
}

export interface McpAuditRow {
  at: string;
  client: string;
  tool: string;
  outcome: 'accepted' | 'refused' | 'failed' | 'error' | string;
  detail: string;
}

export interface McpStatus {
  enabled: boolean;
  running: boolean;
  port: number;
  url: string;
  lastError: string | null;
  hasToken: boolean;
  tokenMasked: string | null;
  tradeMode?: McpTradeMode;
  rails?: McpRails;
  approvals?: boolean;
  agents?: McpAgentStatus[];
  pendingApprovals?: McpOrderRow[];
  sessions: McpSession[];
  toolCount: number;
  spentTodayUsd: number | null;
  lossTodayUsd?: number | null;
  lossToday?: McpLossToday | null;
  httpEnabled?: boolean;
  httpUrl?: string;
  accountMode?: AccountMode;
  audit: McpAuditRow[];
  fetchedAt: string;
}

export type McpClientId =
  | 'claude-desktop' | 'claude-code' | 'cursor' | 'codex' | 'windsurf' | 'vscode';

export interface McpClientSetup {
  id: McpClientId;
  label: string;
  agentId: string;
  serverName: string;
  configPath: string | null;
  configPaths?: string[];
  detected: boolean;
  installed: boolean;
  canInstall: boolean;
  snippet: string;
  snippetKind: 'json' | 'toml' | 'shell';
  note?: string;
}

export interface McpInstallResult {
  ok: boolean;
  message: string;
  files?: string[];
  backups?: string[];
}

export interface StandingEvent {
  id: string;
  conditionId: string;
  title: string | null;
  kind: string;
  priceCents: number;
  triggerCents: number;
  blocked?: boolean;
  result?: string;
  ok?: boolean;
}

export type OrderStyle = 'limit_cross' | 'limit_mid' | 'market';

export type SignalSource = 'whale' | 'momentum' | 'convergence' | 'external' | 'copy';

export interface RuleCondition {
  field: string;
  op: '>=' | '<=' | '>' | '<';
  value: number;
}

export interface TraderConfig {
  network: Network;
  enableTrading: boolean;

  tradeWhales: boolean;
  tradeMomentum: boolean;
  tradeConvergence: boolean;

  minEdgePtsWhale: number;
  minEdgePtsMomentum: number;
  minConfidenceWhale: number;
  minConfidenceMomentum: number;
  minEntryPriceCents: number;
  maxEntryPriceCents: number;
  maxResolutionDays?: number;
  allowedMomentumSignalTypes: string[];
  allowedCategories: string[] | null;
  allowedWhaleCategories: string[] | null;
  allowedMomentumCategories: string[] | null;
  contrarianOnly: boolean;

  useRules?: boolean;
  rules?: RuleCondition[];

  sizingMode?: 'percent' | 'contracts';
  baseSizeFraction: number;
  minSizeFraction: number;
  maxSizeFraction: number;
  minContracts?: number;
  maxContracts?: number;
  sizingBaseEdge: number;
  sizingMaxEdge: number;
  hardMaxPositionUsd: number;
  minCashReserveFraction: number;

  orderStyle: OrderStyle;
  crossSpreadFallbackOffset: number;
  orderExpirationSec: number | null;

  maxOpenPositions: number;
  maxPositionsPerEvent: number;
  maxDailyNewPositions: number;
  unlimitedDailyNewPositions: boolean;
  maxTotalExposureFraction: number;

  tradeScanInterval: number;
  positionPollInterval: number;
  balancePollInterval: number;
  resolutionCheckInterval: number;
  whaleScanInterval: number;
  momentumScanInterval: number;
  marketRefreshInterval: number;

  maxSignalAgeSec: number;

  startBankrollUsd: number;
  stopLossOnDay: number;
  takeProfitOnDay: number;
  takeProfitPct?: number;
  flattenOnDailyStop?: boolean;
  lifetimeLossLimitPct?: number;
  lifetimeLossLimitUsd?: number;

  tradingHoursEnabled: boolean;
  tradingHoursStart: string;
  tradingHoursEnd: string;
  tradingDays: string[];
  tradingTimezoneOffsetMin: number;

  minWhaleUsd: number;
  minWhaleConfidence: number;
  minWhaleEdge: number;
  minMomentumConfidence: number;
  minMomentumEdge: number;
  minEntryPriceFrac: number;

  statsChartWindowHours: number;

  crypto15mEnabled?: boolean;
  crypto15mInterval?: '5m' | '15m' | 'hourly' | '4h';
  crypto15mAssets?: string[] | null;
  crypto15mArbDetect?: boolean;
  crypto15mArbMinEdgeCents?: number;
  crypto15mImbalanceDetect?: boolean;
  crypto15mImbalanceLevels?: number;
  crypto15mImbalanceGate?: boolean;
  crypto15mImbalanceGateMin?: number;
  crypto15mIndicatorDetect?: boolean;
  crypto15mWsBook?: boolean;
  crypto15mUseRules?: boolean;
  crypto15mRules?: RuleCondition[];
  crypto15mSizingMode?: 'fixed' | 'balance_pct';
  crypto15mOrderSize?: number;
  crypto15mBalancePct?: number;
  crypto15mMaxLossPct?: number;
  crypto15mStreakSizing?: boolean;
  crypto15mStreakLossPct?: number;
  crypto15mStreakWinPct?: number;
  crypto15mStreakMaxMult?: number;
  crypto15mMaxConcurrent?: number;
  crypto15mDailyLossLimit?: number;
  crypto15mLifetimeLossLimitPct?: number;
  crypto15mLifetimeLossLimitUsd?: number;
  crypto15mTakeProfitTotal?: number;
  crypto15mDirectionMode?: 'favorite' | 'contrarian' | 'model';
  crypto15mModelMinProb?: number;
  crypto15mModelMinEdgeCents?: number;
  crypto15mModelFinalMinute?: boolean;
  crypto15mModelAutopause?: boolean;
  crypto15mModelMaxBookGapCents?: number;
  crypto15mSpotWs?: boolean;
  crypto15mRtdsWs?: boolean;
  crypto15mPairedMode?: boolean;
  crypto15mPairedMaxCombinedCents?: number;
  crypto15mPairedTilt1Cents?: number;
  crypto15mPairedTilt2Cents?: number;
  crypto15mPairedTilt3Cents?: number;
  crypto15mSellIntoStrength?: boolean;
  crypto15mSellStrengthCents?: number;
  crypto15mTimeDelayMin?: number;
  crypto15mEntryThreshold?: number;
  crypto15mEntryMax?: number;
  crypto15mExitThreshold?: number;
  crypto15mTakeProfit?: number;
  crypto15mStopLossPct?: number;
  crypto15mTakeProfitPct?: number;
  crypto15mMinRsi?: number;
  crypto15mMinMacdHist?: number;
  crypto15mMinDeltaPct?: number;
  crypto15mEntryDiff?: number;
  crypto15mEntryStyle?: 'maker' | 'taker';
  crypto15mTakerFak?: boolean;
  crypto15mMakerCancelMin?: number;
  crypto15mMakerEscalate?: boolean;
  crypto15mMakerFillSec?: number;
  crypto15mHoursStartUtc?: number;
  crypto15mHoursEndUtc?: number;
  crypto15mRecordSignals?: boolean;
  mainRecordSignals?: boolean;
  takerRebatePct?: number;

  perpsEnabled?: boolean;
  perpsSelectMode?: 'rewards' | 'volume' | 'manual';
  perpsMarkets?: string[];
  perpsMarketCount?: number;
  perpsCategories?: PerpsCategory[];
  perpsQuoteBps?: number;
  perpsLevels?: number;
  perpsLevelStepBps?: number;
  perpsSizeUsd?: number;
  perpsMaxInventoryUsd?: number;
  perpsSkewBps?: number;
  perpsRequoteBps?: number;
  perpsVolPullBps?: number;
  perpsVolWindowSec?: number;
  perpsPullCooldownSec?: number;
  perpsMaxIndexDevBps?: number;
  perpsLeverage?: number;
  perpsCrossMargin?: boolean;
  perpsDailyLossLimit?: number;
  perpsLifetimeLossLimitUsd?: number;
  perpsMarkoutKillBps?: number;
  perpsMarkoutMinFills?: number;
  perpsDailyVolumeTargetUsd?: number;
  perpsPollSec?: number;
  perpsRealtime?: boolean;
  perpsBookRefreshSec?: number;

  copyEnabled?: boolean;
  copyWallets?: string[];
  copySizingMode?: 'fixed' | 'balance_pct';
  copyFixedUsd?: number;
  copyBalancePct?: number;
  copyMinTradeUsd?: number;
  copyMaxConcurrent?: number;
  copyEntryMaxCents?: number;
  copyEntryMinCents?: number;
  copyDailyLossLimit?: number;
  copyLifetimeLossLimitPct?: number;
  copyLifetimeLossLimitUsd?: number;
  copyPollSec?: number;
  copyFastPollSec?: number;
  copyActivityWs?: boolean;
  copyMirrorReductions?: boolean;
  copyReduceThreshold?: number;
  copyOnlyNewEntries?: boolean;
  copyAllowReentries?: boolean;

  scriptsLiveEnabled?: boolean;
  scriptPollSec?: number;
  scriptMaxEntryCents?: number;
  scriptMaxContracts?: number;
  scriptMaxOpen?: number;
  scriptDailyLossUsd?: number;
  scriptMaxEnabled?: number;
  terminalOfflineMode?: boolean;
  terminalNotifications?: boolean;
  aiEnabled?: boolean;
  aiProvider?: AiProvider;
  aiModel?: string;
  aiWebSearch?: boolean;
  remoteEnabled?: boolean;
  remoteDiscordEnabled?: boolean;
  remoteTelegramEnabled?: boolean;
  remoteTradingEnabled?: boolean;
  remoteMaxOrderUsd?: number;
  terminalPaperMode?: boolean;
  terminalPaperBankrollUsd?: number;
  accountMode?: AccountMode;
  mcpEnabled?: boolean;
  mcpPort?: number;
  mcpTradeMode?: McpTradeMode;
  mcpMaxOrderUsd?: number;
  mcpDailySpendUsd?: number;
  mcpMaxPositions?: number;
  mcpMinEdgeCents?: number;
  mcpDailyLossUsd?: number;
  mcpLiveApproval?: boolean;
  mcpAgents?: McpAgent[];
  mcpAllowLiveSwitches?: boolean;
  mcpScriptsEnabled?: boolean;
  mcpPaperBankrollUsd?: number;
  mcpHttpEnabled?: boolean;
  autopilotEnabled?: boolean;
  autopilotIntervalMin?: number;
  autopilotMaxRunsPerDay?: number;
  autopilotDailyTokenBudget?: number;
  autopilotMaxSteps?: number;
  autopilotMission?: string;
  autopilotAgentId?: string;
  whaleActivityWs?: boolean;
  dbCleanupInterval?: number;
  crypto15mRulesNo?: RuleCondition[];
  crypto15mPollSec?: number;
  crypto15mModelFmMaxBookGapCents?: number;
  crypto15mAutosizeToMinNotional?: boolean;
  scriptHookTimeoutSec?: number;
  scriptMarketLimit?: number;
  scriptMarketMinVolume?: number;
  scriptMarketMaxSpreadCents?: number;
}

export interface CopyWalletInfo {
  address: string;
  short: string;
  positions: number;
  valueUsd: number;
  error?: string | null;
}

export interface CopyStatus {
  enabled: boolean;
  authed: boolean;
  trading: boolean;
  wallets: CopyWalletInfo[];
  openCopies: number;
  todayPnlUsd: number;
  lossLimitHit: boolean;
  sizing: { mode: 'fixed' | 'balance_pct'; fixedUsd: number; balancePct: number };
  maxConcurrent: number;
  entryMaxCents: number;
  entryMinCents: number;
}

export interface CredentialsState {
  env?: Network;
  hasWalletKey: boolean;
  hasApiCreds: boolean;
  address: string;
  addressPreview: string;
  funder?: string;
  signatureType?: number;
  walletMode?: 'eoa' | 'deposit' | 'error';
  metaError?: string;
  keyStoredUnencrypted?: boolean;
}

export interface CredentialsStatusAll {
  current: Network;
  mainnet: CredentialsState;
}

export interface CredentialsInput {
  privateKey?: string;
  apiCreds?: { apiKey: string; secret: string; passphrase: string };
  env?: Network;
  funder?: string;
  signatureType?: number;
}

export type ProfileScope = 'main' | 'crypto' | 'copy' | 'perps';

export interface Profile {
  id: string;
  name: string;
  description?: string;
  scope?: ProfileScope;
  createdAt: string;
  updatedAt: string;
  config: TraderConfig;
  builtin?: boolean;
}

export interface AppState {
  config: TraderConfig;
  activeProfileId: string | null;
  activeCryptoProfileId?: string | null;
  activeCopyProfileId?: string | null;
  activePerpsProfileId?: string | null;
  customProfiles: Profile[];
  startMinimized: boolean;
  startWithWindows: boolean;
  enableDiscordRpc: boolean;
  acceptedDisclaimer: boolean;
  uiMode: 'simple' | 'advanced';
  windowBounds: { x: number; y: number; width: number; height: number } | null;
  onboardingSeen?: number;
}

export type BackendStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'restarting'
  | 'crashed';

export interface BackendInfo {
  status: BackendStatus;
  pid: number | null;
  startedAt: string | null;
  lastError: string | null;
  pythonOk: boolean;
  authOk: boolean;
}

export type RegionStatusKind = 'allowed' | 'close_only' | 'blocked' | 'unknown';

export interface RegionStatus {
  status: RegionStatusKind;
  country: string;
  region: string;
  checkedAt?: number;
  ageSec?: number;
  message: string;
  refusedOrder?: boolean;
}

export interface AccountSnapshot {
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  backendDown?: boolean;
  balanceSyncing?: boolean;
  tradingGeoblocked?: boolean;
  tradingRegion?: RegionStatus | null;
  tradingRestriction?: { kind: 'restarting' | 'post_only'; untilEpoch: number; message: string } | null;
  userFeed?: { running: boolean; connected: boolean; events: number; lastEventAgoSec: number | null; lastError: string };
  startBankrollUsd: number;
  bankrollSource?: 'user' | 'auto' | 'live';
  roiPct: number;
  realizedPnlUsd: number;
  todayPnlUsd?: number;
  alltimePnlUsd?: number;
  todayBaselineUsd?: number | null;
  alltimeBaselineUsd?: number | null;
  todayWins?: number;
  todayLosses?: number;
  unrealizedPnlUsd: number;
  openCostUsd: number;
  unredeemedWinningsUsd?: number;
  unredeemedWinningsCount?: number;
  unredeemedWinningsStale?: boolean;
  feesUsd: number;
  wins: number;
  losses: number;
  winRate: number;
  pendingCount: number;
  openCount: number;
  resolvedCount: number;
  totalOpened: number;
  byNetwork: { mainnet: AccountByEnv };
  sessionPnlUsd?: number;
  sessionRoiPct?: number;
  sessionBaselineUsd?: number;
  sessionStartedAt?: string;
  sessionRunId?: number;
}

export interface BotRun {
  id: number;
  network: Network;
  startedAt: string;
  endedAt: string | null;
  startCashUsd: number;
  startPortfolioUsd: number;
  startTotalUsd: number;
  endCashUsd: number | null;
  endPortfolioUsd: number | null;
  endTotalUsd: number | null;
  pnlUsd: number;
  tradesOpened: number;
  tradesWon: number;
  tradesLost: number;
  isActive: boolean;
}

export interface BotRunsResponse {
  runs: BotRun[];
  activeRunId: number;
  activeRun: BotRun | null;
}

export interface AccountByEnv {
  wins: number;
  losses: number;
  realizedPnl: number;
}

export interface PnlPoint {
  at: string;
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  realizedPnlUsd: number;
  openPositions: number;
}

export interface BotPosition {
  id: number;
  signalSource: SignalSource;
  signalId: number;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  action: 'buy' | 'sell';
  targetContracts: number;
  limitPriceCents: number;
  filledContracts: number;
  avgFillPriceCents: number | null;
  costUsd: number;
  feesUsd: number;
  clientOrderId: string;
  orderId: string | null;
  status:
    | 'submitted'
    | 'partial'
    | 'filled'
    | 'canceled'
    | 'expired'
    | 'gone'
    | 'error'
    | 'dry_run';
  confidence: number;
  edgePts: number;
  signalPriceCents: number;
  resolved: boolean;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  markPriceCents: number | null;
  livePnlUsd: number | null;
  balanceBeforeUsd: number | null;
  network: Network;
  createdAt: string;
  lastUpdated: string;
  resolvedAt: string | null;
  error: string | null;
}

export interface SignalRow {
  id: number;
  source: SignalSource;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  priceCents: number;
  confidence: number;
  edgePts: number;
  signalType?: string;
  dollarValue?: number;
  createdAt: string;
  resolved: boolean;
  outcomeCorrect: number | null;
  pnlEstimate: number | null;
  traded: boolean;
}

export interface ScannerStats {
  whales: { total: number; sent: number; resolved: number; winRate: number };
  momentum: { total: number; sent: number; resolved: number; winRate: number };
  marketsTracked: number;
  lastWhaleScanAt: string | null;
  lastMomentumScanAt: string | null;
  lastTradeScanAt: string | null;
}

export interface LogEntry {
  ts: string;
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'CRITICAL';
  source: 'main' | 'backend' | 'trader' | 'whale' | 'momentum' | 'discord';
  msg: string;
}

export interface ActionResult<T = void> {
  ok: boolean;
  message?: string;
  data?: T;
}

export interface StrategyPreset {
  id: string;
  name: string;
  tagline: string;
  description: string;
  riskLabel: 'safe' | 'balanced' | 'aggressive' | 'experimental';
  measured?: string;
  badge?: 'recommended' | 'new' | null;
  config: TraderConfig;
}

export interface Crypto15mConstants {
  timeDelayMin: number;
  entryThreshold: number;
  exitThreshold: number;
  entryMax: number;
  minDeltaPct?: number;
  entryDiff: number;
  directionMode?: 'favorite' | 'contrarian';
  entryStyle?: 'maker' | 'taker';
  hoursStartUtc?: number;
  hoursEndUtc?: number;
}

export interface Crypto15mAsset {
  asset: string;
  series: string;
  enabled?: boolean;
  upAsk?: number | null;
  downAsk?: number | null;
  arbEdgeCents?: number | null;
  arbSignal?: boolean;
  bookImbalance?: number | null;
  spotUsd: number | null;
  open15mUsd: number | null;
  deltaUsd: number | null;
  deltaPct?: number | null;
  hasMarket: boolean;
  ticker: string | null;
  closeTime: string | null;
  minsLeft: number | null;
  upProb: number | null;
  downProb: number | null;
  favorite: 'up' | 'down' | null;
  favoritePrice: number | null;
  entryCost: number | null;
  yesBid?: number | null;
  yesAsk?: number | null;
  inWindow: boolean;
  signal: boolean;
  openMarketCount: number;
  error: string | null;
  hourUtc?: number | null;
  peersAgree?: number | null;
  marketBias?: number | null;
  macd?: number | null;
  macdSignal?: number | null;
  macdHist?: number | null;
  macdCross?: number | null;
  rsi?: number | null;
  wsBid?: number | null;
  wsAsk?: number | null;
  priceSource?: 'ws' | 'gamma';
  strikeUsd?: number | null;
  deltaSignedPct?: number | null;
  sigma1m?: number | null;
  modelProb?: number | null;
  edgeNetCents?: number | null;
  spotLive?: boolean;
}

export interface Crypto15mSnapshot {
  fetchedAt: string;
  spotOk: boolean;
  spotSource: string;
  hoursOk?: boolean;
  constants: Crypto15mConstants;
  assets: Crypto15mAsset[];
}

export type Crypto15mStatusName =
  | 'dry_run' | 'submitted' | 'filled' | 'exiting'
  | 'exited' | 'settled' | 'canceled' | 'error';

export interface Crypto15mPosition {
  id: number;
  asset: string;
  series: string;
  ticker: string;
  side: 'up' | 'down' | '';
  direction: 'yes' | 'no' | '';
  targetContracts: number;
  filledContracts: number;
  entryLimitCents: number;
  avgEntryCents: number | null;
  costUsd: number;
  status: Crypto15mStatusName | string;
  exitReason: string | null;
  exitLimitCents: number | null;
  proceedsUsd: number | null;
  confidence: number;
  entryDeltaUsd: number | null;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  resolved: boolean;
  settling: boolean;
  closeTime: string;
  network: Network;
  createdAt: string;
  resolvedAt: string | null;
  error: string | null;
  strategy?: string;
  feesUsd?: number;
}

export interface Crypto15mStats {
  openCount: number;
  wins: number;
  losses: number;
  realizedPnlUsd: number;
  total: number;
}

export interface Crypto15mSizing {
  mode: 'fixed' | 'balance_pct';
  balancePct: number;
  maxLossPct: number;
  balanceUsd: number;
  estPriceCents: number;
  estContracts: number;
  estCostUsd: number;
  streakMult?: number;
  note: string;
}

export interface Crypto15mStatus {
  enabled: boolean;
  authed: boolean;
  trading: boolean;
  haltReason?: string;
  blockReasons?: Record<string, string>;
  byStrategy?: { strategy: string; n: number; wins: number; losses: number; pnl_usd: number; fees_usd: number }[];
  modelCalibration?: { ok: boolean; n: number; rate: number | null; lb: number | null };
  orderSize: number;
  maxConcurrent: number;
  sizing: Crypto15mSizing;
  env: Network;
  stats: Crypto15mStats;
  open: Crypto15mPosition[];
  recent: Crypto15mPosition[];
}

export interface TradingGate {
  id: string;
  label: string;
  state: 'ok' | 'blocked' | 'off';
  reason: string;
}

export interface TradingStatus {
  main: TradingGate[];
  mainFilterCounts: Record<string, number>;
  mainCandidates: number;
  mainPlaced: number;
  c15: {
    enabled: boolean;
    live: boolean;
    authed: boolean;
    env: string;
    blockReasons: Record<string, string>;
  };
}

export interface Crypto15mBacktest {
  n: number;
  wins: number;
  winRate: number;
  netEvCentsPerContract: number;
  totalPnlUsd: number;
  maxDrawdownUsd: number;
  contracts: number;
  windowsScanned: number;
  byAsset: Record<string, { n: number; wins: number; pnlUsd: number }>;
  equity: { at: string | null; value: number }[];
  byHourUtc: { hour: number; n: number; wins: number; pnlUsd: number }[];
  byDay: { day: string; n: number; wins: number; pnlUsd: number }[];
  trades: { ticker: string; asset: string; side: string; costCents: number; minsLeft: number | null; won: boolean; pnlUsd: number; at: string }[];
  caveats: string[];
  tStat?: number | null;
  tStatNaive?: number | null;
  nClusters?: number;
  interval?: '5m' | '15m' | 'hourly' | '4h';
  noData?: ReplayNoData;
}

export interface ReplayNoData {
  reason: 'interval' | 'assets' | 'collection-off' | 'none-yet';
  interval: string;
  recordingInterval: string | null;
  recordingOn: boolean | null;
  recorded: Record<string, number>;
  message: string;
}

export interface UserScriptStats {
  n: number;
  open: number;
  wins: number;
  losses: number;
  pnlUsd: number;
}

export interface ScriptAuditFinding {
  severity: 'critical' | 'warning' | 'info';
  category: string;
  line: number;
  message: string;
}

export interface ScriptAudit {
  ok: boolean;
  parsed: boolean;
  critical: number;
  warning: number;
  info: number;
  categories: string[];
  findings: ScriptAuditFinding[];
  summary: string;
}

export interface UserScriptStatus {
  state: 'off' | 'blocked' | 'error' | 'starting' | 'running';
  detail: string;
  ticks: number;
  intents: number;
  orders: number;
  lastTickAt: string | null;
  lastIntentAt: string | null;
  lastOrderAt: string | null;
}

export interface UserScript {
  id: string;
  name: string;
  description: string;
  code: string;
  enabled: boolean;
  dryRun: boolean;
  assets: string[] | null;
  audit: ScriptAudit;
  status: UserScriptStatus;
  notes: string;
  lastError: string | null;
  lastErrorAt: string | null;
  createdAt: string;
  updatedAt: string;
  stats: UserScriptStats | null;
  shadowStats: UserScriptStats | null;
}

export interface ScriptShadowOrder {
  id: number;
  source: 'crypto' | 'signal';
  asset: string;
  ticker: string;
  side: string;
  contracts: number;
  entryCents: number;
  orderType: string;
  reason: string;
  refused: boolean;
  note: string;
  resolved: boolean;
  won: boolean | null;
  pnlUsd: number | null;
  at: string;
}

export interface ScriptValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  audit: ScriptAudit;
  name: string;
  description: string;
  hasHeader: boolean;
  ctxFields: string[];
}

export interface ScriptApiDocs {
  contract: string;
  fields: { name: string; doc: string; backtestable: boolean }[];
  injected: string[];
  hookTimeoutSec: number;
  rails: {
    maxEntryCents: number; maxContracts: number; maxOpen: number;
    dailyLossUsd: number; defaultOrderSize: number;
  };
  examples: { name: string; code: string }[];
  presets: {
    id: string; name: string; tagline: string;
    expected: string; risks: string; bankrollMin: number; code: string;
  }[];
}

export interface ScriptBacktest extends Crypto15mBacktest {
  tStat?: number | null;
  scriptError?: string | null;
  scriptLogs?: string[];
  signalResult?: (Crypto15mBacktest & { tStat?: number | null; scriptError?: string | null }) | null;
}

export interface CollectionStats {
  c15: {
    windows: number; resolved: number; ticks: number;
    firstAt: string | null; lastAt: string | null;
    recent: { ticker: string; asset: string; favorite: string | null; favorite_price: number | null; up_won: number | null; resolved: number; close_time: string }[];
    byInterval?: Record<string, { windows: number; resolved: number }>;
    recordingInterval?: string;
  };
  main: {
    whales: number; whalesResolved: number; alerts: number; alertsResolved: number;
    firstAt: string | null; lastAt: string | null;
    topCategories: { category: string; n: number }[];
    recent: { ticker: string; category: string; taker_side: string; price: number; dollar_value: number; outcome_correct: number | null; resolved: number; created_at: string }[];
  };
  collecting: { c15: boolean; main: boolean };
}

export interface AccountInfo {
  name: string;
  current: boolean;
  isDefault: boolean;
}

export interface KryptApi {
  app: {
    version: () => Promise<string>;
    openExternal: (url: string) => Promise<void>;
    showItemInFolder: (filePath: string) => Promise<void>;
    saveVideo: (bytes: Uint8Array, ext: 'webm' | 'mp4', name?: string) => Promise<{ ok: boolean; data?: string; message?: string }>;
    getUserDataPath: () => Promise<string>;
    factoryReset: () => Promise<ActionResult<{ deleted: Record<string, number> }>>;
    clearHistory: () => Promise<ActionResult<{ deleted: Record<string, number> }>>;
    onDataReset: (cb: (payload: unknown) => void) => () => void;
    markOnboardingSeen: () => Promise<ActionResult>;
    isVisible: () => Promise<boolean>;
    onVisibility: (cb: (visible: boolean) => void) => () => void;
  };
  accounts: {
    current: () => Promise<string>;
    list: () => Promise<AccountInfo[]>;
    create: (name: string) => Promise<{ ok: boolean; name?: string; message?: string }>;
    launch: (name: string) => Promise<{ ok: boolean; message?: string }>;
  };
  state: {
    get: () => Promise<AppState>;
    onChange: (cb: (state: AppState) => void) => () => void;
    setStartMinimized: (v: boolean) => Promise<ActionResult>;
    setStartWithWindows: (v: boolean) => Promise<ActionResult>;
    setEnableDiscordRpc: (v: boolean) => Promise<ActionResult>;
    acceptDisclaimer: () => Promise<ActionResult>;
    setUiMode: (v: 'simple' | 'advanced') => Promise<ActionResult>;
    resetOnboarding: () => Promise<ActionResult>;
    markOnboardingSeen: () => Promise<ActionResult>;
  };
  config: {
    get: () => Promise<TraderConfig>;
    update: (patch: Partial<TraderConfig>) => Promise<TraderConfig>;
    replace: (config: TraderConfig) => Promise<TraderConfig>;
    reset: () => Promise<TraderConfig>;
    listStrategies: () => Promise<StrategyPreset[]>;
    applyStrategy: (id: string) => Promise<TraderConfig>;
  };
  profiles: {
    list: () => Promise<Profile[]>;
    save: (name: string, description?: string, scope?: ProfileScope) => Promise<ActionResult<Profile>>;
    apply: (id: string) => Promise<ActionResult<TraderConfig>>;
    rename: (id: string, name: string) => Promise<ActionResult>;
    delete: (id: string) => Promise<ActionResult>;
    duplicate: (id: string) => Promise<ActionResult<Profile>>;
    export: (id: string) => Promise<ActionResult<string>>;
    import: (json: string) => Promise<ActionResult<Profile>>;
  };
  credentials: {
    status: () => Promise<CredentialsState>;
    statusAll: () => Promise<CredentialsStatusAll>;
    save: (input: CredentialsInput) => Promise<ActionResult>;
    test: (env?: Network) => Promise<ActionResult<{
      env: Network;
      balanceUsd: number;
      ready?: boolean;
      approvalsOk?: boolean | null;
      issues?: string[];
      fatalIssues?: string[];
      region?: RegionStatus | null;
      address?: string;
    }>>;
    clear: (env?: Network) => Promise<ActionResult>;
    onChanged: (cb: (payload: unknown) => void) => () => void;
  };
  backend: {
    info: () => Promise<BackendInfo>;
    start: () => Promise<ActionResult>;
    stop: () => Promise<ActionResult>;
    restart: () => Promise<ActionResult>;
    onInfo: (cb: (info: BackendInfo) => void) => () => void;
    runOnce: (
      action:
        | 'syncMarkets'
        | 'pollOrders'
        | 'resolveAll'
        | 'reconcilePositions'
        | 'syncPositions'
        | 'recomputePnl'
        | 'reconcileFills'
        | 'auditPnl'
    ) => Promise<ActionResult<{ summary: string }>>;
  };
  trading: {
    setEnabled: (enabled: boolean) => Promise<ActionResult>;
    cancelAllOpen: () => Promise<ActionResult<{ canceled: number }>>;
    flatten: () => Promise<ActionResult<{ closed: number }>>;
    status: () => Promise<TradingStatus>;
    collection: () => Promise<CollectionStats | null>;
    exportData: () => Promise<{ dir: string; files: string[] } | null>;
  };
  data: {
    account: () => Promise<AccountSnapshot>;
    pnlSeries: (sinceHours?: number) => Promise<PnlPoint[]>;
    positions: (filter?: PositionFilter) => Promise<BotPosition[]>;
    signals: (filter?: SignalFilter) => Promise<SignalRow[]>;
    scannerStats: () => Promise<ScannerStats>;
    botRuns: (env?: Network | null, limit?: number) => Promise<BotRunsResponse>;
    onAccount: (cb: (snap: AccountSnapshot) => void) => () => void;
    onPosition: (cb: (pos: BotPosition) => void) => () => void;
    onSignal: (cb: (sig: SignalRow) => void) => () => void;
    onSignalBatch: (cb: (d: { signals: SignalRow[] }) => void) => () => void;
  };
  polydata: {
    userPnl: (interval?: '1d' | '1w' | '1m' | 'max') => Promise<OfficialPnl>;
    resolutions: (conditionIds: string[]) => Promise<{ ok: boolean; message?: string; resolutions: Record<string, ResolutionState> }>;
    holders: (conditionId: string, limit?: number) => Promise<{ ok: boolean; message?: string; holders: MarketHolder[] }>;
    biggestWinners: (period?: 'day' | 'week' | 'month' | 'all', category?: string) =>
      Promise<{ ok: boolean; message?: string; winners: BiggestWinner[] }>;
  };
  perps: {
    board: () => Promise<PerpsBoard>;
    status: () => Promise<PerpsStatus | null>;
    link: () => Promise<{ ok: boolean; message: string }>;
    unlink: () => Promise<{ ok: boolean; message: string }>;
    flatten: () => Promise<{ ok: boolean; message: string }>;
    cancelAll: () => Promise<{ ok: boolean; message: string }>;
    backtest: (args: PerpsBacktestArgs) => Promise<PerpsBacktestResult>;
  };
  crypto15m: {
    snapshot: () => Promise<Crypto15mSnapshot>;
    status: () => Promise<Crypto15mStatus>;
    history: (opts?: { limit?: number }) => Promise<{ rows: Crypto15mPosition[] }>;
    backtest: (args?: { sinceDays?: number; config?: Record<string, unknown> }) => Promise<Crypto15mBacktest | null>;
    backtestMain: (args?: { sinceDays?: number; config?: Record<string, unknown> }) => Promise<Crypto15mBacktest | null>;
    onAutoOff: (cb: (d: { reason: string; gained: number; target: number }) => void) => () => void;
  };
  scripts: {
    list: () => Promise<{ scripts: UserScript[] }>;
    save: (s: { id?: string; name?: string; description?: string; code: string; notes?: string }) =>
      Promise<{ script: UserScript; errors: string[]; warnings: string[];
                audit: ScriptAudit; disarmed?: boolean }>;
    delete: (id: string) => Promise<{ ok: boolean }>;
    setEnabled: (id: string, enabled: boolean) => Promise<{ script: UserScript }>;
    setAssets: (id: string, assets: string[] | null) => Promise<{ script: UserScript }>;
    setDryRun: (id: string, dryRun: boolean) => Promise<{ script: UserScript }>;
    shadowOrders: (id: string, limit?: number) => Promise<{ orders: ScriptShadowOrder[] }>;
    exportFile: (name: string, code: string) => Promise<ActionResult<string>>;
    importFile: () => Promise<ActionResult<string>>;
    validate: (code: string) => Promise<ScriptValidation>;
    backtest: (args: { id?: string; code?: string; sinceDays?: number; assets?: string[] | null; config?: Record<string, unknown> }) =>
      Promise<ScriptBacktest | null>;
    contextPack: () => Promise<{ text: string }>;
    docs: () => Promise<ScriptApiDocs>;
    exportPack: () => Promise<ActionResult<string>>;
    onStatus: (cb: (d: { id: string; enabled: boolean; lastError?: string }) => void) => () => void;
    onLog: (cb: (d: { id: string; lines: string[] }) => void) => () => void;
  };
  copy: {
    status: () => Promise<CopyStatus>;
  };
  polymarket: {
    marketUrl: (args: { eventTicker?: string; ticker?: string; env?: string }) =>
      Promise<{ url: string }>;
  };
  terminal: {
    discover: (args: { column: DiscoverColumn; limit?: number; filters?: DiscoverFilters })
      => Promise<DiscoverResult>;
    search: (args: { query: string; limit?: number }) => Promise<DiscoverResult>;
    market: (conditionId: string) => Promise<{ market: MarketSummary | null }>;
    book: (conditionId: string, side: 'yes' | 'no') => Promise<BookSnapshot>;
    history: (conditionId: string, side: 'yes' | 'no', interval: TerminalInterval)
      => Promise<PriceSeries>;
    tape: (conditionId: string, limit?: number) => Promise<TapeResult>;
    resolution: (conditionId: string) => Promise<ResolutionReport>;
    micro: (conditionId: string, side: 'yes' | 'no') => Promise<MicroSeries>;
    related: (conditionId: string, limit?: number) => Promise<RelatedMarkets>;
    crossVenue: (conditionId: string, interval: TerminalInterval)
      => Promise<CrossVenueComparison>;
    health: () => Promise<TerminalHealth>;
    watchlist: (args: { action: 'list' | 'add' | 'remove'; conditionId?: string; title?: string })
      => Promise<{ entries: WatchEntry[] }>;
    preview: (order: ManualOrderRequest) => Promise<ManualOrderPreview>;
    place: (order: ManualOrderRequest) => Promise<ManualOrderResult>;
    cancel: (orderId: string) => Promise<{ ok: boolean; message: string }>;
    orders: (conditionId?: string)
      => Promise<{ orders: RestingOrder[]; fetchedAt: string; note: string | null }>;
    standing: (args: {
      action: 'list' | 'add' | 'cancel' | 'arm';
      id?: string; armed?: boolean; item?: Partial<StandingInstruction>;
    }) => Promise<{ instructions: StandingInstruction[]; error: string | null }>;
    flattenPreview: () => Promise<FlattenPreview>;
    flattenWallet: () => Promise<FlattenResult>;
    sellPreview: (order: SellPositionRequest) => Promise<SellPreview>;
    sellPosition: (order: SellPositionRequest) => Promise<SellResult>;
    portfolio: () => Promise<TerminalPortfolio>;
    settled: () => Promise<SettledRecord>;
    fills: (limit?: number) => Promise<{ fills: TerminalFill[]; fetchedAt: string; note: string | null }>;
    orderLog: (limit?: number) => Promise<{ orders: TerminalOrderLogRow[]; fetchedAt: string }>;
    reconcile: () => Promise<{
      inserted: number; matched: number;
      corrected?: number;
      unmapped?: number;
      scanned?: number; complete?: boolean; note: string | null;
    }>;
    onStanding: (cb: (e: StandingEvent) => void) => () => void;
    paperPlace: (order: ManualOrderRequest) => Promise<PaperOrderResult>;
    paperPortfolio: () => Promise<PaperPortfolio>;
    paperOrders: (limit?: number) => Promise<{ orders: PaperOrder[]; fetchedAt: string }>;
    paperCancel: (localId: number) => Promise<{ ok: boolean; message: string }>;
    paperReset: () => Promise<{ cleared: number; message: string }>;
  };
  ai: {
    status: () => Promise<AiStatus>;
    analyze: (conditionId: string) => Promise<AiAnalysis>;
    setKey: (provider: AiProvider, key: string)
      => Promise<{ ok: boolean; message: string; models?: string[]; masked?: string }>;
    listModels: (provider: AiProvider)
      => Promise<{ ok: boolean; models: string[]; message?: string }>;
    checkProvider: (provider: AiProvider) => Promise<AiProviderCheck>;
    scoreboard: (recent?: number) => Promise<ForecastScoreboard>;
  };
  autopilot: {
    status: () => Promise<AutopilotStatus | null>;
    runNow: () => Promise<{ ok: boolean; message: string }>;
  };
  paper: {
    status: () => Promise<PaperStatus | null>;
    reset: () => Promise<ActionResult<unknown>>;
  };
  health: {
    check: (deep?: boolean) => Promise<HealthReport | null>;
  };
  remote: {
    status: () => Promise<RemoteStatus>;
    setToken: (platform: 'discord' | 'telegram', token: string)
      => Promise<{ ok: boolean; message: string; username?: string }>;
    pairCode: (platform: 'discord' | 'telegram')
      => Promise<{ code: string; expiresAt: string; ttlSec: number }>;
    addPaired: (platform: 'discord' | 'telegram', id: string, label?: string)
      => Promise<{ paired: RemotePairedAccount[]; error: string | null }>;
    unpair: (platform: 'discord' | 'telegram', id: string)
      => Promise<{ paired: RemotePairedAccount[] }>;
    sync: () => Promise<RemoteStatus>;
    test: () => Promise<{ sent: number; message: string }>;
  };
  mcp: {
    status: () => Promise<McpStatus>;
    sync: () => Promise<McpStatus>;
    rotateToken: (agentId: string) => Promise<McpStatus>;
    paper: (agentId: string, reset?: boolean) => Promise<AgentPaper>;
    decide: (id: number, approve: boolean) => Promise<{ ok: boolean; message: string }>;
    settleUnknown: (id: number) => Promise<{ ok: boolean; status?: string; message: string }>;
    activity: (limit?: number) => Promise<McpActivity>;
    seen: () => Promise<McpSeen>;
    copyHttpSnippet: (agentId: string) => Promise<{ ok: boolean; message?: string }>;
    clients: (agentId?: string) => Promise<McpClientSetup[]>;
    install: (clientId: McpClientId, agentId: string) => Promise<McpInstallResult>;
    uninstall: (clientId: McpClientId, agentId: string) => Promise<McpInstallResult>;
    copyConfig: (clientId: McpClientId, agentId: string) => Promise<{ ok: boolean; message?: string }>;
    openConfigFolder: (clientId: McpClientId) => Promise<{ ok: boolean; message?: string }>;
    closeAgentPaper: (agentId: string) => Promise<{
      ok: boolean; message: string; closed?: unknown[]; open?: unknown[];
    }>;
    onOrder: (cb: (d: McpOrderEvent) => void) => () => void;
    onToolCall: (cb: (d: McpToolCallEvent) => void) => () => void;
  };
  logs: {
    tail: (limit?: number) => Promise<LogEntry[]>;
    onAppend: (cb: (entry: LogEntry) => void) => () => void;
    clear: () => Promise<ActionResult>;
    openFolder: () => Promise<void>;
  };
  window: {
    minimize: () => void;
    maximize: () => void;
    close: () => void;
    isMaximized: () => Promise<boolean>;
    onMaximizeChange: (cb: (max: boolean) => void) => () => void;
    isVisible: () => Promise<boolean>;
    onVisibility: (cb: (visible: boolean) => void) => () => void;
  };
}

export interface PositionFilter {
  status?: BotPosition['status'][];
  resolved?: boolean | null;
  signalSource?: SignalSource | null;
  limit?: number;
}

export interface SignalFilter {
  source?: SignalSource | null;
  minConfidence?: number;
  minEdge?: number;
  resolved?: boolean | null;
  limit?: number;
}

declare global {
  interface Window {
    krypt: KryptApi;
  }
}

export type PerpsCategory = 'crypto' | 'equity' | 'index' | 'commodity';

export interface PerpsMarketRow {
  instrumentId: number;
  symbol: string;
  displaySymbol: string;
  base: string;
  category: PerpsCategory | string;
  closeOnly: boolean;
  isolatedOnly: boolean;
  maxLeverage: number;
  minNotional: number;
  priceDecimals: number;
  quantityDecimals: number;
  markPrice: number | null;
  indexPrice: number | null;
  midPrice: number | null;
  indexDevBps: number | null;
  spreadBps: number | null;
  fundingRate: number | null;
  fundingAprPct: number | null;
  openInterestUsd: number;
  vol7dUsd: number | null;
  avgDailyVolUsd: number | null;
  competingScore: number | null;
  competingBidScore: number | null;
  competingAskScore: number | null;
  depthAgeSec: number | null;
  poolUsd: number;
  estSharePct: number | null;
  estRewardUsd: number | null;
}

export interface PerpsBoard {
  ok: boolean;
  error?: string;
  markets: PerpsMarketRow[];
  poolPerMarketUsd: number;
  dailyPoolUsd: number;
  activeMarkets: number;
  exchangeVol7dUsd: number;
  volumeCoverage: number;
  makerVol7dNeededUsd: number;
  myQuoteScore: number;
  fetchedAt: number;
}

export interface PerpsQuote { buy: boolean; price: number; qty: number; level?: number }

export interface PerpsEngineMarket {
  instrumentId: number;
  symbol: string;
  selected: boolean;
  state: 'idle' | 'off' | 'quoting' | 'pulled' | 'blocked' | 'killed' | 'halted' | 'error';
  note: string;
  bestBid: number | null;
  bestAsk: number | null;
  mid: number | null;
  competingScore: number | null;
  desired: PerpsQuote[];
  resting: PerpsQuote[];
  positionQty: number;
  positionEntry: number | null;
  positionValueUsd: number | null;
  unrealizedPnlUsd: number | null;
  liquidationPrice: number | null;
  todayFills: number;
  todayVolumeUsd: number;
  todayPnlUsd: number;
  avgMarkoutBps: number | null;
  markoutN: number;
  priceSource?: 'live' | 'rest' | null;
}

export interface PerpsFill {
  tradeId: number;
  instrumentId: number;
  symbol: string;
  side: 'long' | 'short';
  price: number;
  quantity: number;
  notional: number;
  taker: boolean;
  fee: number;
  pnl: number;
  markoutBps: number | null;
  ts: number;
}

export interface PerpsTotals {
  fills: number;
  volumeUsd: number;
  makerVolumeUsd: number;
  feesUsd: number;
  realizedPnlUsd: number;
  netPnlUsd?: number;
  unrealizedPnlUsd?: number;
}

export interface PerpsBacktestArgs {
  days?: number;
  markets?: string[];
  variants?: { name: string; config: Partial<TraderConfig> }[];
  grid?: Partial<Record<'perpsQuoteBps' | 'perpsSizeUsd' | 'perpsSkewBps' | 'perpsMaxInventoryUsd'
    | 'perpsRequoteBps' | 'perpsLevels' | 'perpsVolPullBps' | 'perpsMarkoutKillBps', number[]>>;
  feeTier?: number;
  queueShare?: number;
  latencyMs?: number;
}

export interface PerpsBacktestMarket {
  symbol: string;
  instrumentId: number;
  trades: number;
  ticks: number;
  dataComplete: boolean;
  dataError: string | null;
  fills: number;
  volumeUsd: number;
  volumePerDayUsd: number;
  feesUsd: number;
  realizedPnlUsd: number;
  fundingUsd: number;
  unrealizedPnlUsd: number;
  netPnlUsd: number;
  netPerDayUsd: number;
  endPositionQty: number;
  maxInventoryUsd: number;
  maxDrawdownUsd: number;
  avgMarkoutBps: number | null;
  uptimePct: number;
  avgQuoteScore: number;
  estRewardPerDayUsd: number | null;
  estRewardSharePct: number | null;
  realTouchPct: number;
  days: number;
  equity: [number, number][];
}

export interface PerpsBacktestRun {
  name: string;
  config?: Partial<TraderConfig>;
  totals: {
    fills: number; volumeUsd: number; volumePerDayUsd: number; feesUsd: number;
    realizedPnlUsd: number; fundingUsd: number; unrealizedPnlUsd: number; netPnlUsd: number;
    netPerDayUsd: number; maxInventoryUsd: number; avgMarkoutBps: number | null;
    estRewardPerDayUsd: number | null;
  };
  markets: PerpsBacktestMarket[];
}

export interface PerpsBacktestResult {
  ok: boolean;
  message?: string;
  startMs?: number;
  endMs?: number;
  days?: number;
  feeTier?: number;
  makerFeeBps?: number;
  queueShare?: number;
  latencyMs?: number;
  markets?: string[];
  runs?: PerpsBacktestRun[];
  elapsedSec?: number;
  assumptions?: string[];
}

export interface PerpsStatus {
  enabled: boolean;
  link: {
    linked: boolean; expired: boolean; expiresAt: number | null;
    proxy: string | null; owner: string | null; walletPresent: boolean;
    renewError?: string;
  };
  trading: boolean;
  haltReason: string;
  lastError: string;
  account: {
    equityUsd: number | null; availableMarginUsd: number | null; withdrawableUsd: number | null;
    positionValueUsd: number | null; inLiquidation: boolean; feeTier: number | null;
  } | null;
  stats7d: {
    volumeUsd: number | null; makerVolumeUsd: number | null;
    makerShare: number | null; entityMakerShare: number | null;
  } | null;
  localWeek: PerpsTotals;
  rewards: Record<string, unknown>[];
  today: PerpsTotals;
  markets: PerpsEngineMarket[];
  recentFills: PerpsFill[];
  feed?: {
    running: boolean; connected: boolean; subscribed: number[]; messages: number;
    lastMessageAgoSec: number | null; uptimeSec: number;
  };
}

export interface OfficialPnl {
  ok: boolean;
  message?: string;
  user?: string;
  interval?: string;
  points?: number;
  from?: number;
  to?: number;
  economic_pnl?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  trade_pnl?: number;
  settled_pnl?: number;
  fees_paid?: number;
  fees_refunded?: number;
  maker_rebate?: number;
  taker_rebate?: number;
  reward_income?: number;
  referral_income?: number;
  volume_usdc?: number;
  trade_count?: number;
  rebatesAndRewards?: number;
  series?: [number, number][];
}

export interface ResolutionState {
  status: string | null;
  expectedSettlementTime: string | null;
  settlementTimeBasis: string | null;
  extendedReview: boolean;
  wasDisputed: boolean;
  proposedPrice: string | null;
}

export interface MarketHolder {
  wallet: string;
  name: string;
  outcomeIndex: number | null;
  shares: number | null;
  avgPrice: number | null;
  currentValue: number | null;
  totalPnl: number | null;
  realizedPnl: number | null;
  unrealizedPnl: number | null;
}

export interface BiggestWinner {
  rank: number;
  wallet: string;
  name: string;
  pnl: number | null;
  initialValue: number | null;
  eventTitle: string;
  eventSlug: string;
  resolvedAt: number | null;
}
