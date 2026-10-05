import { useState } from 'react';
import { ArrowRight, ExternalLink, Gift, ShieldAlert, Youtube } from 'lucide-react';
import { useToast } from '../state/ToastProvider';
import { POLYMARKET_REFERRAL_URL, KRYPT_YOUTUBE_GUIDE } from '../utils/links';

export function OnboardingModal({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [accepted, setAccepted] = useState(false);
  const [eligible, setEligible] = useState(false);
  const toast = useToast();

  const finish = async (): Promise<void> => {
    if (!accepted || !eligible) {
      toast.warn('Please tick both boxes to continue');
      return;
    }
    await window.krypt.state.acceptDisclaimer();
    toast.success('Welcome to Krypt PolyBot');
    onDone();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 backdrop-blur">
      <div className="w-[640px] max-w-[92vw] overflow-hidden rounded-2xl border border-krypt-borderHi bg-krypt-surface shadow-krypt-strong">
        <div className="bg-krypt-glow p-1">
          <div className="rounded-t-xl bg-krypt-surface px-8 py-6 text-center">
            <div className="mx-auto mb-3 grid h-16 w-16 place-items-center rounded-2xl bg-krypt-glow shadow-krypt-strong">
              <span className="font-pixel text-xs">K</span>
            </div>
            <h2 className="font-pixel text-base tracking-wider">KRYPT POLYBOT</h2>
            <p className="mt-1 text-xs text-krypt-muted">
              The free Polymarket auto-trader.
            </p>
          </div>
        </div>

        <div className="px-8 py-6">
          {step === 0 && (
            <div className="space-y-4 text-sm text-white/90">
              <p>
                Krypt PolyBot watches Polymarket's whale flow and momentum signals, scores
                them, and (optionally) auto-places contrarian bets on the highest-edge
                setups. Everything runs locally on your machine — your wallet key never
                leaves it.
              </p>
              <ul className="grid grid-cols-2 gap-3 text-xs">
                <Feature title="Whale tracker" body="Sub-$2.5k+ taker orders, scored." />
                <Feature title="Momentum scanner" body="Trade-cluster contrarian fades." />
                <Feature title="Auto-trader" body="Limit-cross orders, sized 2-6%." />
                <Feature title="Daily P&L gate" body="Stop-loss / take-profit safety." />
                <Feature title="Local SQLite" body="Full trade history, exportable." />
                <Feature title="Tray + autostart" body="Runs in the background between sessions." />
              </ul>

              <div className="rounded-lg border border-krypt-purple/40 bg-gradient-to-r from-krypt-indigo/10 via-krypt-purple/10 to-krypt-pink/10 p-3">
                <div className="flex items-center gap-2 text-sm font-semibold text-white">
                  <Gift className="h-4 w-4 text-krypt-purple" /> Start here — make your Polymarket account with our link
                </div>
                <p className="mt-1 text-xs text-krypt-muted">
                  Krypt PolyBot is set up to run on a Polymarket account created through our
                  link — <span className="text-white">use it so everything connects</span> (it&apos;s
                  our referral, at no cost to you). Then deposit <span className="text-white">$20</span>{' '}
                  and make your first trade to get <span className="text-white">up to $50 in trading
                  credits</span> — Polymarket&apos;s current new-user offer. New here? The video guide
                  walks through the whole setup.
                </p>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
                    className="flex items-center justify-center gap-2 rounded-lg bg-krypt-purple/90 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-krypt-purple"
                  >
                    <Gift className="h-4 w-4" /> Sign up on Polymarket
                  </button>
                  <button
                    type="button"
                    onClick={() => void window.krypt.app.openExternal(KRYPT_YOUTUBE_GUIDE)}
                    className="flex items-center justify-center gap-2 rounded-lg border border-krypt-border bg-krypt-surface2 px-3 py-2 text-xs font-semibold text-white transition-colors hover:border-krypt-purple"
                  >
                    <Youtube className="h-4 w-4 text-krypt-loss" /> Watch the guide
                  </button>
                </div>
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="space-y-4 text-sm text-white/90">
              <h3 className="text-base font-semibold">Quick start</h3>

              <div className="rounded-lg border border-krypt-purple/40 bg-gradient-to-r from-krypt-indigo/10 via-krypt-purple/10 to-krypt-pink/10 p-3">
                <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
                  <Gift className="h-4 w-4 text-krypt-purple" /> Step 1 — Create your Polymarket account (required)
                </div>
                <p className="text-xs text-krypt-muted">
                  The bot trades through a Polymarket <span className="text-white">account wallet</span>, so you
                  need an account — and you <span className="text-white">must create it with the button
                  below</span> for Krypt PolyBot to work. It&apos;s our referral link (supports the
                  tool at no cost to you). Then deposit <span className="text-white">$20</span> and
                  place your first trade to get <span className="text-white">up to $50 in trading
                  credits</span> — Polymarket&apos;s current new-user offer. Sign up with{' '}
                  <span className="text-white">Google/Gmail</span> — fastest.
                </p>
                <button
                  type="button"
                  onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
                  className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg bg-krypt-purple/90 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-krypt-purple"
                >
                  <Gift className="h-4 w-4" /> Create my Polymarket account
                  <ExternalLink className="h-4 w-4 opacity-80" />
                </button>
                <button
                  type="button"
                  onClick={() => void window.krypt.app.openExternal(KRYPT_YOUTUBE_GUIDE)}
                  className="mt-2 flex items-center gap-1.5 text-[11px] text-krypt-muted transition-colors hover:text-white"
                >
                  <Youtube className="h-3.5 w-3.5 text-krypt-loss" /> Watch the full setup video guide
                  <ExternalLink className="h-3 w-3 opacity-70" />
                </button>
                <p className="mt-2 text-[11px] text-krypt-dim">
                  A fresh account from this link keeps you supported (our referral). Any bonus is
                  Polymarket&apos;s — trading credit, not instant cash; the amount, minimum deposit,
                  and terms are set by Polymarket and can change.
                </p>
              </div>

              <ol className="list-decimal space-y-2 pl-4 text-sm text-krypt-muted" start={2}>
                <li>Deposit funds on Polymarket (USDC becomes pUSD in your account wallet). Deposit
                  at least <span className="text-white">$20</span> and place a first trade to claim the
                  new-user trading-credit bonus.</li>
                <li>Turn on <span className="text-white">auto-redeem</span> (Polymarket → Settings → Trading → auto-redeem wins) so winnings convert to cash automatically.</li>
                <li>Export your <span className="text-white">private key</span> and copy your <span className="text-white">deposit-wallet address</span> (both from Polymarket).</li>
                <li>Paste both into the Wallet page → Save &amp; connect → Test (shows your balance).</li>
                <li>Pick a strategy (<span className="text-white">Krypt Balanced</span>), start with a small balance, then scale up.</li>
              </ol>
              <div className="rounded-lg border border-krypt-warn/40 bg-krypt-warn/5 p-3 text-xs text-krypt-warn">
                <strong>Every engine places REAL orders</strong> with your connected
                wallet. They all start <strong>OFF</strong>, and each has its own switch
                on its own page: <strong>Main Engine</strong>, <strong>15m Crypto</strong>,{' '}
                <strong>Copy Trading</strong> and <strong>Scripts</strong>. Turning one on
                never turns on another. Leave them off unless you mean it.
                <div className="mt-2 text-krypt-muted">
                  Want to practise first? Turn on <strong>paper mode</strong> in Settings
                  and trade by hand from a market page — simulated fills against the real
                  order book, no wallet needed. It covers hand-placed orders only, not
                  the engines.
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-4 text-sm">
              <div className="flex items-start gap-3 rounded-lg border border-krypt-loss/30 bg-krypt-loss/5 p-3 text-krypt-loss">
                <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
                <div className="text-xs leading-relaxed">
                  <strong>Disclaimer.</strong> Krypt PolyBot is provided as-is, free.
                  Auto-trading involves risk; all P&amp;L is your own. We make no
                  guarantee of profitability. You are solely responsible for
                  compliance with Polymarket&apos;s terms of service and applicable
                  law in your jurisdiction. The engines trade real money the moment
                  you enable them — start with a small balance you can afford to lose.
                </div>
              </div>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={accepted}
                  onChange={(e) => setAccepted(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                />
                <span className="text-xs text-white/90">
                  I&apos;ve read the disclaimer and accept the risks of auto-trading.
                </span>
              </label>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={eligible}
                  onChange={(e) => setEligible(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                />
                <span className="text-xs text-white/90">
                  I confirm I am legally eligible to use Polymarket in my jurisdiction
                  and accept full responsibility for my trading.
                </span>
              </label>
              <button
                type="button"
                onClick={() => void window.krypt.app.openExternal(POLYMARKET_REFERRAL_URL)}
                className="flex w-full items-center gap-3 rounded-lg border border-krypt-purple/40 bg-gradient-to-r from-krypt-indigo/10 via-krypt-purple/10 to-krypt-pink/10 p-3 text-left transition-colors hover:border-krypt-purple"
              >
                <Gift className="h-5 w-5 shrink-0 text-krypt-purple" />
                <div className="flex-1 text-xs">
                  <div className="text-white">No Polymarket account yet?</div>
                  <div className="text-krypt-muted">
                    Sign up with our link, deposit $20, and make your first trade for{' '}
                    <span className="text-white">up to $50 in trading credits</span> (Polymarket&apos;s offer).
                  </div>
                </div>
                <ExternalLink className="h-4 w-4 text-krypt-muted" />
              </button>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-krypt-border bg-krypt-surface2/50 px-8 py-4">
          <div className="flex gap-1">
            {[0, 1, 2].map((i) => (
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
          <div className="flex items-center gap-2">
            {step > 0 && (
              <button onClick={() => setStep((s) => s - 1)} className="krypt-btn-ghost">
                Back
              </button>
            )}
            {step < 2 ? (
              <button
                onClick={() => setStep((s) => s + 1)}
                className="krypt-btn-primary"
              >
                Continue <ArrowRight className="h-4 w-4" />
              </button>
            ) : (
              <button onClick={finish} className="krypt-btn-primary">
                Get started <ArrowRight className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      </div>
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
