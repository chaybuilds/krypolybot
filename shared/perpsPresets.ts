import type { TraderConfig } from './types';

export const PERPS_PRESETS: { id: string; name: string; hint: string; patch: Partial<TraderConfig> }[] = [
  {
    id: 'reward-farmer',
    name: 'Reward Farmer',
    hint: 'Quote the 4 crypto markets with the best expected liquidity-reward share (thin books, where a small quote is a big slice of the ~$850/day pool), 3 bps each side of mid. Earns nothing until your 7-day maker share reaches 1% of exchange maker volume — pair it with Volume Builder or larger size until the eligibility bar fills.',
    patch: {
      perpsSelectMode: 'rewards', perpsMarketCount: 4, perpsCategories: ['crypto'],
      perpsQuoteBps: 3, perpsLevels: 1, perpsSizeUsd: 100,
      perpsMaxInventoryUsd: 300, perpsSkewBps: 4, perpsRequoteBps: 1.5,
      perpsVolPullBps: 20, perpsMarkoutKillBps: 15,
    },
  },
  {
    id: 'volume-builder',
    name: 'Volume Builder',
    hint: 'Join the touch on the deepest markets (BTC, ETH, indices, metals) to build maker volume — fee-tier progress, the 1% maker-share eligibility bar, and airdrop-style activity. Every fill pays the 0.0125% maker fee and some adverse selection; the inventory skew leans quotes to unload, but this is a cost centre whose payoff is the volume.',
    patch: {
      perpsSelectMode: 'volume', perpsMarketCount: 3, perpsCategories: ['crypto', 'index', 'commodity'],
      perpsQuoteBps: 0, perpsLevels: 1, perpsSizeUsd: 200,
      perpsMaxInventoryUsd: 600, perpsSkewBps: 6, perpsRequoteBps: 1,
      perpsVolPullBps: 15, perpsMarkoutKillBps: 10,
    },
  },
  {
    id: 'two-tier',
    name: 'Two-Tier Ladder',
    hint: 'Rewards mode with two quotes per side: one inside 5 bps (full reward weight) and one at 8 bps (quarter weight, but filled only on bigger moves). More resting size and score per market for the same fill risk at the inside quote.',
    patch: {
      perpsSelectMode: 'rewards', perpsMarketCount: 4, perpsCategories: ['crypto'],
      perpsQuoteBps: 3, perpsLevels: 2, perpsLevelStepBps: 5, perpsSizeUsd: 100,
      perpsMaxInventoryUsd: 400, perpsSkewBps: 5, perpsRequoteBps: 1.5,
      perpsVolPullBps: 20, perpsMarkoutKillBps: 15,
    },
  },
  {
    id: 'cautious',
    name: 'Cautious',
    hint: 'Small quotes at 8 bps (outside the 5 bps full-weight band, so a quarter of the reward score) with a tight inventory cap and a hair-trigger markout kill. For watching how fills behave on your account before committing size.',
    patch: {
      perpsSelectMode: 'rewards', perpsMarketCount: 3, perpsCategories: ['crypto'],
      perpsQuoteBps: 8, perpsLevels: 1, perpsSizeUsd: 50,
      perpsMaxInventoryUsd: 150, perpsSkewBps: 4, perpsRequoteBps: 2,
      perpsVolPullBps: 15, perpsMarkoutKillBps: 10,
    },
  },
];
