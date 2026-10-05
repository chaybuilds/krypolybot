import { useEffect, useRef, useState } from 'react';
import { RefreshCw, RotateCcw } from 'lucide-react';
import type { TraderConfig } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import type { AiProvider, AiStatus, TerminalHealth } from '@shared/market';
import { Card, Page, Section, Switch } from '../components/common';
import { CommitInput, Val } from '../components/terminal/TerminalBits';
import { cls } from '../utils/format';

export function SettingsPage() {
  const { config, refresh, state, backend } = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const restartBackend = async (): Promise<void> => {
    toast.info('Restarting backend — every engine stops and restarts…');
    await window.krypt.backend.restart();
    setTimeout(() => void refresh.backend(), 1000);
  };

  if (!config) return <Page title="Settings"><div className="text-krypt-muted">Loading…</div></Page>;

  const update = async <K extends keyof TraderConfig>(key: K, value: TraderConfig[K]): Promise<void> => {
    try {
      await window.krypt.config.update({ [key]: value } as Partial<TraderConfig>);
      await refresh.state();
    } catch (e: any) {
      toast.error(`${e?.message || e}`);
    }
  };

  return (
    <Page
      title="Settings"
      subtitle="App-level preferences: startup behavior, notifications, and data. Each trading engine has its own page and its own on/off switch — Main Engine (whales + momentum), Crypto, Copy Trading, and Scripts."
    >
      <Section
        title="Backend"
        description="The Python process every engine runs inside. Restarting it stops and restarts ALL of them — it is not scoped to any single engine, which is why it lives here rather than on an engine page."
      >
        <Card>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <button onClick={() => void restartBackend()} className="krypt-btn-default">
              <RefreshCw className="h-4 w-4" /> Restart backend
            </button>
            <div className="text-[11px] text-krypt-dim">
              Status:{' '}
              <span className={backend.status === 'running' ? 'text-krypt-win' : 'text-krypt-warn'}>
                {backend.status}
              </span>
              {backend.status === 'running'
                ? ' — open positions keep their management passes; a restart briefly interrupts them.'
                : ' — a restart usually clears a stuck backend.'}
            </div>
          </div>
        </Card>
      </Section>

      <Section title="App preferences">
        <Card>
          <div className="grid gap-2 md:grid-cols-2">
            <Switch
              label="Start with Windows"
              description="Launch Krypt PolyBot at login (silent if Start Minimized is on)."
              checked={!!state?.startWithWindows}
              onChange={(v) => window.krypt.state.setStartWithWindows(v).then(refresh.state)}
            />
            <Switch
              label="Start minimized to tray"
              description="If autostarted, hide to tray on launch. Open from the tray icon."
              checked={!!state?.startMinimized}
              onChange={(v) => window.krypt.state.setStartMinimized(v).then(refresh.state)}
            />
            <Switch
              label="Show Krypt PolyBot on your Discord profile"
              description="Rich Presence: your Discord status reads “Auto-trading on Polymarket”. It never says which markets, which side, or how much. Off = the app does not connect to Discord at all."
              checked={state?.enableDiscordRpc !== false}
              onChange={(v) => window.krypt.state.setEnableDiscordRpc(v).then(refresh.state)}
            />
          </div>
        </Card>
      </Section>

      <Section
        title="Manual terminal"
        description="How the order ticket on a market page behaves."
      >
        <Card>
          <Switch
            label="Desktop notification when a standing instruction fires"
            description="Tells you even when the window is not focused — which is exactly when a stop matters. A desktop notification, not a webhook: it never leaves this machine."
            checked={config.terminalNotifications !== false}
            onChange={(v) => void update('terminalNotifications', v)}
          />
          <div className="my-4 border-t border-krypt-border" />
          <Switch
            label="Paper trading — simulate orders instead of placing them"
            description="Fills are walked out of the REAL order book at the price you would actually have got, with the real fees. Nothing reaches the exchange and no wallet is needed. Paper figures live on Track -> Paper and are never mixed into your real P&L."
            checked={!!config.terminalPaperMode}
            onChange={(v) => void update('terminalPaperMode', v)}
          />
          {config.terminalPaperMode && (
            <>
              <div className="mt-3 rounded-lg border border-krypt-pink/30 bg-krypt-pink/5 p-3 text-xs text-krypt-pink">
                Paper mode is on. The order ticket is labelled PAPER, and the
                backend refuses a live order outright while this is set — so a
                stale screen cannot spend real money behind your back.
              </div>
              <label className="mt-3 block max-w-xs">
                <span className="text-[11px] text-krypt-dim">Starting bankroll (USD)</span>
                <CommitInput
                  type="number" min={1}
                  value={config.terminalPaperBankrollUsd ?? 1000}
                  onCommit={(raw) => {
                    const n = Number(raw);
                    if (Number.isFinite(n) && n >= 1) void update('terminalPaperBankrollUsd', n);
                  }}
                  className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-sm text-white focus:border-krypt-purple/50 focus:outline-none"
                />
                <span className="mt-1 block text-[11px] text-krypt-dim">
                  Changing this re-bases the return figure; it does not alter
                  any simulated fill you have already taken.
                </span>
              </label>
            </>
          )}
        </Card>
      </Section>

      <AiAnalysisSettings config={config} update={update} />

      <DataPrivacy
        offline={!!config.terminalOfflineMode}
        onToggle={(v) => void update('terminalOfflineMode', v)}
      />

      <DangerZone busy={busy} setBusy={setBusy} />
    </Page>
  );
}

function AiAnalysisSettings({
  config, update,
}: {
  config: TraderConfig;
  update: <K extends keyof TraderConfig>(k: K, v: TraderConfig[K]) => Promise<void>;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [draft, setDraft] = useState('');
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const provider: AiProvider = config.aiProvider ?? 'anthropic';
  const modelKey = provider === 'anthropic' ? 'aiAnthropicModel' : 'aiOpenaiModel';
  const model = (config[modelKey] as string | undefined) ?? '';

  const load = () => { void window.krypt.ai.status().then(setStatus).catch(() => {}); };
  useEffect(load, []);
  useEffect(() => { setModels([]); setDraft(''); }, [provider]);

  const saveKey = async () => {
    setBusy(true);
    try {
      const r = await window.krypt.ai.setKey(provider, draft.trim());
      if (!r.ok) { toast.error(r.message); return; }
      toast.success(r.message);
      setDraft('');
      setModels(r.models ?? []);
      load();
    } finally { setBusy(false); }
  };

  const loadModels = async () => {
    setBusy(true);
    try {
      const r = await window.krypt.ai.listModels(provider);
      if (!r.ok) { toast.error(r.message ?? 'Could not load models.'); return; }
      setModels(r.models);
      toast.success(`${r.models.length} models available.`);
    } finally { setBusy(false); }
  };

  const stored = status?.keys?.[provider];

  return (
    <Section
      title="AI analysis"
      description="Have a language model read a market's resolution rules on the Market page — what it turns on, the traps in the wording, and what it does not know. Off by default, on demand only, and billed to your own provider account."
    >
      <Card>
        <Switch
          label="Enable AI analysis"
          description="Adds a 'Read this market' button to every market page. Nothing runs on a timer — each click is one request you pay your provider for."
          checked={!!config.aiEnabled}
          onChange={(v) => void update('aiEnabled', v)}
        />

        <div className="my-4 border-t border-krypt-border" />

        <div className="rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs text-krypt-muted">
          <div className="mb-1 font-medium text-white">What leaves this machine</div>
          The market&rsquo;s question, its published rules text, the current
          quote, volume and close time — public data anyone can read on
          Polymarket. <strong className="text-white">Never your wallet, your
          positions, your cost basis or your orders.</strong> Both provider
          hosts appear in Data &amp; privacy below with live call counts, and
          the offline switch there stops them like every other host.
        </div>

        <div className="mt-3 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs text-krypt-muted">
          <div className="mb-1 font-medium text-white">What it will not tell you</div>
          A probability, a fair value, or an edge in cents. The model is never
          asked for one and the response format gives it nowhere to put one. It
          returns a direction — looks cheap / rich / about right / no view —
          with the reasoning behind it. A number from a language model has no
          calibration behind it and would read as authority it has not earned.
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="block">
            <span className="text-[11px] text-krypt-dim">Provider</span>
            <select
              value={provider}
              onChange={(e) => void update('aiProvider', e.target.value as AiProvider)}
              className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 text-sm text-white focus:border-krypt-purple/50 focus:outline-none"
            >
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI</option>
            </select>
          </label>

          <label className="block">
            <span className="text-[11px] text-krypt-dim">Model</span>
            <div className="mt-0.5 flex gap-2">
              {models.length > 0 ? (
                <select
                  value={model}
                  onChange={(e) => void update(modelKey, e.target.value)}
                  className="min-w-0 flex-1 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-xs text-white focus:border-krypt-purple/50 focus:outline-none"
                >
                  <option value="">(pick one)</option>
                  {models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <CommitInput
                  value={model}
                  onCommit={(raw) => void update(modelKey, raw)}
                  placeholder={provider === 'anthropic' ? 'claude-opus-5' : 'load models to pick'}
                  className="min-w-0 flex-1 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-xs text-white focus:border-krypt-purple/50 focus:outline-none"
                />
              )}
              <button
                onClick={() => void loadModels()}
                disabled={busy || !stored?.present}
                title={stored?.present ? undefined : 'Add an API key first.'}
                className="krypt-btn-default shrink-0 text-xs disabled:opacity-50"
              >
                Load
              </button>
            </div>
            <span className="mt-1 block text-[11px] text-krypt-dim">
              Read from {provider} with your own key, so the list is what that
              key can actually reach.
            </span>
          </label>
        </div>

        <div className="mt-4">
          <span className="text-[11px] text-krypt-dim">
            {provider === 'anthropic' ? 'Anthropic' : 'OpenAI'} API key
          </span>
          <div className="mt-0.5 flex flex-wrap gap-2">
            <input
              type="password"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={stored?.present ? `stored: ${stored.masked}` : 'sk-…'}
              className="min-w-0 flex-1 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-xs text-white focus:border-krypt-purple/50 focus:outline-none"
            />
            <button
              onClick={() => void saveKey()}
              disabled={busy || !draft.trim()}
              className="krypt-btn-primary shrink-0 text-xs disabled:opacity-50"
            >
              Verify &amp; save
            </button>
            {stored?.present && (
              <button
                onClick={async () => {
                  await window.krypt.ai.setKey(provider, '');
                  toast.info('Key cleared.');
                  setModels([]);
                  load();
                }}
                disabled={busy}
                className="krypt-btn-default shrink-0 text-xs disabled:opacity-50"
              >
                Clear
              </button>
            )}
          </div>
          <span className="mt-1 block text-[11px] text-krypt-dim">
            Stored encrypted, the same way your wallet key is — never in
            settings.json, and never read back to this screen. Checked against
            the provider before it is saved, so a typo fails here.
          </span>
        </div>

        <div className="my-4 border-t border-krypt-border" />

        <Switch
          label="Let it search the web"
          description="Grounds the reading in current sources instead of a training cutoff, which matters most on event markets. Costs extra per search and adds a few seconds. With this off, the panel says plainly that anything recent is outside what the model can see."
          checked={!!config.aiWebSearch}
          onChange={(v) => void update('aiWebSearch', v)}
        />
      </Card>
    </Section>
  );
}

function DataPrivacy({
  offline, onToggle,
}: { offline: boolean; onToggle: (v: boolean) => void }) {
  const [health, setHealth] = useState<TerminalHealth | null>(null);

  useEffect(() => {
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy) return;
      busy = true;
      try {
        const h = await window.krypt.terminal.health();
        if (alive) setHealth(h);
      } catch {
      } finally { busy = false; }
    }
    void tick();
    const id = window.setInterval(tick, 5000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  const providers = health?.providers ?? [];
  const totalCalls = providers.reduce((n, p) => n + p.calls, 0);
  const totalErrors = providers.reduce((n, p) => n + p.errors, 0);
  const hits = providers.reduce((n, p) => n + p.cacheHits, 0);
  const misses = providers.reduce((n, p) => n + p.cacheMisses, 0);
  const cachePct = hits + misses > 0 ? Math.round((hits / (hits + misses)) * 100) : null;

  return (
    <Section
      title="Data & privacy"
      description="Every host this machine contacts for the terminal, and what each is used for."
    >
      <Card>
        <p className="mb-4 text-xs text-krypt-muted">
          There is no Krypt server in this path. Your machine talks to
          Polymarket directly, and nothing about what you look at or trade is
          sent anywhere else. The counts below are live, so you can check that
          rather than take our word for it.
        </p>

        {providers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-krypt-border p-4 text-center text-xs text-krypt-dim">
            No provider activity recorded yet — open Discover or a market and
            these counters start moving.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                  <th className="pb-2">Host</th>
                  <th className="pb-2">Used for</th>
                  <th className="pb-2 text-right">Requests</th>
                  <th className="pb-2 text-right">Errors</th>
                  <th className="pb-2 text-right">Median</th>
                  <th className="pb-2 text-right">Served from cache</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((p) => {
                  const seen = p.cacheHits + p.cacheMisses;
                  return (
                    <tr key={p.host} className="border-b border-krypt-border/50 last:border-0">
                      <td className="py-2 font-mono text-xs text-white">
                        {p.host}
                        {p.parkedUntil && (
                          <span
                            className="ml-2 rounded-full border border-krypt-warn/40 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-krypt-warn"
                            title={`Rate-limited us; paused until ${p.parkedUntil}. Requests fail fast instead of queueing.`}
                          >
                            paused
                          </span>
                        )}
                      </td>
                      <td className="py-2 text-xs text-krypt-muted">{p.purpose}</td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        {p.calls.toLocaleString()}
                      </td>
                      <td className={cls('py-2 text-right font-mono',
                        p.errors > 0 ? 'text-krypt-warn' : 'text-krypt-dim')}
                      >
                        {p.errors.toLocaleString()}
                      </td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        <Val v={p.latencyMs} fmt={(n) => `${n.toFixed(0)}ms`} />
                      </td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        {seen ? `${Math.round((p.cacheHits / seen) * 100)}%` : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {providers.length > 0 && (
          <div className="mt-3 text-[11px] text-krypt-dim">
            {totalCalls.toLocaleString()} request(s) this session,{' '}
            {totalErrors.toLocaleString()} error(s)
            {cachePct !== null && <> · {cachePct}% served from cache without a request</>}.
          </div>
        )}

        <div className="mt-4 border-t border-krypt-border pt-4">
          <Switch
            label="Offline mode — contact no providers"
            description="Stops every terminal request. Discover, charts, books and the tape go blank and say why; your watchlist, fill ledger and the trading engines are untouched."
            checked={offline}
            onChange={onToggle}
          />
          {offline && (
            <div className="mt-3 rounded-lg border border-krypt-warn/30 bg-krypt-warn/5 p-3 text-xs text-krypt-warn">
              Offline mode is on. The terminal is showing you nothing rather
              than showing you something stale — every panel explains which
              data is missing and why.
            </div>
          )}
        </div>
      </Card>
    </Section>
  );
}

function DangerZone({
  busy, setBusy,
}: { busy: boolean; setBusy: (b: boolean) => void }) {
  const toast = useToast();
  const [showModal, setShowModal] = useState(false);
  const [phrase, setPhrase] = useState('');

  const openModal = (): void => {
    setPhrase('');
    setShowModal(true);
  };

  const cancel = (): void => {
    setShowModal(false);
    setPhrase('');
  };

  const confirm = async (): Promise<void> => {
    if (phrase.trim() !== 'RESET') {
      toast.error('Type RESET (uppercase) to confirm.');
      return;
    }
    setShowModal(false);
    setBusy(true);
    try {
      const r = await window.krypt.app.factoryReset();
      if (r.ok) {
        const summary = (r.data as { deleted?: Record<string, number> })?.deleted || {};
        const detail = Object.entries(summary)
          .filter(([k, v]) => !k.startsWith('_') && (v as number) > 0)
          .map(([k, v]) => `${k}: ${v}`)
          .join(', ');
        toast.success(detail
          ? `Wiped — ${detail}`
          : (r.message || 'Local data cleared (nothing to delete)'));
      } else {
        toast.error(r.message || 'Reset failed');
      }
    } catch (e: any) {
      toast.error(`${e?.message || e}`);
    } finally {
      setBusy(false);
      setPhrase('');
    }
  };

  return (
    <>
      <Section title="Danger zone">
        <Card>
          <div className="space-y-3 rounded-xl border border-rose-500/40 bg-rose-500/5 p-4">
            <div>
              <div className="text-sm font-semibold text-rose-300">
                Full reset
              </div>
              <p className="text-xs text-krypt-muted mt-1">
                Wipes all locally stored trading history, P&amp;L snapshots,
                bot runs, and signals. Your wallet, profiles, and settings are
                preserved. Live Polymarket positions will be re-imported on the
                next reconcile cycle. Useful for clearing inconsistent state
                from old builds before live testing.
              </p>
            </div>
            <button
              onClick={openModal}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-lg border border-rose-500/60 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-200 hover:bg-rose-500/25 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RotateCcw className="h-4 w-4" />
              Full reset
            </button>
          </div>
        </Card>
      </Section>

      {showModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
          onClick={cancel}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-rose-500/50 bg-krypt-panel p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-rose-300">
              Confirm full reset
            </h3>
            <div className="mt-3 space-y-2 text-sm text-krypt-muted">
              <p>This will permanently delete:</p>
              <ul className="ml-5 list-disc space-y-1">
                <li>all bot positions and trade history</li>
                <li>all bot runs (session P&amp;L)</li>
                <li>all P&amp;L snapshots</li>
                <li>all whale and momentum signals</li>
              </ul>
              <p className="pt-2">
                Your wallet, profiles, and settings are <strong>kept</strong>.
                Live Polymarket positions will be re-imported on the next
                reconcile cycle.
              </p>
            </div>
            <label className="mt-4 block text-xs font-semibold uppercase tracking-wider text-krypt-muted">
              Type <span className="text-rose-300">RESET</span> to confirm
            </label>
            <input
              autoFocus
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void confirm();
                if (e.key === 'Escape') cancel();
              }}
              className="mt-1 w-full rounded-lg border border-rose-500/40 bg-black/30 px-3 py-2 text-sm font-mono text-rose-100 placeholder-krypt-muted/50 outline-none focus:border-rose-400"
              placeholder="RESET"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={cancel}
                className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm font-semibold text-krypt-muted hover:bg-white/10"
              >
                Cancel
              </button>
              <button
                onClick={() => void confirm()}
                disabled={phrase.trim() !== 'RESET'}
                className="rounded-lg border border-rose-500/60 bg-rose-500/20 px-4 py-2 text-sm font-semibold text-rose-100 hover:bg-rose-500/30 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Wipe everything
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
