import type { StrategyPreset, TraderConfig } from '../../shared/types';
import { DEFAULT_CONFIG } from './settings-store';

const merge = (over: Partial<TraderConfig>): TraderConfig => ({
  ...DEFAULT_CONFIG,
  ...over,
});

export const BUILTIN_STRATEGIES: StrategyPreset[] = [
  {
    id: 'krypt-edge',
    name: 'Edge Stack',
    tagline: 'Two sources at once — sports whales + sports momentum.',
    description:
      'Runs both signal sources together, both restricted to SPORTS: whale-following (the crypto-whale leg was dropped after it reversed to −3.9¢/contract on a bigger sample) and contrarian trade-cluster momentum (confidence ≥ 40, since momentum scores run low), with an 85¢ entry cap to skip near-decided favorites. Example configuration, not an edge: on the full recorded corpus (2026-09-01) whale-follow measured −2.42¢/contract and momentum −1.78¢ before spread. Start with a small balance you can afford to lose.',
    riskLabel: 'experimental',
    measured: 'Measured 2026-09-01 on the full recorded corpus: whale-follow −2.42¢/contract (n=8,360), momentum −1.78¢ (n=1,391), before spread.',
    config: merge({
      tradeWhales: true,
      tradeMomentum: true,
      contrarianOnly: true,
      allowedCategories: null,
      allowedWhaleCategories: ['sports'],
      allowedMomentumCategories: ['sports'],
      allowedMomentumSignalTypes: ['trade_cluster'],
      minConfidenceWhale: 55.0,
      minEdgePtsWhale: 5.0,
      minConfidenceMomentum: 40.0,
      minEntryPriceCents: 15,
      maxEntryPriceCents: 85,
    }),
  },
  {
    id: 'krypt-crypto-whale',
    name: 'Crypto Whale',
    tagline: 'Whale-following, crypto markets only.',
    description:
      'Follows $2.5k+ taker orders in CRYPTO markets only, momentum off, with the entry cap raised to 98¢ to allow the high-price favorites crypto whales tend to back. Did not hold up: a June sample (+9.3¢/contract, n=36) reversed to −3.9¢ on new data (n=143), and on the full recorded corpus (2026-09-01) whale-follow measures −2.42¢/contract before spread. Kept as an example configuration so existing users can see the update — not recommended.',
    riskLabel: 'experimental',
    measured: 'Measured: crypto whale-follow −3.9¢/contract (n=143); all whale-follow −2.42¢ (n=8,360, 2026-09-01), before spread.',
    config: merge({
      tradeWhales: true,
      tradeMomentum: false,
      allowedCategories: ['crypto'],
      minConfidenceWhale: 55.0,
      minEdgePtsWhale: 5.0,
      minEntryPriceCents: 15,
      maxEntryPriceCents: 98,
    }),
  },
  {
    id: 'krypt-aggressive',
    name: 'Krypt Aggressive',
    tagline: 'More signals, larger sizing, higher variance.',
    description:
      'Loosens edge gates to 3pts and confidence to 50%. Sizing scales 4-10% of bankroll, $100 cap, higher max-open count. High variance — use only with a bankroll you can stand to drop 30% on a bad day. Example configuration, not an edge: it loosens the same gates that measure negative on the full recorded corpus (2026-09-01).',
    riskLabel: 'aggressive',
    measured: 'Measured 2026-09-01: it loosens gates that lose −2.42¢ (whale) and −1.78¢ (momentum) per contract before spread, at larger size.',
    config: merge({
      minEdgePtsWhale: 3.0,
      minEdgePtsMomentum: 3.0,
      minConfidenceWhale: 50.0,
      minConfidenceMomentum: 50.0,
      baseSizeFraction: 0.06,
      minSizeFraction: 0.04,
      maxSizeFraction: 0.1,
      hardMaxPositionUsd: 100.0,
      maxOpenPositions: 40,
      maxDailyNewPositions: 80,
      maxTotalExposureFraction: 0.85,
      stopLossOnDay: -100.0,
    }),
  },
];

export function listStrategies(): StrategyPreset[] {
  return BUILTIN_STRATEGIES;
}

export function findStrategy(id: string): StrategyPreset | undefined {
  return BUILTIN_STRATEGIES.find((s) => s.id === id);
}
