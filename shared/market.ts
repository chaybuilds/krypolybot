export type DataSource =
  | 'gamma'
  | 'clob'
  | 'clob-ws'
  | 'data-api'
  | 'ledger'
  | 'local';

export const DATA_SOURCE_LABEL: Record<DataSource, string> = {
  gamma: 'Gamma API',
  clob: 'CLOB API',
  'clob-ws': 'Our live feed',
  'data-api': 'Data API',
  ledger: 'Your fill ledger',
  local: 'Computed locally',
};

export interface SourceStamp {
  source: DataSource;
  observedAt: string;
  ageSec: number | null;
}

export type MarketField =
  | 'probability' | 'bid' | 'ask' | 'spread' | 'mid'
  | 'volume24h' | 'volumeTotal' | 'liquidity'
  | 'priceChange24h' | 'priceChange1h'
  | 'closeTime' | 'status' | 'lastTrade' | 'book' | 'history'
  | 'avgCost';

export type Provenance = Partial<Record<MarketField, SourceStamp>>;

export interface UnknownReason {
  field: MarketField | string;
  reason: string;
}

export type MarketStatus = 'open' | 'closed' | 'settled' | 'inactive' | 'unknown';

export type Venue = 'polymarket' | 'kalshi';

export const VENUE_LABEL: Record<Venue, string> = {
  polymarket: 'Polymarket',
  kalshi: 'Kalshi',
};

export interface MarketSummary {
  conditionId: string;
  venue: Venue;
  title: string;
  slug: string | null;
  eventSlug: string | null;
  category: string | null;
  status: MarketStatus;
  outcomes: [string, string] | null;

  probability: number | null;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  mid: number | null;

  volume24hUsd: number | null;
  volumeTotalUsd: number | null;
  liquidityUsd: number | null;

  priceChange24h: number | null;
  priceChange1h: number | null;

  closeTime: string | null;
  hoursToClose: number | null;

  negRisk: boolean | null;
  tickSize: number | null;
  minOrderSize: number | null;
  yesTokenId: string | null;
  noTokenId: string | null;

  resolvedOutcome: 'yes' | 'no' | null;

  sources: Provenance;
  unknowns: UnknownReason[];
}

export interface BookLevel {
  priceCents: number;
  size: number;
  cumulative: number;
  notionalUsd: number;
}

export interface BookSnapshot {
  conditionId: string;
  tokenSide: 'yes' | 'no';
  bids: BookLevel[];
  asks: BookLevel[];
  bestBid: number | null;
  bestAsk: number | null;
  spreadCents: number | null;
  midCents: number | null;
  bidDepthUsd: number | null;
  askDepthUsd: number | null;
  available: boolean;
  fetchedAt: string;
  source: DataSource;
  note: string | null;
}

export type TerminalInterval = '1h' | '6h' | '1d' | '1w' | 'max';

export const INTERVALS: { id: TerminalInterval; label: string }[] = [
  { id: '1h', label: '1H' },
  { id: '6h', label: '6H' },
  { id: '1d', label: '1D' },
  { id: '1w', label: '1W' },
  { id: 'max', label: 'MAX' },
];

export interface PricePoint {
  t: number;
  p: number;
}

export interface PriceSeries {
  conditionId: string;
  side: 'yes' | 'no';
  interval: TerminalInterval;
  points: PricePoint[];
  source: DataSource;
  ownFeed: boolean;
  thin: boolean;
  note: string | null;
}

export type RiskLevel = 'ok' | 'watch' | 'risk' | 'unknown';

export interface ResolutionCheck {
  id: string;
  label: string;
  level: RiskLevel;
  detail: string;
  resolved: boolean;
}

export interface ResolutionReport {
  conditionId: string;
  checks: ResolutionCheck[];
  resolvedCount: number;
  totalCount: number;
  score: number | null;
  scoreNote: string;
  resolverName: string | null;
  resolutionSource: string | null;
  fetchedAt: string;
}

export const MIN_RESOLVED_CHECKS = 4;

export interface TapeTrade {
  ts: string;
  side: 'yes' | 'no';
  action: 'buy' | 'sell' | null;
  priceCents: number | null;
  size: number | null;
  notionalUsd: number | null;
  taker: string | null;
  source: DataSource;
}

export type DiscoverColumn =
  | 'new'
  | 'closing'
  | 'volume'
  | 'volatility'
  | 'watchlist';

export const DISCOVER_COLUMNS: { id: DiscoverColumn; label: string; hint: string }[] = [
  { id: 'new', label: 'New', hint: 'Recently listed markets' },
  { id: 'closing', label: 'Closing soon', hint: 'Still tradable, closing soonest' },
  { id: 'volume', label: 'High volume', hint: 'Most traded in the last 24h' },
  { id: 'volatility', label: 'High volatility', hint: 'Biggest 24h price move' },
  { id: 'watchlist', label: 'Watchlist', hint: 'Markets you starred' },
];

export interface DiscoverFilters {
  minVolume24hUsd?: number | null;
  maxVolume24hUsd?: number | null;
  minLiquidityUsd?: number | null;
  minProbability?: number | null;
  maxProbability?: number | null;
  maxHoursToClose?: number | null;
  minHoursToClose?: number | null;
  categories?: string[];
  search?: string;
}

export interface DiscoverResult {
  column: DiscoverColumn;
  rows: MarketSummary[];
  fetchedAt: string;
  skippedForUnknown: number;
  excluded: number;
  note: string | null;
}

export type OrderType = 'GTC' | 'FAK' | 'FOK';

export interface ManualOrderRequest {
  conditionId: string;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  priceCents: number;
  size: number;
  orderType: OrderType;
  confirmUsd: number;
  paper?: boolean;
}

export interface ManualOrderPreview {
  conditionId: string;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  priceCents: number | null;
  requestedPriceCents: number;
  size: number | null;
  requestedSize: number;
  costUsd: number | null;
  fillableSize: number | null;
  estimatedAvgCents: number | null;
  slippageCents: number | null;
  feeUsd: number | null;
  maxPayoutUsd: number | null;
  maxLossUsd: number | null;
  tickSize: number | null;
  minOrderSize: number | null;
  bestBidCents: number | null;
  bestAskCents: number | null;
  crossesNow: boolean | null;
  availableUsd: number | null;
  warnings: string[];
  blockers: string[];
}

export interface ManualOrderResult {
  ok: boolean;
  orderId: string | null;
  status: string | null;
  filledSize: number | null;
  avgPriceCents: number | null;
  message: string;
  localId: number | null;
}

export interface FlattenPreviewRow {
  conditionId: string;
  side: 'yes' | 'no';
  size: number;
  bidCents: number | null;
  fillableSize: number | null;
  proceedsUsd: number | null;
  blocked: string | null;
  partial: string | null;
  engineTracked: boolean | null;
}

export interface FlattenPreview {
  ok: boolean;
  positions: FlattenPreviewRow[];
  count: number;
  skipped: number;
  settledSkipped?: number;
  proceedsUsd: number | null;
  engineTracked: number | null;
  paper: boolean;
  message: string;
}

export interface FlattenResultRow {
  conditionId: string;
  side: 'yes' | 'no';
  size: number;
  ok: boolean;
  message: string;
}

export interface FlattenResult {
  ok: boolean;
  attempted: number;
  sold: number;
  refused: number;
  skipped: number;
  settledSkipped?: number;
  proceedsUsd: number | null;
  engineTrackedSold: number | null;
  reason: string;
  results: FlattenResultRow[];
  message: string;
}

export interface SellPositionRequest {
  conditionId: string;
  side: 'yes' | 'no';
  size?: number;
  maxSlippageCents?: number;
  confirmUsd?: number;
}

export type SellBasisSource = 'ledger' | 'data-api' | 'paper' | null;

export interface SellPreview {
  conditionId: string;
  side: 'yes' | 'no';
  sideLabel: string | null;
  title: string | null;
  paper: boolean;
  positionSize: number | null;
  requestedSize: number | null;
  size: number | null;
  fillableSize: number | null;
  bestBidCents: number | null;
  limitCents: number | null;
  estimatedAvgCents: number | null;
  worstFillCents: number | null;
  maxSlippageCents: number;
  proceedsUsd: number | null;
  feeUsd: number | null;
  netProceedsUsd: number | null;
  avgCostCents: number | null;
  basisSource: SellBasisSource;
  costBasisUsd: number | null;
  estimatedPnlUsd: number | null;
  markCents: number | null;
  settled: boolean;
  resolvedOutcome: 'yes' | 'no' | null;
  minOrderSize: number | null;
  tickSize: number | null;
  warnings: string[];
  blockers: string[];
  note: string;
}

export interface SellResult {
  ok: boolean;
  orderId: string | null;
  status: string | null;
  localId: number | null;
  sentSize: number | null;
  limitCents: number | null;
  filledSize: number | null;
  avgPriceCents: number | null;
  proceedsUsd: number | null;
  killedSize: number | null;
  estimatedProceedsUsd: number | null;
  message: string;
  preview: SellPreview | null;
}

export type OrderOrigin =
  | 'manual' | 'main' | 'crypto15m' | 'copy' | 'script' | 'ai' | 'external' | 'unknown';

export const ORIGIN_LABEL: Record<OrderOrigin, string> = {
  manual: 'You',
  main: 'Main engine',
  crypto15m: 'Crypto engine',
  copy: 'Copy trading',
  script: 'Your script',
  ai: 'AI agent',
  external: 'Placed elsewhere',
  unknown: 'Unknown',
};

export interface RestingOrder {
  orderId: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  priceCents: number | null;
  size: number | null;
  filledSize: number | null;
  remainingSize?: number | null;
  bestTouchCents?: number | null;
  createdAt: string | null;
  origin: OrderOrigin;
  source: DataSource;
}

export type StandingKind = 'stopLoss' | 'takeProfit' | 'alertAbove' | 'alertBelow';

export const STANDING_LABEL: Record<StandingKind, string> = {
  stopLoss: 'Stop loss',
  takeProfit: 'Take profit',
  alertAbove: 'Alert above',
  alertBelow: 'Alert below',
};

export interface StandingInstruction {
  id: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  kind: StandingKind;
  triggerCents: number;
  size: number | null;
  createdAt: string;
  armed: boolean;
  lastCheckedAt: string | null;
  firedAt: string | null;
  note: string | null;
}

export interface TerminalPosition {
  conditionId: string;
  venue: Venue;
  title: string | null;
  slug: string | null;
  side: 'yes' | 'no';
  sideLabel?: string | null;
  size: number;
  avgCostCents: number | null;
  costBasisUsd: number | null;
  basisSource?: 'ledger' | 'data-api' | null;
  markCents: number | null;
  valueUsd: number | null;
  unrealizedPnlUsd: number | null;
  realizedPnlUsd: number | null;
  settledPnlUsd: number | null;
  venueCashPnlUsd?: number | null;
  venueRealizedPnlUsd?: number | null;
  status: MarketStatus;
  resolvedOutcome: 'yes' | 'no' | null;
  resolvedOutcomeLabel?: string | null;
  outcomeLabels?: [string, string] | null;
  closeTime: string | null;
  unreconciled: boolean;
  unreconciledSize: number | null;
  settled: boolean;
  redeemable: boolean;
  sources: Provenance;
  unknowns: UnknownReason[];
}

export interface TerminalPortfolio {
  positions: TerminalPosition[];
  walletReadable?: boolean;
  totalValueUsd: number | null;
  totalCostUsd: number | null;
  totalUnrealizedPnlUsd: number | null;
  realizedPnlUsd: number | null;
  realizedFeesUsd?: number | null;
  realizedPnlNetUsd?: number | null;
  settledPnlUsd: number | null;
  venueCashPnlUsd?: number | null;
  walletPnlUsd?: number | null;
  wonMarkets?: number | null;
  lostMarkets?: number | null;
  bothSidesMarkets?: number | null;
  unknownResolutionMarkets?: number | null;
  closedOutMarkets?: number | null;
  ledgerMarkets?: number | null;
  recordNote?: string | null;
  unreconciledCount: number;
  unreconciledNote: string | null;
  venueBasisCount?: number;
  settledWorthlessCount?: number;
  redeemableCount: number;
  redeemableValueUsd: number | null;
  redeemableNote: string | null;
  cashUsd: number | null;
  fetchedAt: string;
  costBasisConvention: 'average';
}

export type SettledOutcome = 'won' | 'lost' | 'closedOut' | 'unknown';

export interface SettledRecordRow {
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  sideLabel: string | null;
  contracts: number;
  avgCostCents: number | null;
  costUsd: number;
  payoutUsd: number | null;
  pnlUsd: number | null;
  feesUsd: number | null;
  feesKnown: boolean;
  soldPnlUsd: number;
  outcome: SettledOutcome;
  resolvedOutcome: 'yes' | 'no' | null;
  resolvedOutcomeLabel: string | null;
  settledAt: string | null;
  inWallet: boolean | null;
  bothSides: boolean;
}

export interface SettledRecordTotals {
  won: number;
  lost: number;
  bothSides: number;
  closedOut: number;
  unknown: number;
  markets: number;
  settledMarkets: number;
  rows: number;
  pnlUsd: number | null;
  pnlMissingRows: number;
  feesUsd: number | null;
  feesKnown: boolean;
  unknownRows: number;
}

export interface SettledRecord {
  rows: SettledRecordRow[];
  totals: SettledRecordTotals;
  walletReadable: boolean;
  fetchedAt: string;
  note: string | null;
}

export interface TerminalFill {
  id: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  size: number;
  priceCents: number | null;
  feeUsd: number | null;
  costUsd: number | null;
  ts: string;
  reconciled: boolean;
  origin: OrderOrigin;
  source: DataSource;
}

export type MatchConfidence = 'strong' | 'possible';

export interface ComparedSides {
  polymarket: 'yes';
  kalshi: 'yes' | 'no';
}

export interface CrossVenueMatch {
  confidence: MatchConfidence;
  score: number;
  for: string[];
  against: string[];
  comparedSides: ComparedSides;
}

export interface CrossVenueSide {
  title: string | null;
  probability: number | null;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  closeTime: string | null;
  volume24hUsd: number | null;
  liquidityUsd: number | null;
}

export interface KalshiSide extends CrossVenueSide {
  ticker: string;
  subtitle: string | null;
  side: 'yes' | 'no';
  url: string;
}

export interface CrossVenuePoint {
  t: number;
  p: number | null;
  k: number | null;
}

export interface CrossVenueComparison {
  conditionId: string;
  interval: TerminalInterval;
  polymarket: CrossVenueSide | null;
  kalshi: KalshiSide | null;
  match: CrossVenueMatch | null;
  spreadCents: number | null;
  spreadNote: string | null;
  series: CrossVenuePoint[];
  overlapPeriods: number;
  historyNote: string | null;
  venueNote: string;
  caveats: string[];
  fetchedAt: string;
  note: string | null;
}

export interface RelatedMarket extends MarketSummary {
  titleOverlap: number;
  gapCents: number | null;
  contradicts: boolean;
}

export interface RelatedMarkets {
  conditionId: string;
  eventSlug: string | null;
  rows: RelatedMarket[];
  sumProbability: number | null;
  negRisk: boolean | null;
  fetchedAt: string;
  note: string | null;
}

export type AiLean = 'cheap' | 'rich' | 'fair' | 'no_view';

export type AiConfidence = 'low' | 'medium' | 'high';

export type AiProvider = 'anthropic' | 'openai';

export interface AiSource {
  url: string;
  title: string | null;
}

export interface AiAnalysis {
  ok: boolean;
  provider: AiProvider | string;
  model: string;
  askedAt: string;
  latencyMs: number | null;
  webSearch: boolean;
  sources: AiSource[];

  summary: string | null;
  resolutionNotes: string[];
  drivers: string[];
  unknowns: string[];
  reasoning: string[];
  lean: AiLean | null;
  confidence: AiConfidence | null;

  redactions: number;

  reason: string | null;
  fix: string | null;
}

export interface AiKeyState {
  present: boolean;
  masked: string | null;
}

export interface AiStatus {
  enabled: boolean;
  provider: AiProvider;
  webSearch: boolean;
  offline: boolean;
  models: Record<AiProvider, string>;
  keys: Record<AiProvider, AiKeyState>;
}

export const AI_LEAN_LABEL: Record<AiLean, string> = {
  cheap: 'Looks cheap',
  rich: 'Looks rich',
  fair: 'Looks about right',
  no_view: 'No view',
};

export interface PaperOrder {
  localId: number;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  orderType: string;
  requestedSize: number;
  requestedPriceCents: number;
  filledSize: number;
  avgFillPriceCents: number | null;
  restingSize: number;
  feesUsd: number | null;
  costUsd: number | null;
  status: 'filled' | 'partial' | 'resting' | 'unfilled' | 'canceled' | 'settled' | string;
  note: string | null;
  createdAt: string;
  filledAt: string | null;
}

export interface PaperPortfolio extends TerminalPortfolio {
  bankrollUsd: number;
  equityUsd: number | null;
  returnPct: number | null;
}

export interface PaperOrderResult {
  ok: boolean;
  localId: number | null;
  status: string | null;
  filledSize: number | null;
  avgPriceCents: number | null;
  message: string;
  warnings?: string[];
}

export interface WatchEntry {
  conditionId: string;
  title: string | null;
  addedAt: string;
  note: string | null;
}

export interface ProviderHealth {
  host: string;
  purpose: string;
  calls: number;
  errors: number;
  latencyMs: number | null;
  parkedUntil: string | null;
  cacheHits: number;
  cacheMisses: number;
}

export interface TerminalHealth {
  providers: ProviderHealth[];
  offline: boolean;
  fetchedAt: string;
}

export function dash(v: number | null | undefined, fmt?: (n: number) => string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return fmt ? fmt(v) : String(v);
}

export function fmtCents(v: number | null | undefined): string {
  return dash(v, (n) => `${n.toFixed(1)}¢`);
}

export function fmtProb(v: number | null | undefined): string {
  return dash(v, (n) => `${n.toFixed(1)}%`);
}

export function fmtUsdOrDash(v: number | null | undefined): string {
  return dash(v, (n) => {
    const a = Math.abs(n);
    const s = a >= 1_000_000 ? `${(a / 1_000_000).toFixed(2)}M`
      : a >= 1_000 ? `${(a / 1_000).toFixed(1)}K`
        : a.toFixed(2);
    return `${n < 0 ? '-' : ''}$${s}`;
  });
}

export function fmtUsdExact(v: number | null | undefined): string {
  return dash(v, (n) => `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`);
}

export function fmtSignedUsd(v: number | null | undefined): string {
  return dash(v, (n) => `${n < 0 ? '-' : '+'}$${Math.abs(n).toFixed(2)}`);
}

export function fmtHours(v: number | null | undefined): string {
  return dash(v, (n) => {
    if (n < 0) return `${(-n).toFixed(0)}h ago`;
    if (n < 1) return `${Math.round(n * 60)}m`;
    if (n < 48) return `${n.toFixed(1)}h`;
    return `${(n / 24).toFixed(0)}d`;
  });
}
