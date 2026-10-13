import { useCallback, useEffect, useState } from 'react';
import { FlaskConical, KeyRound, Loader2, RotateCcw, ShieldCheck, Wallet, X } from 'lucide-react';
import type { PaperStatus } from '@shared/market';
import type { PageId } from '../App';
import { Card, ConfirmDialog, DialogShell, NumberInput } from './common';
import { StepRow } from './GoLivePanel';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { cls, fmtPct, fmtUsd } from '../utils/format';
import { isLive } from '../utils/account';
import {
  accountGoLiveChecklist, accountGoLivePatch, backToPaperAccountPatch, liveAgents, liveSwitchLines,
  walletFingerprint, type KeyVerify,
} from '../utils/goLive';
import { userMessage } from '../utils/errors';

export function AccountModePanel({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { config, refresh } = useApp();
  const toast = useToast();
  const [paper, setPaper] = useState<PaperStatus | null>(null);
  const [paperLoaded, setPaperLoaded] = useState(false);
  const [goLiveOpen, setGoLiveOpen] = useState(false);
  const [askReset, setAskReset] = useState(false);
  const [resetting, setResetting] = useState(false);
  const toPaper = useBackToPaper();
  const busy = resetting || toPaper.busy;
  const live = isLive(config);

  const loadPaper = useCallback(async (): Promise<void> => {
    try {
      setPaper(await window.krypt.paper.status());
    } catch {
      setPaper(null);
    } finally {
      setPaperLoaded(true);
    }
  }, []);

  useEffect(() => { void loadPaper(); }, [loadPaper, config?.accountMode, config?.terminalPaperBankrollUsd]);
  useEffect(() => window.krypt.app.onDataReset(() => { void loadPaper(); }), [loadPaper]);

  const resetPaper = async (): Promise<void> => {
    setAskReset(false);
    setResetting(true);
    try {
      const r = await window.krypt.paper.reset();
      if (r.ok) toast.success(r.message || 'Paper account reset.');
      else toast.error(r.message || 'Reset failed.');
      await Promise.allSettled([refresh.account(), refresh.positions(), loadPaper()]);
    } finally {
      setResetting(false);
    }
  };

  if (!config) return null;
  const bankroll = config.terminalPaperBankrollUsd ?? 1000;

  return (
    <Card>
      <div className="grid gap-4 md:grid-cols-2" data-testid="account-mode-panel" data-tour="account-mode" data-mode={live ? 'live' : 'paper'}>
        <div>
          <label className="krypt-label">Account mode</label>
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="account-mode-paper"
              disabled={busy}
              onClick={() => { if (live) void toPaper.ask(); }}
              className={cls(
                'flex flex-1 items-center justify-center gap-1.5 rounded-md border px-3 py-2 text-sm transition-colors',
                !live
                  ? 'border-krypt-purple bg-krypt-purple/10 text-white'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
              )}
            >
              <FlaskConical className="h-4 w-4" /> Paper
            </button>
            <button
              type="button"
              data-testid="account-mode-live"
              disabled={busy}
              onClick={() => { if (!live) setGoLiveOpen(true); }}
              className={cls(
                'flex flex-1 items-center justify-center gap-1.5 rounded-md border px-3 py-2 text-sm transition-colors',
                live
                  ? 'border-krypt-loss bg-krypt-loss/10 text-white'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
              )}
            >
              <ShieldCheck className="h-4 w-4" /> {live ? 'Live' : 'Go live…'}
            </button>
          </div>
          <p className="krypt-help mt-2 text-xs leading-relaxed text-krypt-muted">
            {live
              ? 'LIVE: orders spend the real USDC in your wallet. Each engine still has its own on/off switch, and the AI agents their own trade mode — those can only narrow this one. Paper stops every real order at once (and pauses the stops and exits that manage real positions).'
              : 'PAPER: Polymarket’s real order books, imaginary money, no wallet needed. Your orders from the terminal and the phone, standing stops and AI agents all trade the paper book, and the engines (main, crypto, copy trading, perps, live scripts) send nothing — their switches are kept, they simply wait for Live.'}
          </p>
        </div>

        <div className="space-y-2">
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <label className="krypt-label">Paper starting balance</label>
              <NumberInput
                prefix="$"
                min={1}
                max={10_000_000}
                value={bankroll}
                onChange={(v) => void window.krypt.config.update({ terminalPaperBankrollUsd: v }).then(() => refresh.state())}
              />
            </div>
            <button
              type="button"
              className="krypt-btn-default"
              disabled={busy}
              onClick={() => setAskReset(true)}
              data-testid="paper-reset"
            >
              <RotateCcw className="h-4 w-4" /> Reset paper
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-krypt-muted" data-testid="paper-cash">
            <Wallet className="h-3.5 w-3.5" />
            {paper ? (
              <>
                <span>Paper cash <span className="font-mono text-white">{fmtUsd(paper.cashUsd)}</span></span>
                <span>· equity <span className="font-mono text-white">{fmtUsd(paper.equityUsd)}</span></span>
                {paper.returnPct !== undefined && paper.returnPct !== null && (
                  <span>· {fmtPct(paper.returnPct, 2)}</span>
                )}
                <span>· {paper.openPositions} open</span>
              </>
            ) : (
              <span className="text-krypt-dim" data-testid="paper-unavailable">
                {paperLoaded
                  ? 'The engine can’t report the paper account yet (stopped, or an older build). Track → Paper shows it.'
                  : 'Reading the paper account…'}
              </span>
            )}
          </div>
          <p className="text-xs leading-relaxed text-krypt-dim">
            Fills are walked out of the REAL order book at the price you would actually have got,
            with Polymarket&apos;s real fees, and never mixed into your real P&amp;L. Changing the
            starting balance re-bases the return figure; it never alters a simulated fill. Reset
            empties the paper book back to it. Live is never touched. AI agents have their own
            paper accounts (AI Agents page).
          </p>
        </div>
      </div>

      <ConfirmDialog
        open={askReset}
        title="Reset the paper account?"
        danger
        confirmLabel="Reset paper"
        onClose={() => setAskReset(false)}
        onConfirm={() => void resetPaper()}
        body={
          <p>
            Every paper position, resting paper order and paper fill of YOUR paper account is
            deleted, and paper cash goes back to {fmtUsd(bankroll)}. Your agents&apos; own paper
            accounts, your wallet and its history are not touched. This cannot be undone.
          </p>
        }
      />

      {toPaper.dialog}

      {goLiveOpen && <GoLiveAccountDialog onClose={() => setGoLiveOpen(false)} onNav={onNav} />}
    </Card>
  );
}

export function useBackToPaper(): { ask: () => Promise<void>; busy: boolean; dialog: JSX.Element } {
  const { refresh, positions } = useApp();
  const toast = useToast();
  const [askPaper, setAskPaper] = useState<{ open: number; rules: number | null } | null>(null);
  const [busy, setBusy] = useState(false);

  const ask = async (): Promise<void> => {
    const open = positions.filter((p) => !p.resolved
      && (p.status === 'filled' || p.status === 'partial' || p.status === 'submitted')).length;
    let rules: number | null = null;
    try {
      const r = await window.krypt.terminal.standing({ action: 'list' });
      rules = r.error ? null : r.instructions.filter((i) => i.armed).length;
    } catch {
      rules = null;
    }
    setAskPaper({ open, rules });
  };

  const backToPaper = async (): Promise<void> => {
    setAskPaper(null);
    setBusy(true);
    try {
      await window.krypt.config.update(backToPaperAccountPatch());
      await Promise.allSettled([refresh.state(), refresh.account(), refresh.backend(), refresh.positions()]);
      toast.success('Back on Paper. Nothing sends a real order now; real positions stay open on Polymarket.');
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const dialog = (
    <ConfirmDialog
      open={askPaper !== null}
      title="Switch back to Paper?"
      confirmLabel="Switch to Paper"
      onClose={() => setAskPaper(null)}
      onConfirm={() => void backToPaper()}
      body={
        <div className="space-y-2" data-testid="back-to-paper-confirm">
          <p>No real order can be sent from Paper — not an entry, and not an exit.</p>
          <p className="text-krypt-warn">
            That includes the exits guarding your REAL positions: standing stop-losses and
            take-profits and the engines&apos; own exits pause until you are Live again. The
            positions themselves stay open on Polymarket.
          </p>
          <p>
            Open positions the engines track: {askPaper?.open ?? '—'} · armed standing
            instructions: {askPaper?.rules ?? '—'}.
          </p>
        </div>
      }
    />
  );

  return { ask, busy, dialog };
}

export function GoLiveAccountDialog({ onClose, onNav }: { onClose: () => void; onNav?: (p: PageId) => void }) {
  const { config, credentialsAll, account, refresh } = useApp();
  const toast = useToast();
  const [verify, setVerify] = useState<KeyVerify | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [ack, setAck] = useState(false);
  const [applying, setApplying] = useState(false);
  const wallet = credentialsAll?.mainnet;
  const armed = liveSwitchLines(config);
  const list = accountGoLiveChecklist({ config, creds: credentialsAll, verify, armed, ack, region: account?.tradingRegion });

  const runVerify = async (): Promise<void> => {
    const fp = walletFingerprint(credentialsAll);
    if (!fp) return;
    setVerifying(true);
    try {
      const r = await window.krypt.credentials.test();
      setVerify({
        ok: r.ok,
        message: r.ok ? 'Connected to Polymarket' : (r.message || 'the wallet could not be verified'),
        fingerprint: fp,
        balanceUsd: r.ok ? r.data?.balanceUsd ?? null : null,
        ready: r.ok ? r.data?.ready : undefined,
        issues: r.ok ? r.data?.issues : undefined,
        fatalIssues: r.ok ? r.data?.fatalIssues : undefined,
        region: r.ok ? r.data?.region ?? null : null,
      });
    } catch (e) {
      setVerify({ ok: false, message: userMessage(e), fingerprint: fp });
    } finally {
      setVerifying(false);
    }
  };

  const apply = async (): Promise<void> => {
    if (!list.ready) return;
    setApplying(true);
    try {
      await window.krypt.config.update(accountGoLivePatch());
      await Promise.allSettled([
        refresh.state(), refresh.credentials(), refresh.account(), refresh.backend(), refresh.positions(),
      ]);
      toast.warn('The app is LIVE: orders now spend the real USDC in your wallet.');
      onClose();
    } catch (e) {
      toast.error(`Go live failed: ${userMessage(e)}`);
    } finally {
      setApplying(false);
    }
  };

  return (
    <DialogShell onClose={onClose} maxWidth="max-w-xl">
      <div className="max-h-[82vh] overflow-y-auto pr-1" data-testid="golive-account">
        <div className="mb-3 flex items-start gap-3">
          <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-krypt-loss" />
          <div className="flex-1">
            <h3 className="text-sm font-semibold text-white">Go live — trade real money</h3>
            <p className="mt-0.5 text-[11px] text-krypt-muted">
              Switches the whole app from Paper to your real Polymarket wallet. Nothing changes until
              the last button, and Paper is one click away afterwards.
            </p>
          </div>
          <button onClick={onClose} className="krypt-btn-ghost p-1" aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-3">
          {list.steps.map((step, i) => (
            <StepRow key={step.id} n={i + 1} step={step}>
              {step.id === 'keys' && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {wallet?.hasWalletKey ? (
                    <button
                      onClick={() => void runVerify()}
                      disabled={verifying}
                      className="krypt-btn-default text-xs"
                      data-testid="golive-account-verify"
                    >
                      {verifying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                      Test wallet (derives credentials, reads balance)
                    </button>
                  ) : (
                    <button
                      onClick={() => { onClose(); onNav?.('api'); }}
                      className="krypt-btn-default text-xs"
                      data-testid="golive-account-addkey"
                    >
                      <KeyRound className="h-3.5 w-3.5" /> Connect a wallet
                    </button>
                  )}
                </div>
              )}
              {step.id === 'engines' && armed.length > 0 && (
                <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[11px] text-krypt-loss">
                  {armed.map((a) => <li key={a}>{a}</li>)}
                </ul>
              )}
              {step.id === 'ack' && (
                <label className="mt-2 flex items-start gap-3 rounded-lg border border-krypt-loss/40 bg-krypt-loss/5 p-3">
                  <input
                    type="checkbox"
                    checked={ack}
                    onChange={(e) => setAck(e.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-krypt-loss"
                    data-testid="golive-account-ack"
                  />
                  <span className="text-xs text-white/90">
                    I understand that in Live every order — mine, the engines&apos;, my agents&apos; —
                    spends the real USDC in my wallet, and that past paper results promise nothing.
                  </span>
                </label>
              )}
            </StepRow>
          ))}
          {liveAgents(config).length === 0 && (
            <div
              className="flex flex-wrap items-center gap-2 rounded-lg border border-krypt-warn/40 bg-krypt-warn/5 px-3 py-2 text-[11px] text-krypt-warn"
              data-testid="golive-account-agents-paper"
            >
              <span className="flex-1">
                Your AI agents stay on paper — go live for them on the AI Agents page.
              </span>
              {onNav && (
                <button
                  onClick={() => { onClose(); onNav('agents'); }}
                  className="krypt-btn-default text-xs"
                  data-testid="golive-account-agents-link"
                >
                  Open AI Agents
                </button>
              )}
            </div>
          )}
          <div className="flex items-center justify-end gap-2 pt-1">
            {!list.ready && (
              <span className="mr-auto text-[11px] text-krypt-loss">
                {list.steps.filter((s) => s.state === 'block').map((s) => s.title).join(', ')} still needs you.
              </span>
            )}
            <button onClick={onClose} className="krypt-btn-default text-xs">Stay on Paper</button>
            <button
              onClick={() => void apply()}
              disabled={!list.ready || applying}
              className="krypt-btn-danger text-xs"
              data-testid="golive-account-apply"
            >
              {applying && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Switch to Live
            </button>
          </div>
        </div>
      </div>
    </DialogShell>
  );
}
