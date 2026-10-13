import type { AccountMode } from '@shared/market';
import type { TraderConfig } from '@shared/types';

type Cfg = Partial<Pick<TraderConfig, 'accountMode'>> | null | undefined;

export function isLive(c: Cfg): boolean {
  return c?.accountMode === 'live';
}

export function accountModeOf(c: Cfg): AccountMode {
  return isLive(c) ? 'live' : 'paper';
}

export const MODE_LABEL: Record<AccountMode, string> = {
  paper: 'Paper',
  live: 'Live',
};

export function backToPaperAccountPatch(): Partial<TraderConfig> {
  return { accountMode: 'paper' };
}

export function accountGoLivePatch(): Partial<TraderConfig> {
  return { accountMode: 'live' };
}
