import { useEffect, useState } from 'react';
import {
  ArrowRight, ExternalLink, FlaskConical, Gift, ShieldAlert, Wallet, X,
} from 'lucide-react';
import type { PageId } from '../App';
import { ConnectAgent } from '../components/ConnectAgent';
import { AutopilotQuickstart } from '../components/AutopilotQuickstart';
import { Badge, ChoiceCard } from '../components/SetupSteps';
import { useToast } from '../state/ToastProvider';
import { useApp } from '../state/AppStateProvider';
import { KryptSprite } from '../components/KryptSprite';
import { POLYMARKET_REFERRAL_URL } from '../utils/links';
import { GuideVideoCard } from '../components/GuideVideo';
import { GlassPanel } from '../components/glass/GlassPanel';
import { GlassButton } from '../components/glass/GlassButton';
import { GLASS_TINTS } from '../components/glass/glassPresets';

type Step = 'welcome' | 'disclaimer' | 'agent' | 'start' | 'autopilot';

export function OnboardingModal({ onDone, replay = false, updated = false, onClose }: {
  onDone: (page: PageId) => void;
  replay?: boolean;
  updated?: boolean;
  onClose?: () => void;
}) {
  const [step, setStep] = useState(0);
  const [accepted, setAccepted] = useState(false);
  const [eligible, setEligible] = useState(false);
  const [wantAutopilot, setWantAutopilot] = useState(false);
  const [walletNext, setWalletNext] = useState(false);
  const toast = useToast();
  const { state, config } = useApp();
  const alreadyAccepted = replay && !!state?.acceptedDisclaimer;
  const steps: Step[] = ['welcome', 'disclaimer', 'agent', 'start', ...(wantAutopilot ? ['autopilot' as const] : [])];
  const at = steps[step];
  const last = steps.length - 1;
  const next = (): void => setStep((s) => Math.min(s + 1, last));
  const back = (): void => setStep((s) => Math.max(0, s - 1));

  const acceptAndContinue = async (): Promise<void> => {
    if (alreadyAccepted) { next(); return; }
    if (!accepted || !eligible) {
      toast.warn('Please tick both boxes to continue');
      return;
    }
    await window.krypt.state.acceptDisclaimer();
    next();
  };

  const finish = (toWallet = walletNext): void => {
    if (!replay) toast.success('Welcome to Krypt PolyBot');
    onDone(toWallet ? 'api' : 'agents');
  };

  const advanceOrFinish = (toWallet = walletNext): void => {
    if (step >= last) finish(toWallet); else next();
  };
  const embedded = at === 'start' || at === 'autopilot';

  const startOnPaper = async (): Promise<void> => {
    setWalletNext(false);
    if (config?.accountMode === 'live') {
      await window.krypt.config.update({ accountMode: 'paper' });
      toast.success('Back on Paper. Nothing can send a real order now.');
    }
    advanceOrFinish(false);
  };

  const connectWallet = (): void => {
    setWalletNext(true);
    advanceOrFinish(true);
  };

  useEffect(() => {
    if (!replay || !onClose) return undefined;
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [replay, onClose]);

  const bankroll = config?.terminalPaperBankrollUsd ?? 1000;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/55">
      <GlassPanel
        preset="modal"
        tint={GLASS_TINTS.modal}
        display="flex"
        className="glass-pop relative max-h-[94vh] w-[680px] max-w-[94vw] flex-col overflow-hidden rounded-3xl"
        style={{ boxShadow: '0 0 80px -20px rgba(59,130,246,0.55), var(--glass-shadow)' }}
        role="dialog"
        aria-modal="true"
        aria-label={replay ? 'Onboarding (replay)' : 'Onboarding'}
      >
        {replay && onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close onboarding"
            title="Close (nothing changes)"
            className="absolute right-3 top-3 z-10 grid h-7 w-7 place-items-center rounded-lg text-krypt-muted transition-colors hover:bg-white/[0.08] hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        )}
        <div className="shrink-0 bg-gradient-to-r from-krypt-indigo/70 via-krypt-purple/70 to-krypt-pink/70 pb-px">
          {at === 'welcome' ? (
            <div className="bg-[linear-gradient(180deg,rgba(24,30,64,0.85),rgba(12,14,28,0.9))] px-8 py-6 text-center">
              <div className="relative mx-auto mb-3 grid h-20 w-20 place-items-center">
                <div aria-hidden className="absolute inset-0 rounded-full bg-[radial-gradient(circle_at_50%_40%,rgba(96,165,250,0.45),rgba(59,130,246,0.12)_55%,transparent_72%)]" />
                <div aria-hidden className="absolute inset-2 rounded-full border border-white/15 bg-white/[0.04] shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_10px_30px_-8px_rgba(59,130,246,0.7)]" />
                <KryptSprite size={56} className="relative" title="Bluey" />
              </div>
              <h2 className="font-pixel text-base tracking-wider">KRYPT POLYBOT</h2>
              <p className="mt-1 text-xs text-krypt-muted">
                Your AI agent on Polymarket: inside rails, scored against the market.
              </p>
              {replay && (
                <p className="mx-auto mt-2 w-fit rounded-full border border-krypt-purple/40 bg-krypt-purple/10 px-2.5 py-0.5 text-[10px] uppercase tracking-wider text-krypt-purple">
                  {updated ? 'New in this version' : 'Replay'} · nothing changes unless you act in a step
                </p>
              )}
              {updated && (
                <p className="mx-auto mt-2 max-w-md text-[11px] leading-relaxed text-krypt-warn" data-testid="onboarding-reconnect-note">
                  Using Krypt PolyBot from Claude Desktop, Cursor or Codex? The update closed their
                  connection. Fully quit and reopen them (Claude Desktop: right-click its tray icon → Quit)
                  so they reconnect.
                </p>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-3 bg-[linear-gradient(180deg,rgba(24,30,64,0.85),rgba(12,14,28,0.9))] px-8 py-3">
              <div className="h-[18px] w-[18px] rounded-md bg-gradient-to-br from-sky-400 via-blue-500 to-blue-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_0_14px_-2px_rgba(59,130,246,0.85)]" />
              <h2 className="font-pixel text-xs tracking-wider">KRYPT POLYBOT</h2>
              {replay && (
                <span className="rounded-full border border-krypt-purple/40 bg-krypt-purple/10 px-2 py-0.5 text-[9px] uppercase tracking-wider text-krypt-purple">
                  {updated ? 'New' : 'Replay'}
                </span>
              )}
              <span className={replay ? 'ml-auto mr-8 text-[11px] text-krypt-muted' : 'ml-auto text-[11px] text-krypt-muted'}>
                {at === 'agent' ? 'Your AI agent' : at === 'disclaimer' ? 'Before you trade'
                  : at === 'start' ? 'Paper or wallet' : 'Autopilot'}
              </span>
            </div>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
          {at === 'welcome' && (
            <div className="space-y-4 text-sm text-white/90">
              <div className="rounded-xl border border-krypt-purple/50 bg-gradient-to-r from-krypt-indigo/15 via-krypt-purple/15 to-krypt-pink/15 p-4 shadow-[0_0_28px_-10px_rgba(168,85,247,0.7)]">
                <div className="flex items-center gap-2 text-sm font-semibold text-white">
                  <Gift className="h-4 w-4 text-krypt-purple" /> Start here: make your Polymarket account with our link
                </div>
                <p className="mt-1.5 text-xs text-krypt-muted">
                  It&apos;s free, takes about two minutes, and costs you nothing extra.{' '}
                  <span className="text-white">Accounts made through our link are how Krypt PolyBot stays
                  free and keeps getting better — please use it.</span> Then deposit{' '}
                  <span className="text-white">$20</span> and make a first trade for{' '}
                  <span className="text-white">up to $50 in trading credits</span> (Polymarket&apos;s current
                  new-user offer). Paper mode needs no account at all.
                </p>
                <button
                  type="button"
                  onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
                  className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg bg-krypt-purple px-3 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-krypt-purple/80"
                >
                  <Gift className="h-4 w-4" /> Sign up on Polymarket
                </button>
              </div>
              <GuideVideoCard />
              <p>
                Connect the AI agent you already use —{' '}
                <span className="text-white">Claude Desktop, Claude Code, Cursor, Codex, Windsurf or
                VS Code</span> — and it can read every Polymarket market, commit to a forecast, and
                trade <span className="text-white">on paper</span> against the real order book. Every
                forecast is scored against the market price once the market resolves, so you find out
                whether it beats the market before a dollar is at risk.
              </p>
              <p className="text-krypt-muted">
                The <span className="text-white">terminal</span> (Discover, Market, Track) is the manual
                side, and the original <span className="text-white">engines</span> are still here. Everything
                runs locally on your machine — your wallet key never leaves it.
              </p>
              <ul className="grid grid-cols-2 gap-3 text-xs">
                <Feature title="AI agents (MCP)" body="Your agent, on a local server only it can reach." />
                <Feature title="Forecast first" body="No buy without a forecast that beats the price after fees." />
                <Feature title="Paper by default" body="Real books, imaginary money. Live is its own switch." />
                <Feature title="Forecast scoreboard" body="Brier vs the market, per agent." />
                <Feature title="Whale tracker" body="$2.5k+ taker orders, scored." />
                <Feature title="Momentum scanner" body="Trade-cluster contrarian fades." />
                <Feature title="Copy trading & Perps" body="Each its own engine and its own switch." />
                <Feature title="Strategy scripts" body="Write your own, with money rails." />
              </ul>

            </div>
          )}

          {at === 'disclaimer' && (
            <div className="space-y-4 text-sm">
              <div className="flex items-start gap-3 rounded-lg border border-krypt-loss/30 bg-krypt-loss/5 p-3 text-krypt-loss">
                <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
                <div className="text-xs leading-relaxed">
                  <strong>Disclaimer.</strong> Krypt PolyBot is provided as-is, free.
                  Auto-trading involves risk; all P&amp;L is your own. We make no
                  guarantee of profitability. You are solely responsible for
                  compliance with Polymarket&apos;s terms of service and applicable
                  law in your jurisdiction. An AI agent can be wrong with total
                  confidence. Once the app is Live, the engines and any agent you set
                  live trade real money — start with a small balance you can afford to lose.
                </div>
              </div>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={alreadyAccepted || accepted}
                  disabled={alreadyAccepted}
                  onChange={(e) => setAccepted(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                  data-testid="disclaimer-accept"
                />
                <span className="text-xs text-white/90">
                  I&apos;ve read the disclaimer and accept the risks of auto-trading.
                  {alreadyAccepted && (
                    <span className="mt-0.5 block text-krypt-muted" data-testid="disclaimer-accepted">
                      You accepted this when you set up the app. It stays accepted; it is here to re-read.
                    </span>
                  )}
                </span>
              </label>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={alreadyAccepted || eligible}
                  disabled={alreadyAccepted}
                  onChange={(e) => setEligible(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                  data-testid="disclaimer-eligible"
                />
                <span className="text-xs text-white/90">
                  I confirm I am legally eligible to use Polymarket in my jurisdiction
                  and accept full responsibility for my trading.
                </span>
              </label>
              <div className="rounded-lg border border-krypt-warn/40 bg-krypt-warn/5 p-3 text-xs text-krypt-warn">
                You start in <strong>PAPER</strong> — Polymarket&apos;s real order books, imaginary
                money, no wallet needed. The terminal, your phone and your AI agents trade paper, and
                the engines (Main Engine, Crypto, Copy Trading, Perps, live Scripts) send nothing. Real
                money needs a connected wallet AND switching the app to Live in Settings; even then each
                engine has its own switch, off by default, and turning one on never turns on another.
              </div>
              <button
                type="button"
                onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
                className="flex w-full items-center gap-3 rounded-lg border border-krypt-purple/40 bg-gradient-to-r from-krypt-indigo/10 via-krypt-purple/10 to-krypt-pink/10 p-3 text-left transition-colors hover:border-krypt-purple"
              >
                <Gift className="h-5 w-5 shrink-0 text-krypt-purple" />
                <div className="flex-1 text-xs">
                  <div className="text-white">No Polymarket account yet? Make it with our link</div>
                  <div className="text-krypt-muted">
                    Free, and it keeps Krypt PolyBot developed. Deposit $20 and make a first trade for{' '}
                    <span className="text-white">up to $50 in trading credits</span> (Polymarket&apos;s offer).
                  </div>
                </div>
                <ExternalLink className="h-4 w-4 text-krypt-muted" />
              </button>
            </div>
          )}

          {at === 'agent' && (
            <div className="space-y-3 text-sm text-white/90">
              <h3 className="text-base font-semibold">Connect your AI agent</h3>
              <p className="text-xs leading-relaxed text-white/80">
                Don&apos;t use Claude, Cursor or Codex? Pick{' '}
                <span className="text-white">In-app Autopilot</span>: nothing to install — the app runs
                the agent itself with an AI you choose (including free ones on your own computer).
              </p>
              <p className="text-xs leading-relaxed text-krypt-muted">
                Pick the one you use. Connecting it switches on the local agent server, and if agent
                trading is off it sets it to <span className="text-white">paper</span>: real order books,
                imaginary money. Nothing here can turn on live trading; that is its own deliberate
                switch on the AI Agents page. The client&apos;s config holds no token.
              </p>
              <ConnectAgent
                paperDefault
                autopilotSetup="callback"
                onAutopilot={() => { setWantAutopilot(true); next(); }}
              />
              <p className="text-[11px] text-krypt-dim">
                Then ask it something like: &ldquo;Use krypt-polybot. Check get_status, look through
                markets closing soon, read the rules, and record honest forecasts.&rdquo; Not using an AI
                agent? Skip this: nothing is switched on unless you connect one.
              </p>
            </div>
          )}

          {at === 'start' && (
            <div className="space-y-3 text-sm text-white/90" data-testid="onboarding-start">
              <h3 className="text-base font-semibold">How do you want to start?</h3>
              <ChoiceCard
                testId="start-paper-card" selected={!walletNext} onClick={() => setWalletNext(false)}
                title={<><FlaskConical className="mr-1 inline h-4 w-4" />Start on paper (no wallet needed)</>}
                badge={<Badge>Recommended</Badge>}
              >
                Polymarket&apos;s real markets and order books with imaginary money — $
                {bankroll.toLocaleString()} to start. The terminal, your phone and your AI agents all
                work, and none of them can spend real money.
              </ChoiceCard>
              <ChoiceCard
                testId="connect-wallet-card" selected={walletNext} onClick={() => setWalletNext(true)}
                title={<><Wallet className="mr-1 inline h-4 w-4" />I have a Polymarket account — connect my wallet</>}
              >
                Opens the Wallet page: export your private key and copy your deposit-wallet address
                from Polymarket, paste both, then Save &amp; connect → Test. Turn on auto-redeem in
                Polymarket (Settings → Trading) so winnings convert to cash. The app still starts on
                Paper; going Live is its own step in Settings → Paper | Live whenever you are ready.
              </ChoiceCard>
              <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                <button onClick={back} className="krypt-btn-ghost">Back</button>
                <div className="flex gap-2">
                  <button onClick={connectWallet} className="krypt-btn-default" data-testid="onboarding-connect-wallet">
                    Connect a wallet
                  </button>
                  <GlassButton variant="primary" onClick={() => void startOnPaper()} data-testid="onboarding-start-paper">
                    Start on paper <ArrowRight className="h-4 w-4" />
                  </GlassButton>
                </div>
              </div>
            </div>
          )}

          {at === 'autopilot' && (
            <AutopilotQuickstart
              onDone={() => finish()}
              onSkip={() => finish()}
              onBackOut={back}
            />
          )}
        </div>

        <div className="flex shrink-0 items-center justify-between border-t border-krypt-border bg-krypt-surface2/50 px-8 py-4">
          <div className="flex gap-1" aria-label={`Onboarding step ${step + 1} of ${steps.length}`}>
            {steps.map((_, i) => (
              <span
                key={i}
                className={
                  i === step
                    ? 'h-1.5 w-6 rounded-full bg-krypt-purple'
                    : 'h-1.5 w-1.5 rounded-full bg-krypt-border'
                }
              />
            ))}
          </div>
          {embedded ? (
            <span className="text-[11px] text-krypt-dim">
              {at === 'start' ? 'Paper or wallet' : 'Autopilot setup'}
            </span>
          ) : (
            <div className="flex items-center gap-2">
              {step > 0 && (
                <button onClick={back} className="krypt-btn-ghost">
                  Back
                </button>
              )}
              {at === 'agent' && (
                <button onClick={next} className="krypt-btn-ghost">
                  Skip
                </button>
              )}
              <GlassButton
                variant="primary"
                data-testid="onboarding-continue"
                onClick={() => { if (at === 'disclaimer') void acceptAndContinue(); else next(); }}
              >
                Continue <ArrowRight className="h-4 w-4" />
              </GlassButton>
            </div>
          )}
        </div>
      </GlassPanel>
    </div>
  );
}

function Feature({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
      <div className="text-xs font-semibold text-white">{title}</div>
      <div className="mt-0.5 text-[11px] text-krypt-muted">{body}</div>
    </div>
  );
}
