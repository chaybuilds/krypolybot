import React, { useEffect, useState } from 'react';
import {
  ExternalLink, Eye, EyeOff, Gift, KeyRound, RefreshCcw,
  Save, ShieldAlert, ShieldCheck, Trash2, Wallet, Wifi, WifiOff,
} from 'lucide-react';
import type { CredentialsState, CredentialsStatusAll } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, Page, Section } from '../components/common';
import { cls } from '../utils/format';
import { POLYMARKET_REFERRAL_URL } from '../utils/links';

const SIGNATURE_TYPE_LABELS: Record<number, string> = {
  0: 'EOA',
  1: 'POLY_PROXY',
  2: 'POLY_GNOSIS_SAFE',
  3: 'POLY_1271',
};

export function ApiKeysPage() {
  const { backend, refresh } = useApp();
  const toast = useToast();
  const [statusAll, setStatusAll] = useState<CredentialsStatusAll | null>(null);

  const reload = async (): Promise<void> => {
    try {
      const all = await window.krypt.credentials.statusAll();
      setStatusAll(all);
    } catch {}
  };

  useEffect(() => {
    void reload();
  }, [backend.authOk]);

  const cred: CredentialsState | undefined = statusAll?.mainnet;

  return (
    <Page
      title="Wallet"
      subtitle="Connect a Polygon wallet to trade on Polymarket. Your private key is stored locally (encrypted with your Windows account) under %APPDATA%/Krypt PolyBot/credentials and never sent off-machine."
      actions={
        <button onClick={() => void reload()} className="krypt-btn-default" title="Re-read credential status from disk">
          <RefreshCcw className="h-4 w-4" /> Refresh
        </button>
      }
    >
      {!cred?.hasWalletKey && <ReferralBanner />}

      <Section title="Connection">
        <Card>
          <div className="flex flex-col items-start gap-4 md:flex-row md:items-center">
            <div className={cls(
              'grid h-10 w-10 shrink-0 place-items-center rounded-lg',
              backend.authOk ? 'bg-krypt-win/10 text-krypt-win' : 'bg-krypt-loss/10 text-krypt-loss',
            )}>
              {backend.authOk ? <Wifi className="h-5 w-5" /> : <WifiOff className="h-5 w-5" />}
            </div>
            <div className="flex-1">
              <div className="text-sm text-white">
                {backend.authOk ? 'Connected · Polygon mainnet' : 'Not connected'}
              </div>
              <div className="mt-0.5 text-xs text-krypt-muted">
                {cred?.address
                  ? <>Wallet <span className="font-mono text-white">{cred.address}</span></>
                  : 'The bot signs CLOB orders locally with your wallet key. Connect a funded wallet to trade.'}
              </div>
            </div>
          </div>
        </Card>
      </Section>

      <WalletSlot
        status={cred}
        onSaved={async () => { await reload(); await refresh.credentials(); await refresh.account(); await refresh.backend(); }}
      />

      <Section title="Security notes">
        <Card>
          <ul className="list-disc space-y-1.5 pl-5 text-xs text-krypt-muted">
            <li>Your private key is written to <span className="font-mono text-white">%APPDATA%/Krypt PolyBot/credentials/wallet.mainnet.key</span>, encrypted at rest with the Windows user keystore (DPAPI).</li>
            <li>The Python backend derives Polymarket CLOB API credentials from your key and signs every order locally (EIP-712). Nothing is sent anywhere but Polymarket.</li>
            <li>Use a dedicated trading wallet funded only with what you intend to trade. Anyone with this key controls those funds.</li>
            <li>Click &quot;Delete&quot; before uninstalling if you want the key gone.</li>
          </ul>
        </Card>
      </Section>
    </Page>
  );
}

function ReferralBanner() {
  return (
    <div className="mb-4 flex flex-col items-start gap-3 rounded-xl border border-krypt-purple/40 bg-gradient-to-r from-krypt-indigo/10 via-krypt-purple/10 to-krypt-pink/10 p-4 md:flex-row md:items-center">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-krypt-glow shadow-krypt-soft">
        <Gift className="h-5 w-5 text-white" />
      </div>
      <div className="flex-1 text-sm">
        <div className="font-medium text-white">No Polymarket account yet?</div>
        <div className="mt-0.5 text-xs text-krypt-muted">
          Sign up with our link — deposit $20 and place a first trade for up to $50 in trading
          credits (Polymarket&apos;s current new-user offer). Then fund the wallet with USDC and
          export its private key to connect it here.
        </div>
      </div>
      <button
        onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
        className="krypt-btn-primary"
      >
        <Gift className="h-4 w-4" /> Sign up <ExternalLink className="h-3 w-3" />
      </button>
    </div>
  );
}

interface SlotProps {
  status?: CredentialsState;
  onSaved: () => Promise<void>;
}

function WalletSlot({ status, onSaved }: SlotProps) {
  const toast = useToast();
  const [privateKey, setPrivateKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [funder, setFunder] = useState('');
  useEffect(() => { setFunder(status?.funder || ''); }, [status?.funder]);

  const has = !!status?.hasWalletKey;

  const reportTest = (data?: {
    balanceUsd?: number; ready?: boolean; issues?: string[];
  }): void => {
    const balStr = `$${(data?.balanceUsd ?? 0).toFixed(2)}`;
    const issues = data?.issues ?? [];
    if (data?.ready === false || issues.length) {
      const detail = issues[0]
        || ((data?.balanceUsd ?? 0) <= 0 ? 'wallet has no USDC balance' : 'not ready to trade');
      toast.warn(`Connected · balance ${balStr} — but not ready to trade yet: ${detail}`);
    } else {
      toast.success(`Connected · balance ${balStr} · ready to trade`);
    }
  };

  const save = async (): Promise<void> => {
    const pk = privateKey.trim();
    const fund = funder.trim();
    if (fund && !/^0x[0-9a-fA-F]{40}$/.test(fund)) {
      toast.error('Deposit wallet address must be a 0x… 40-hex address (or leave it blank for a raw wallet).');
      return;
    }

    if (!pk && has) {
      if (fund === (status?.funder || '')) {
        toast.error('Nothing to update — paste a new private key to replace the wallet, change the deposit wallet, or click Delete to remove it.');
        return;
      }
      setBusy(true);
      try {
        const r = await window.krypt.credentials.save({
          funder: fund || undefined,
          signatureType: fund ? 3 : 0,
          env: 'mainnet',
        });
        if (!r.ok) { toast.error(r.message || 'Update failed'); return; }
        toast.success(fund ? 'Deposit wallet updated. Verifying…' : 'Deposit wallet cleared. Verifying…');
        const t = await window.krypt.credentials.test('mainnet');
        if (t.ok) reportTest(t.data);
        else toast.error(t.message || 'Could not connect to Polymarket');
        await onSaved();
      } finally {
        setBusy(false);
      }
      return;
    }

    const hex = pk.replace(/^0x/, '');
    if (hex.length !== 64 || !/^[0-9a-fA-F]+$/.test(hex)) {
      toast.error('Paste a 64-character hex private key (optionally 0x-prefixed).');
      return;
    }
    setBusy(true);
    try {
      const r = await window.krypt.credentials.save({
        privateKey: pk,
        funder: fund || undefined,
        signatureType: fund ? 3 : 0,
        env: 'mainnet',
      });
      if (!r.ok) { toast.error(r.message || 'Save failed'); return; }
      toast.success('Wallet saved. Verifying…');
      const t = await window.krypt.credentials.test('mainnet');
      if (t.ok) reportTest(t.data);
      else toast.error(t.message || 'Could not connect to Polymarket');
      setPrivateKey('');
      await onSaved();
    } finally {
      setBusy(false);
    }
  };

  const test = async (): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.credentials.test('mainnet');
      if (r.ok) reportTest(r.data);
      else toast.error(r.message || 'Test failed');
    } finally {
      setBusy(false);
    }
  };

  const clear = async (): Promise<void> => {
    if (!window.confirm('Delete the saved wallet key from disk?')) return;
    setBusy(true);
    try {
      const r = await window.krypt.credentials.clear('mainnet');
      if (r.ok) { toast.success('Wallet removed'); await onSaved(); }
      else toast.error(r.message || 'Failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Wallet key">
      <Card>
        <div className="mb-3 flex items-center gap-2">
          <span className="inline-flex items-center gap-1 rounded-md border border-krypt-purple/30 bg-krypt-purple/5 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-purple">
            <Wallet className="h-3 w-3" /> Polygon
          </span>
          <div className="text-sm font-semibold text-white">Trading wallet</div>
          <span className={cls(
            'ml-auto rounded-md px-2 py-0.5 text-[10px] uppercase tracking-wider',
            has
              ? 'border border-krypt-win/30 bg-krypt-win/10 text-krypt-win'
              : 'border border-krypt-border bg-krypt-surface2 text-krypt-muted',
          )}>
            {has ? 'connected' : 'empty'}
          </span>
        </div>

        {has && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-krypt-border bg-krypt-surface2 px-3 py-2 text-xs text-krypt-muted">
            <KeyRound className="h-3.5 w-3.5 text-krypt-win" />
            <div className="flex-1">
              <div>
                Signer <span className="font-mono text-white">{status?.addressPreview || '—'}</span>
                <span className="mx-2 text-krypt-dim">·</span>
                API creds {status?.hasApiCreds ? <span className="text-krypt-win">derived</span> : <span className="text-krypt-warn">derive on connect</span>}
              </div>
              {status?.funder && (
                <div className="mt-0.5">
                  Deposit wallet{' '}
                  <span className="font-mono text-white">{status.funder.slice(0, 6)}…{status.funder.slice(-4)}</span>
                  <span className="mx-2 text-krypt-dim">·</span>
                  <span className="text-krypt-win">{SIGNATURE_TYPE_LABELS[status.signatureType ?? 0] ?? `type ${status.signatureType}`}</span>
                  <span className="mx-2 text-krypt-dim">·</span>
                  <span className="text-krypt-dim">detected from the chain</span>
                </div>
              )}
              {status?.walletMode === 'error' && (
                <div className="mt-0.5 text-krypt-loss">
                  Deposit-wallet setting unreadable: {status.metaError || 'unknown error'}. Re-save your
                  deposit wallet address below.
                </div>
              )}
              <div className="text-[10px] text-krypt-dim">
                Paste a new key below to replace, or click Delete to remove.
              </div>
            </div>
          </div>
        )}

        {has && status?.keyStoredUnencrypted && (
          <div className="mb-3 flex items-start gap-2 rounded-lg border border-krypt-loss/50 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-semibold">Private key stored unencrypted</div>
              <div className="mt-0.5 text-krypt-muted">
                No OS keystore (Windows DPAPI / macOS Keychain / Secret Service) was
                available, so your wallet key is saved in plaintext on this machine.
                Anyone with access to this device — or to a backup/cloud-sync of it —
                could take your funds. Enable a system keychain and re-save the key,
                or use a small dedicated wallet.
              </div>
            </div>
          </div>
        )}

        <label className="krypt-label mt-1 flex items-center justify-between">
          Wallet private key (0x-hex)
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="text-xs text-krypt-muted hover:text-white"
          >
            {showKey ? <><EyeOff className="mr-1 inline h-3 w-3" />hide</> : <><Eye className="mr-1 inline h-3 w-3" />show</>}
          </button>
        </label>
        <input
          type={showKey ? 'text' : 'password'}
          className="krypt-input font-mono"
          placeholder={has ? 'paste a new key to replace the connected wallet' : '0x' + 'x'.repeat(64)}
          value={privateKey}
          onChange={(e) => setPrivateKey(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <p className="krypt-help">
          <strong className="text-white">Polymarket account (email/Google login):</strong> export your
          key on Polymarket (Settings → Export private key) and add your deposit-wallet address below.
          <br />
          A deposit wallet is <strong className="text-white">required</strong>. Polymarket rejects orders
          signed by a bare wallet address, so the bot cannot trade without one.
        </p>

        <div className="mt-4 rounded-lg border border-krypt-purple/40 bg-krypt-purple/5 p-3">
          <label className="krypt-label flex flex-wrap items-center gap-2">
            Deposit wallet address
            <span className="rounded bg-krypt-purple/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-purple">
              needed for Polymarket logins
            </span>
          </label>
          <input
            type="text"
            className="krypt-input font-mono"
            placeholder="0x… your Polymarket deposit wallet"
            value={funder}
            onChange={(e) => setFunder(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <p className="krypt-help">
            <strong className="text-white">This is where your Polymarket balance lives.</strong> Copy the
            Polygon <span className="font-mono">0x…</span> address from your Polymarket profile / deposit
            screen (not the Solana one) and paste it here — without it the bot reads your bare signer
            wallet and shows a <span className="font-mono">$0.00</span> balance. The signing scheme is
            detected from the chain automatically (POLY_1271 for email/Google accounts, POLY_PROXY or
            Gnosis Safe for older ones). If orders are ever rejected with{' '}
            <span className="font-mono">signer ≠ api key</span>, click Test to re-detect it.
          </p>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button onClick={save} disabled={busy} className="krypt-btn-primary">
            <Save className="h-4 w-4" /> Save &amp; connect
          </button>
          <button onClick={test} disabled={busy || !has} className="krypt-btn-default">
            <ShieldCheck className="h-4 w-4" /> Test
          </button>
          {has && (
            <button onClick={clear} disabled={busy} className="krypt-btn-danger ml-auto">
              <Trash2 className="h-4 w-4" /> Delete
            </button>
          )}
        </div>
      </Card>
    </Section>
  );
}
