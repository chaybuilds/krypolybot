import type { CollectionStats, ReplayNoData, TraderConfig } from './types';

export type C15Interval = '5m' | '15m' | 'hourly' | '4h';
const C15_INTERVAL_IDS: readonly string[] = ['5m', '15m', 'hourly', '4h'];
const C15_INTERVAL_CHIP: Record<C15Interval, string> = {
  '5m': '5 min', '15m': '15 min', hourly: '1 hour', '4h': '4 hours',
};

export function replayInterval(
  patch: Partial<TraderConfig>, current?: string | null,
): C15Interval {
  const iv = patch.crypto15mInterval ?? current ?? '15m';
  return C15_INTERVAL_IDS.includes(iv) ? (iv as C15Interval) : '15m';
}

export function intervalDataWarning(
  interval: C15Interval,
  c15: Pick<CollectionStats['c15'], 'byInterval' | 'recordingInterval'> | null | undefined,
  recordingOn = true,
): string | null {
  if (!c15?.byInterval) return null;
  if ((c15.byInterval[interval]?.resolved ?? 0) > 0) return null;
  const rec = c15.recordingInterval;
  const select =
    `select “${C15_INTERVAL_CHIP[interval]}” on the Crypto tab and leave the app open for a few days to collect them ` +
    '(this also changes which windows the crypto engine trades, if it is on)';
  if (!recordingOn) {
    return `No ${interval} windows recorded, and crypto data collection is off — turn it on below` +
      (rec && rec !== interval ? `, then ${select}.` : '.') +
      ' Until then this backtest has nothing to replay.';
  }
  if (rec === interval) {
    return `No ${interval} windows recorded yet. The recorder is collecting ${interval} now — ` +
      'leave the app open; this backtest has nothing to replay until windows settle.';
  }
  return `This strategy trades ${interval} windows and none are recorded: the recorder only records ` +
    `the interval selected on the Crypto tab${rec ? ` (currently ${rec})` : ''}. To test it, ${select}.`;
}

export function noDataHeadline(noData?: ReplayNoData | null): string {
  switch (noData?.reason) {
    case 'interval':
      return `No ${noData.interval} windows recorded`;
    case 'none-yet':
      return `No ${noData.interval} windows recorded yet`;
    case 'collection-off':
      return 'Crypto data collection is off';
    case 'assets':
      return `No recorded ${noData.interval} windows for this strategy's coins`;
    default:
      return 'No collected data yet';
  }
}

export const C15_PRESET_CORE: Record<
  'sniper' | 'sniper-5m' | 'paired' | 'favorite' | 'contrarian',
  Partial<TraderConfig>
> = {
  sniper: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'model', crypto15mUseRules: false,
    crypto15mModelMinEdgeCents: 2, crypto15mModelFinalMinute: true,
  },
  'sniper-5m': {
    crypto15mInterval: '5m', crypto15mAssets: ['BTC'],
    crypto15mDirectionMode: 'favorite', crypto15mUseRules: false,
    crypto15mPairedMode: false, crypto15mTimeDelayMin: 2,
    crypto15mEntryThreshold: 0.90, crypto15mEntryMax: 0.99,
    crypto15mMinDeltaPct: 0.0005,
  },
  paired: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'model', crypto15mUseRules: false,
    crypto15mPairedMode: true, crypto15mPairedMaxCombinedCents: 100,
    crypto15mPairedTilt1Cents: 3, crypto15mPairedTilt2Cents: 6,
    crypto15mPairedTilt3Cents: 10,
    crypto15mTimeDelayMin: 15, crypto15mEntryMax: 0.97,
  },
  favorite: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'favorite', crypto15mUseRules: false,
    crypto15mEntryThreshold: 0.95, crypto15mEntryMax: 0.98,
  },
  contrarian: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'contrarian', crypto15mUseRules: false,
    crypto15mEntryThreshold: 0.90,
  },
};
