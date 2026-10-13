import { describe, expect, it } from 'vitest';
import type { CredentialsStatusAll, TraderConfig } from '@shared/types';
import {
  accountGoLiveChecklist, accountGoLivePatch, backToPaperAccountPatch, liveSwitchLines,
  liveSwitchMessage, reachableLiveAgents, remoteTradingArmed, walletFingerprint,
} from '../src/utils/goLive';
import { accountModeOf, isLive, MODE_LABEL } from '../src/utils/account';

const creds = (has: boolean): CredentialsStatusAll => ({
  current: 'mainnet',
  mainnet: { hasWalletKey: has, hasApiCreds: has, address: has ? '0xAbC' : '', addressPreview: '' },
});
const verified = { ok: true, message: 'ok', fingerprint: walletFingerprint(creds(true)), balanceUsd: 40 };
const base = { config: { accountMode: 'paper' as const }, armed: [] as string[] };

describe('the account Go live checklist', () => {
  it('a no-wallet Paper user is blocked at the wallet, told where to connect one', () => {
    const l = accountGoLiveChecklist({ ...base, creds: creds(false), verify: null, ack: true });
    expect(l.ready).toBe(false);
    expect(l.steps[0].state).toBe('block');
    expect(l.steps[0].detail).toMatch(/Wallet page/);
  });

  it('needs a test of the current wallet and an explicit acknowledgement', () => {
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: null, ack: true }).ready).toBe(false);
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: false }).ready).toBe(false);
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: true }).ready).toBe(true);
  });

  it('the wallet fingerprint follows the address, case-insensitively, and is empty without one', () => {
    expect(walletFingerprint(creds(true))).toBe('0xabc|');
    expect(walletFingerprint(creds(false))).toBe('');
    expect(walletFingerprint(null)).toBe('');
  });

  it('names every arm that starts spending, and still lets you go live knowingly', () => {
    const cfg: Partial<TraderConfig> = {
      accountMode: 'paper', enableTrading: true, crypto15mEnabled: true, copyEnabled: true,
      copyWallets: ['0x1'], perpsEnabled: true, scriptsLiveEnabled: true,
    };
    const armed = liveSwitchLines(cfg);
    expect(armed).toHaveLength(5);
    expect(armed.join(' | ')).toMatch(/Main engine.*Crypto.*Copy trading: on \(1 wallet\).*Perps.*Scripts/);
    const l = accountGoLiveChecklist({ config: cfg, creds: creds(true), verify: verified, armed, ack: true });
    expect(l.steps.find((s) => s.id === 'engines')!.state).toBe('warn');
    expect(l.ready).toBe(true);
  });

  it('says plainly when nothing is armed', () => {
    expect(liveSwitchLines({ accountMode: 'paper' })).toEqual([]);
    expect(liveSwitchMessage({ accountMode: 'paper' })).toMatch(/Nothing is armed/);
    const l = accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: true });
    expect(l.steps.find((s) => s.id === 'engines')!.state).toBe('ok');
  });

  it('counts agents only when a live agent can actually reach the app', () => {
    const agents = [{ id: 'default', name: 'Default', mode: 'live', enabled: true }];
    const cfg = { mcpTradeMode: 'live', mcpAgents: agents } as unknown as Partial<TraderConfig>;
    expect(reachableLiveAgents(cfg)).toEqual([]);
    expect(liveSwitchLines({ ...cfg, mcpEnabled: true }).join()).toMatch(/AI agents LIVE: Default \(each order waits/);
    expect(liveSwitchLines({ ...cfg, autopilotEnabled: true, mcpLiveApproval: false }).join())
      .toMatch(/approval OFF, Autopilot on/);
    expect(liveSwitchLines({ ...cfg, mcpEnabled: true, mcpTradeMode: 'paper' })).toEqual([]);
  });

  it('remote trading is an arm only with remote on, trading on and a transport on', () => {
    expect(remoteTradingArmed({ remoteEnabled: true, remoteTradingEnabled: true, remoteTelegramEnabled: true })).toBe(true);
    expect(remoteTradingArmed({ remoteEnabled: true, remoteTradingEnabled: true })).toBe(false);
    expect(remoteTradingArmed({ remoteTradingEnabled: true, remoteDiscordEnabled: true })).toBe(false);
  });

  it('writes the account mode and nothing else; Paper is always one write away', () => {
    expect(accountGoLivePatch()).toEqual({ accountMode: 'live' });
    expect(backToPaperAccountPatch()).toEqual({ accountMode: 'paper' });
    expect(accountGoLivePatch()).not.toHaveProperty('terminalPaperMode');
  });
});

describe('reading the mode', () => {
  it('is Live only on the exact word', () => {
    expect(isLive({ accountMode: 'live' })).toBe(true);
    expect(isLive({ accountMode: 'paper' })).toBe(false);
    expect(isLive({ accountMode: 'LIVE' as never })).toBe(false);
    expect(isLive(null)).toBe(false);
    expect(accountModeOf(undefined)).toBe('paper');
    expect(MODE_LABEL[accountModeOf({ accountMode: 'live' })]).toBe('Live');
  });
});
