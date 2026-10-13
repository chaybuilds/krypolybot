import type { AccountSnapshot, TraderConfig } from '@shared/types';
import { isLive } from './account';

export interface BalanceContext {
  config: Partial<TraderConfig> | null | undefined;
  authOk: boolean;
  hasKeys: boolean | null;
  engineRunning?: boolean;
}

export interface BalanceView {
  known: boolean;
  totalUsd: number | null;
  cashUsd: number | null;
  portfolioUsd: number | null;
  sessionPnlUsd: number | null;
  sessionRoiPct: number | null;
  alltimePnlUsd: number | null;
  todayPnlUsd: number | null;
  roiPct: number | null;
  why: string | null;
}

const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export function balanceUnknownReason(a: AccountSnapshot | null | undefined, ctx: BalanceContext): string | null {
  if (!a || a.backendDown) return 'No balance yet: the trading engine is starting or stopped.';
  if (ctx.engineRunning === false) return 'The trading engine is not running, so there is no current balance.';
  if (ctx.hasKeys === false) return 'No wallet connected, so there is no wallet balance to show.';
  if (isLive(ctx.config) && !ctx.authOk) {
    return 'Polymarket has not accepted the connected wallet yet, so the balance is unknown.';
  }
  if (fin(a.totalUsd) === null) return 'No balance read yet.';
  return null;
}

export function todayPnlOf(a: AccountSnapshot | null | undefined): number | null {
  return a && fin(a.todayBaselineUsd) !== null ? fin(a.todayPnlUsd) : null;
}

export function balanceView(a: AccountSnapshot | null | undefined, ctx: BalanceContext): BalanceView {
  const why = balanceUnknownReason(a, ctx);
  if (why || !a) {
    return {
      known: false, totalUsd: null, cashUsd: null, portfolioUsd: null, sessionPnlUsd: null,
      sessionRoiPct: null, alltimePnlUsd: null, todayPnlUsd: null, roiPct: null, why,
    };
  }
  const session = (fin(a.sessionBaselineUsd) ?? 0) > 0 ? fin(a.sessionPnlUsd) : null;
  const alltime = fin(a.alltimeBaselineUsd) !== null ? fin(a.alltimePnlUsd) : null;
  const today = todayPnlOf(a);
  return {
    known: true,
    totalUsd: fin(a.totalUsd),
    cashUsd: fin(a.cashUsd),
    portfolioUsd: fin(a.portfolioUsd),
    sessionPnlUsd: session,
    sessionRoiPct: session === null ? null : fin(a.sessionRoiPct),
    alltimePnlUsd: alltime,
    todayPnlUsd: today,
    roiPct: (fin(a.startBankrollUsd) ?? 0) > 0 ? fin(a.roiPct) : null,
    why: null,
  };
}

export const PAPER_BADGE_TITLE =
  'The app is in Paper: orders go to the paper book (real Polymarket order books, imaginary money) '
  + 'and the engines send nothing. Balances shown here are your real wallet, which nothing will spend.';
