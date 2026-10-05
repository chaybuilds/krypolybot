import { useState } from 'react';
import {
  AlertTriangle, Banknote, Bitcoin, Check, ChevronRight, Cloud, Film, Globe2,
  Pause, Play, Power, RotateCcw, Save, Sparkles, Trophy, Vote,
} from 'lucide-react';
import type { StrategyPreset, TraderConfig } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, NameDialog, NumberInput, Page, RuleBuilder, Section, Switch } from '../components/common';
import { cls, fmtPct, fmtUsd } from '../utils/format';

const KRYPT_CATEGORIES: { id: string; label: string; Icon: typeof Trophy }[] = [
  { id: 'sports', label: 'Sports', Icon: Trophy },
  { id: 'politics', label: 'Politics', Icon: Vote },
  { id: 'economics', label: 'Economics', Icon: Banknote },
  { id: 'crypto', label: 'Crypto', Icon: Bitcoin },
  { id: 'climate', label: 'Climate', Icon: Cloud },
  { id: 'entertainment', label: 'Entertainment', Icon: Film },
  { id: 'world', label: 'World', Icon: Globe2 },
];

const PRESET_IGNORED_PREFIXES = ['crypto15m', 'copy', 'script'];
const PRESET_IGNORED_KEYS = new Set([
  'enableTrading', 'network',
  'statsChartWindowHours',
]);

function matchesPreset(cfg: TraderConfig, preset: TraderConfig): boolean {
  return Object.keys(preset).every((k) => {
    if (PRESET_IGNORED_PREFIXES.some((p) => k.startsWith(p))) return true;
    if (PRESET_IGNORED_KEYS.has(k)) return true;
    const a = (cfg as unknown as Record<string, unknown>)[k];
    const b = (preset as unknown as Record<string, unknown>)[k];
    return a === b || JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  });
}

export function MainEnginePage() {
  const { config, refresh, strategies, backend } = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [saveProfileOpen, setSaveProfileOpen] = useState(false);
  const [examplesOpen, setExamplesOpen] = useState(false);

  if (!config) return <Page title="Main Engine"><div className="text-krypt-muted">Loading…</div></Page>;

  const tradingOn = !!config.enableTrading;

  const toggleTrading = async (): Promise<void> => {
    const next = !tradingOn;
    if (next && !window.confirm(
      'Start the MAIN engine?\n\nIt will place REAL orders with your '
      + 'Polymarket balance following whale + momentum signals. This is not a '
      + 'simulation.\n\nThe Crypto, Copy Trading and Scripts engines are '
      + 'separate and are not affected.',
    )) return;
    const r = await window.krypt.trading.setEnabled(next);
    if (!r.ok) {
      toast.error(r.message || 'Failed to toggle the main engine');
      return;
    }
    toast.success(next ? 'Main engine started' : 'Main engine paused');
  };

  const update = async <K extends keyof TraderConfig>(key: K, value: TraderConfig[K]): Promise<void> => {
    try {
      await window.krypt.config.update({ [key]: value } as Partial<TraderConfig>);
      await refresh.state();
    } catch (e: any) {
      toast.error(`${e?.message || e}`);
    }
  };

  const apply = async (s: StrategyPreset): Promise<void> => {
    setBusyId(s.id);
    try {
      await window.krypt.config.applyStrategy(s.id);
      await refresh.state();
      toast.success(`Loaded example "${s.name}"`);
    } catch (e: any) {
      toast.error(`Could not load: ${e?.message || e}`);
    } finally {
      setBusyId(null);
    }
  };

  const reset = async (): Promise<void> => {
    if (!window.confirm('Reset all trading settings to defaults?')) return;
    setBusy(true);
    try {
      await window.krypt.config.reset();
      await refresh.state();
      toast.success('Reset to defaults');
    } finally {
      setBusy(false);
    }
  };

  const saveAsProfile = async (name: string): Promise<void> => {
    setSaveProfileOpen(false);
    const r = await window.krypt.profiles.save(name);
    if (r.ok) toast.success(r.message || `Saved "${name}"`);
    else toast.error(r.message || 'Could not save profile');
  };

  return (
    <Page
      title="Main Engine"
      subtitle="The whale + momentum trader. Tune any knob below — changes are saved + applied immediately. (Crypto, Copy Trading and Scripts are separate engines with their own pages and their own switches.)"
      actions={
        <>
          <button onClick={reset} disabled={busy} className="krypt-btn-default">
            <RotateCcw className="h-4 w-4" /> Reset
          </button>
          <button onClick={() => setSaveProfileOpen(true)} className="krypt-btn-primary">
            <Save className="h-4 w-4" /> Save as Profile
          </button>
        </>
      }
    >
      <Card className="mb-4">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <button
            onClick={() => void toggleTrading()}
            className={cls(
              tradingOn ? 'krypt-btn-danger' : 'krypt-btn-primary',
              'min-w-[150px]',
            )}
          >
            {tradingOn ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            {tradingOn ? 'Pause this engine' : 'Start this engine'}
            {!backend.authOk && <Power className="ml-1 h-3 w-3 opacity-60" />}
          </button>
          <div className="min-w-0">
            <div className={cls('text-sm font-semibold',
              tradingOn ? 'text-krypt-win' : 'text-krypt-dim')}>
              {tradingOn ? 'LIVE — placing real orders' : 'Paused'}
            </div>
            <div className="text-[11px] text-krypt-dim">
              {!backend.authOk
                ? 'No wallet connected — connect one on the Dashboard before starting.'
                : tradingOn
                  ? 'Follows whale + momentum signals under the gates below. Affects this engine only.'
                  : 'Whale and momentum signals are still recorded while paused — only order placement stops.'}
            </div>
          </div>
        </div>
      </Card>
      <div className="mb-6">
        <button
          onClick={() => setExamplesOpen((o) => !o)}
          aria-expanded={examplesOpen}
          className="flex w-full items-center gap-2 rounded-lg border border-krypt-border bg-krypt-surface2/40 px-3 py-2 text-left hover:border-krypt-borderHi"
        >
          <ChevronRight className={cls('h-4 w-4 shrink-0 text-krypt-muted transition-transform', examplesOpen && 'rotate-90')} />
          <span className="text-sm font-semibold uppercase tracking-[0.16em] text-krypt-muted">
            Example settings
          </span>
          <span className="ml-1 text-xs text-krypt-dim">
            {(() => {
              const loaded = strategies.find((s) => matchesPreset(config, s.config));
              return loaded
                ? `· “${loaded.name}” is loaded`
                : `· ${strategies.length} examples — every one measured negative`;
            })()}
          </span>
        </button>
        {examplesOpen && (
        <>
        <p className="mb-3 mt-2 text-xs text-krypt-dim">
          Worked examples of how the gates and sizing below combine — <span className="font-semibold text-krypt-warn">not
          strategies to run</span>. Each card shows what it measured on the recorded corpus. Loading one sets every
          gate and sizing knob below to its values; it never turns the engine on. Tweak anything afterwards and the
          card stops reading “Loaded” — keep your own version with Save as Profile.
        </p>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {strategies.map((s) => {
            const active = matchesPreset(config, s.config);
            return (
              <div
                key={s.id}
                className={cls(
                  'group relative flex flex-col overflow-hidden rounded-xl border bg-krypt-surface p-5 transition-colors',
                  active
                    ? 'border-krypt-purple shadow-krypt-soft'
                    : 'border-krypt-border hover:border-krypt-borderHi',
                )}
              >
                {s.badge && (
                  <span className={cls(
                    'absolute right-3 top-3 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider',
                    s.badge === 'recommended' && 'bg-krypt-glow text-white shadow-krypt-soft',
                    s.badge === 'new' && 'border border-krypt-pink/40 bg-krypt-pink/10 text-krypt-pink',
                  )}>
                    {s.badge}
                  </span>
                )}
                <div className="flex items-center gap-3">
                  <div className={cls(
                    'grid h-9 w-9 place-items-center rounded-lg',
                    s.riskLabel === 'safe' && 'bg-krypt-win/10 text-krypt-win',
                    s.riskLabel === 'balanced' && 'bg-krypt-purple/15 text-krypt-purple',
                    s.riskLabel === 'aggressive' && 'bg-krypt-loss/10 text-krypt-loss',
                    s.riskLabel === 'experimental' && 'bg-krypt-warn/10 text-krypt-warn',
                  )}>
                    <Sparkles className="h-4 w-4" />
                  </div>
                  <div>
                    <div className="text-sm font-semibold text-white">{s.name}</div>
                    <div className="text-[11px] uppercase tracking-wider text-krypt-muted">
                      {s.riskLabel}
                    </div>
                  </div>
                </div>

                <p className="mt-3 text-xs italic text-krypt-purple/80">{s.tagline}</p>
                <p className="mt-2 flex-1 text-xs leading-relaxed text-krypt-muted">
                  {s.description}
                </p>
                {s.measured && (
                  <div className="mt-3 flex gap-2 rounded-md border border-krypt-warn/30 bg-krypt-warn/5 px-2.5 py-2 text-[11px] leading-snug text-krypt-warn">
                    <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
                    <span>{s.measured}</span>
                  </div>
                )}

                <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
                  <Stat label="Edge ≥" value={`${s.config.minEdgePtsWhale}pt`} />
                  <Stat label="Conf ≥" value={`${s.config.minConfidenceWhale.toFixed(0)}%`} />
                  <Stat label="Cap" value={fmtUsd(s.config.hardMaxPositionUsd)} />
                  <Stat label="Sizing" value={`${fmtPct(s.config.minSizeFraction * 100, 0)}–${fmtPct(s.config.maxSizeFraction * 100, 0)}`} />
                  <Stat label="Max open" value={`${s.config.maxOpenPositions}`} />
                  <Stat label="Daily" value={`${s.config.maxDailyNewPositions}`} />
                </div>

                <button
                  onClick={() => apply(s)}
                  disabled={busyId === s.id}
                  className={cls(
                    'krypt-btn-default',
                    'mt-4 w-full',
                  )}
                >
                  {active ? (
                    <>
                      <Check className="h-4 w-4" /> Loaded
                    </>
                  ) : (
                    <>
                      Load example <ChevronRight className="h-4 w-4" />
                    </>
                  )}
                </button>
              </div>
            );
          })}
        </div>
        </>
        )}
      </div>

      <Section title="Environment">
        <Card>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="krypt-label">Network</label>
              <div className="flex gap-2">
                <div className="flex-1 rounded-md border border-krypt-purple bg-krypt-purple/10 px-3 py-2 text-sm text-white">
                  Polygon mainnet
                </div>
              </div>
              <p className="krypt-help">
                Polymarket trades on Polygon mainnet (real USDC). Connect a
                funded wallet on the Wallet page. Turning this on places real
                orders — the terminal&apos;s paper mode does not cover this engine.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Switch
                label="Auto-trading enabled"
                description="When ON, the main bot places REAL orders with your Polymarket balance. When off, signals still stream in but nothing is placed."
                checked={config.enableTrading}
                onChange={(v) => {
                  if (v && !window.confirm(
                    'Start LIVE trading?\n\nThe bot will place REAL orders with your '
                    + 'Polymarket balance. This is not a simulation.',
                  )) return;
                  void update('enableTrading', v);
                }}
              />
            </div>
          </div>
        </Card>
      </Section>

      <Section
        title="Signal sources"
        description="Toggle whole signal sources on/off. Off means the scanners still run but the trader ignores them."
      >
        <Card>
          <div className="grid gap-2 md:grid-cols-2">
            <Switch
              label="Trade whale signals"
              description="Follow $2.5k+ taker orders into the same side."
              checked={config.tradeWhales}
              onChange={(v) => void update('tradeWhales', v)}
            />
            <Switch
              label="Trade momentum signals"
              description="Fade clusters of trades against the underdog."
              checked={config.tradeMomentum}
              onChange={(v) => void update('tradeMomentum', v)}
            />
          </div>
        </Card>
      </Section>

      <Section
        title="Categories"
        description="Restrict which kinds of markets the bot is allowed to trade. All selected = no filtering; an empty selection means the bot trades nothing."
      >
        <Card>
          <CategoryPicker
            value={config.allowedCategories}
            onChange={(v) => void update('allowedCategories', v)}
          />
        </Card>

        <Card className="mt-4">
          <div className="text-sm font-medium text-white">Per-engine refinement</div>
          <p className="mt-1 text-xs leading-relaxed text-krypt-muted">
            Optional. These narrow <i>further</i> within the categories allowed above —
            both filters must pass. Use them to run, say, whales only in crypto while
            momentum only trades sports. Leave on “Any” unless you want that split;
            note that <b className="text-white">strategy presets set these for you</b>,
            which is why a category can look enabled above yet still be skipped.
          </p>
          <div className="mt-4 space-y-5">
            <div>
              <div className="mb-2 text-xs font-medium text-krypt-muted">
                Whale &amp; convergence signals
              </div>
              <SourceCategoryPicker
                value={config.allowedWhaleCategories ?? null}
                onChange={(v) => void update('allowedWhaleCategories', v)}
              />
            </div>
            <div>
              <div className="mb-2 text-xs font-medium text-krypt-muted">
                Momentum signals
              </div>
              <SourceCategoryPicker
                value={config.allowedMomentumCategories ?? null}
                onChange={(v) => void update('allowedMomentumCategories', v)}
              />
            </div>
          </div>
        </Card>
      </Section>

      <Section
        title="Signal gates"
        description="Higher thresholds = fewer, higher-quality trades. Edge is confidence minus market-implied probability."
      >
        <Card>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Min edge (whales)" hint="Pts of edge required for a whale signal to fire">
              <NumberInput value={config.minEdgePtsWhale} step={0.5} suffix="pts"
                onChange={(v) => void update('minEdgePtsWhale', v)} />
            </Field>
            <Field label="Min edge (momentum)">
              <NumberInput value={config.minEdgePtsMomentum} step={0.5} suffix="pts"
                onChange={(v) => void update('minEdgePtsMomentum', v)} />
            </Field>
            <Field label="Min confidence (whales)">
              <NumberInput value={config.minConfidenceWhale} step={1} suffix="%"
                onChange={(v) => void update('minConfidenceWhale', v)} />
            </Field>
            <Field label="Min confidence (momentum)">
              <NumberInput value={config.minConfidenceMomentum} step={1} suffix="%"
                onChange={(v) => void update('minConfidenceMomentum', v)} />
            </Field>
            <Field label="Min entry price">
              <NumberInput value={config.minEntryPriceCents} step={1} min={1} max={99} suffix="¢"
                onChange={(v) => void update('minEntryPriceCents', v)} />
            </Field>
            <Field label="Max entry price">
              <NumberInput value={config.maxEntryPriceCents} step={1} min={1} max={99} suffix="¢"
                onChange={(v) => void update('maxEntryPriceCents', v)} />
            </Field>
            <Field label="Max signal age" hint="Older signals are skipped">
              <NumberInput value={config.maxSignalAgeSec} step={10} suffix="s"
                onChange={(v) => void update('maxSignalAgeSec', v)} />
            </Field>
            <Field label="Max resolution time"
              hint="Skip markets that won't resolve for longer than this (e.g. long-dated politics bets that tie up capital for months). 0 = no limit.">
              <NumberInput value={config.maxResolutionDays ?? 0} step={1} min={0} suffix="days"
                onChange={(v) => void update('maxResolutionDays', v)} />
            </Field>
            <Field label="Contrarian only (momentum)">
              <Switch
                checked={config.contrarianOnly}
                label="Only fade markets where the cluster runs against current price"
                onChange={(v) => void update('contrarianOnly', v)}
              />
            </Field>
          </div>
        </Card>
      </Section>

      <Section
        title="Custom entry rules"
        description="Advanced: compose your own entry from confidence, edge and entry cost. When on, this replaces the confidence/edge/price-bound gates above — source toggles, momentum signal-type, and category filters still apply."
      >
        <Card>
          <RuleBuilder
            config={config}
            fields={[
              { key: 'confidence', label: 'Confidence (0–100)', dflt: 60 },
              { key: 'edge', label: 'Edge (pts)', dflt: 5 },
              { key: 'costCents', label: 'Entry cost (¢)', dflt: 50 },
            ]}
            useRulesKey="useRules"
            rulesKey="rules"
            label="Use custom entry rules"
            description="Every condition must pass (AND). The bot enters any signal whose numbers clear all of your rules."
            tip={
              <>Entry cost is what you actually pay per contract (direction-adjusted), so one rule-set
              works across whales, momentum and convergence.</>
            }
          />
        </Card>
      </Section>

      <Section
        title="Bet sizing"
        description="Each trade ramps from your Min to your Max by signal strength (bigger bets on higher-conviction signals). Choose whether that's a % of your balance or a fixed number of contracts — the hard cap and reserves apply either way."
      >
        <Card>
          <div className="mb-4">
            <label className="krypt-label">Sizing mode</label>
            <div className="flex gap-1.5">
              {([['percent', '% of balance'], ['contracts', 'Fixed contracts']] as const).map(([m, lbl]) => (
                <button
                  key={m}
                  onClick={() => void update('sizingMode', m)}
                  className={cls(
                    'flex-1 rounded-md border px-2 py-2 text-xs',
                    (config.sizingMode ?? 'percent') === m
                      ? 'border-krypt-purple bg-krypt-purple/10 text-white'
                      : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
                  )}
                >
                  {lbl}
                </button>
              ))}
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            {(config.sizingMode ?? 'percent') === 'contracts' ? (
              <>
                <Field label="Min contracts" hint="weak signals · Polymarket minimum is 5">
                  <NumberInput value={config.minContracts ?? 5} step={1} min={1}
                    onChange={(v) => void update('minContracts', Math.round(v))} />
                </Field>
                <Field label="Max contracts" hint="high-conviction signals">
                  <NumberInput value={config.maxContracts ?? 20} step={1} min={1}
                    onChange={(v) => void update('maxContracts', Math.round(v))} />
                </Field>
              </>
            ) : (
              <>
                <Field label="Min size" hint="% of balance on weak signals">
                  <NumberInput value={+(config.minSizeFraction * 100).toFixed(2)} step={0.5} min={0.1} max={100} suffix="%"
                    onChange={(v) => void update('minSizeFraction', v / 100)} />
                </Field>
                <Field label="Max size" hint="% of balance on high-conviction signals">
                  <NumberInput value={+(config.maxSizeFraction * 100).toFixed(2)} step={0.5} min={0.1} max={100} suffix="%"
                    onChange={(v) => void update('maxSizeFraction', v / 100)} />
                </Field>
              </>
            )}
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <Field label="Min-size edge" hint="bet Min at/under this edge">
              <NumberInput value={config.sizingBaseEdge} step={1} suffix="pts"
                onChange={(v) => void update('sizingBaseEdge', v)} />
            </Field>
            <Field label="Max-size edge" hint="bet Max at/over this edge">
              <NumberInput value={config.sizingMaxEdge} step={1} suffix="pts"
                onChange={(v) => void update('sizingMaxEdge', v)} />
            </Field>
            <Field label="Hard cap per trade" hint="$ ceiling — applies in both modes">
              <NumberInput value={config.hardMaxPositionUsd} step={5} prefix="$"
                onChange={(v) => void update('hardMaxPositionUsd', v)} />
            </Field>
            <Field label="Min cash reserve" hint="kept uninvested">
              <NumberInput value={+(config.minCashReserveFraction * 100).toFixed(1)} step={1} min={0} max={99} suffix="%"
                onChange={(v) => void update('minCashReserveFraction', v / 100)} />
            </Field>
            <Field label="Max total exposure" hint="across all open trades">
              <NumberInput value={+(config.maxTotalExposureFraction * 100).toFixed(0)} step={5} min={0} max={100} suffix="%"
                onChange={(v) => void update('maxTotalExposureFraction', v / 100)} />
            </Field>
            <Field label="Starting bankroll" hint="0 = auto-detect">
              <NumberInput value={config.startBankrollUsd} step={50} min={0} prefix="$"
                onChange={(v) => void update('startBankrollUsd', v)} />
            </Field>
          </div>
        </Card>
      </Section>

      <Section
        title="Order placement"
        description="How the bot translates a signal into an actual Polymarket order."
      >
        <Card>
          <div className="grid gap-4 md:grid-cols-3">
            <div>
              <label className="krypt-label">Order style</label>
              <div className="flex gap-1.5">
                {(['limit_cross', 'limit_mid', 'market'] as const).map((o) => (
                  <button
                    key={o}
                    onClick={() => void update('orderStyle', o)}
                    className={cls(
                      'flex-1 rounded-md border px-2 py-2 text-xs',
                      config.orderStyle === o
                        ? 'border-krypt-purple bg-krypt-purple/10 text-white'
                        : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
                    )}
                  >
                    {o.replace('_', '-')}
                  </button>
                ))}
              </div>
              <p className="krypt-help">
                limit-cross hits the opposite side&apos;s best bid (highest fill rate).
              </p>
            </div>
            <Field label="Cross fallback offset">
              <NumberInput value={config.crossSpreadFallbackOffset} step={1} suffix="¢"
                onChange={(v) => void update('crossSpreadFallbackOffset', v)} />
            </Field>
            <Field label="Order expiration" hint="Auto-cancel if unfilled this long. 0 = never">
              <NumberInput value={config.orderExpirationSec ?? 0} step={10} suffix="s"
                onChange={(v) => void update('orderExpirationSec', v <= 0 ? null : v)} />
            </Field>
          </div>
        </Card>
      </Section>

      <Section
        title="Concurrency &amp; risk"
        description="Hard caps that prevent runaway exposure when many signals fire at once."
      >
        <Card>
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Max open positions">
              <NumberInput value={config.maxOpenPositions} step={1} min={1}
                onChange={(v) => void update('maxOpenPositions', v)} />
            </Field>
            <Field label="Max positions per event">
              <NumberInput value={config.maxPositionsPerEvent} step={1} min={1}
                onChange={(v) => void update('maxPositionsPerEvent', v)} />
            </Field>
            <Field label="Max new positions / day"
              hint={config.unlimitedDailyNewPositions
                ? 'Disabled — unlimited mode is on'
                : undefined}>
              <NumberInput
                value={config.maxDailyNewPositions}
                step={5}
                min={1}
                disabled={config.unlimitedDailyNewPositions}
                onChange={(v) => void update('maxDailyNewPositions', v)}
              />
            </Field>
            <Field label="Unlimited daily new positions"
              hint="Off = enforce the daily cap above. On = the only entry limit is Max open positions.">
              <div className="flex h-9 items-center">
                <Switch
                  checked={!!config.unlimitedDailyNewPositions}
                  onChange={(v) => void update('unlimitedDailyNewPositions', v)}
                />
              </div>
            </Field>
            <Field label="Max daily loss" hint="Halts new entries once today's loss reaches this. 0 = off">
              <NumberInput value={Math.abs(config.stopLossOnDay)} step={5} prefix="$" min={0}
                onChange={(v) => void update('stopLossOnDay', -Math.abs(v))} />
            </Field>
            <Field label="Sell out on daily loss"
              hint="Off = only halt new entries (open positions ride to settlement). On = also market-sell every open position when the daily loss limit trips, hard-capping the day's loss.">
              <div className="flex h-9 items-center">
                <Switch
                  checked={!!config.flattenOnDailyStop}
                  onChange={(v) => void update('flattenOnDailyStop', v)}
                />
              </div>
            </Field>
            <Field label="Daily take-profit" hint="Halts new entries when reached. 0 = disabled">
              <NumberInput value={config.takeProfitOnDay} step={5} prefix="$"
                onChange={(v) => void update('takeProfitOnDay', v)} />
            </Field>
            <Field label="Per-position take-profit %" hint="Actively SELLS an open position once its live best bid is worth this % more than it cost (e.g. 20 = cash out at +20%), instead of holding to settlement. Percent auto-scales to entry price: a ~$1 favorite can't reach it and naturally holds. Unlike the daily take-profit (which only halts new entries), this closes individual winners. 0 = off.">
              <NumberInput value={Math.round((config.takeProfitPct ?? 0) * 100)} step={5} suffix="%" min={0} max={100}
                onChange={(v) => void update('takeProfitPct', Math.max(0, Math.min(100, v)) / 100)} />
            </Field>
            <Field label="Lifetime loss limit" hint="Circuit-breaker: pause whale/momentum entries once TOTAL realized loss reaches this % of your starting bankroll (survives history wipes; the daily stop re-arms every midnight — this one doesn't). Default 50%. Raise or set 0 (off) to resume a tripped engine.">
              <NumberInput value={Math.round((config.lifetimeLossLimitPct ?? 0.5) * 100)} step={5} suffix="%" min={0} max={100}
                onChange={(v) => void update('lifetimeLossLimitPct', Math.max(0, Math.min(100, v)) / 100)} />
            </Field>
            <Field label="Lifetime limit $" hint="Absolute-$ version of the lifetime breaker; when > 0 it overrides the %. 0 = use the %">
              <NumberInput value={config.lifetimeLossLimitUsd ?? 0} step={10} prefix="$" min={0}
                onChange={(v) => void update('lifetimeLossLimitUsd', Math.max(0, v))} />
            </Field>
          </div>
        </Card>
      </Section>

      <Section
        title="Trading hours"
        description="Optionally restrict the bot to only trade during certain hours and days. Polling, resolution, and signal scanning still run 24/7 — only new entries are gated."
      >
        <Card>
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm text-white">Restrict trading to a weekly window</div>
            <Switch
              checked={config.tradingHoursEnabled}
              onChange={(v) => void update('tradingHoursEnabled', v)}
            />
          </div>
          <div className={config.tradingHoursEnabled ? '' : 'pointer-events-none opacity-40'}>
            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Start (HH:MM)" hint="Local time, 24h format">
                <input
                  type="time"
                  value={config.tradingHoursStart}
                  onChange={(e) => void update('tradingHoursStart', e.target.value)}
                  className="w-full rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 font-mono text-sm text-white"
                />
              </Field>
              <Field label="End (HH:MM)" hint="Same day or next-morning (overnight ranges supported)">
                <input
                  type="time"
                  value={config.tradingHoursEnd}
                  onChange={(e) => void update('tradingHoursEnd', e.target.value)}
                  className="w-full rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 font-mono text-sm text-white"
                />
              </Field>
              <Field label="UTC offset (minutes)" hint="0 = UTC · -300 = US Eastern (winter) · -240 = US Eastern (summer)">
                <NumberInput
                  value={config.tradingTimezoneOffsetMin}
                  step={30}
                  onChange={(v) => void update('tradingTimezoneOffsetMin', v)}
                />
              </Field>
            </div>
            <div className="mt-3">
              <div className="mb-2 text-xs uppercase tracking-wider text-krypt-muted">Active days</div>
              <DayPicker
                value={config.tradingDays}
                onChange={(v) => void update('tradingDays', v)}
              />
            </div>
            <div className="mt-3 rounded-md border border-krypt-border bg-krypt-surface2 p-3 text-[11px] text-krypt-muted">
              <span className="text-white">Tip:</span> sports markets settle on event clocks
              — restrict to evenings (19:00–23:30) if you only want trades around U.S.
              prime time. Late-night liquidity gets thin and the bot's edge can decay.
            </div>
          </div>
        </Card>
      </Section>

      <Section
        title="Loop cadence"
        description="How often each subsystem runs. Lower = more API calls; higher = laggier."
      >
        <Card>
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Trade scan interval">
              <NumberInput value={config.tradeScanInterval} step={5} suffix="s"
                onChange={(v) => void update('tradeScanInterval', v)} />
            </Field>
            <Field label="Position poll interval">
              <NumberInput value={config.positionPollInterval} step={5} suffix="s"
                onChange={(v) => void update('positionPollInterval', v)} />
            </Field>
            <Field label="Balance poll interval">
              <NumberInput value={config.balancePollInterval} step={5} suffix="s"
                onChange={(v) => void update('balancePollInterval', v)} />
            </Field>
            <Field label="Resolution check">
              <NumberInput value={config.resolutionCheckInterval} step={30} suffix="s"
                onChange={(v) => void update('resolutionCheckInterval', v)} />
            </Field>
            <Field label="Whale scan interval">
              <NumberInput value={config.whaleScanInterval} step={10} suffix="s"
                onChange={(v) => void update('whaleScanInterval', v)} />
            </Field>
            <Field label="Momentum scan interval">
              <NumberInput value={config.momentumScanInterval} step={10} suffix="s"
                onChange={(v) => void update('momentumScanInterval', v)} />
            </Field>
            <Field label="Market refresh interval">
              <NumberInput value={config.marketRefreshInterval} step={30} suffix="s"
                onChange={(v) => void update('marketRefreshInterval', v)} />
            </Field>
          </div>
        </Card>
      </Section>

      <Section
        title="Whale + momentum scanner thresholds"
        description="Lower thresholds = more raw signals (which the trade gates will further filter)."
      >
        <Card>
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Min whale $">
              <NumberInput value={config.minWhaleUsd} step={500} prefix="$"
                onChange={(v) => void update('minWhaleUsd', v)} />
            </Field>
            <Field label="Min whale confidence">
              <NumberInput value={config.minWhaleConfidence} step={1} suffix="%"
                onChange={(v) => void update('minWhaleConfidence', v)} />
            </Field>
            <Field label="Min entry price (whale)">
              <NumberInput value={config.minEntryPriceFrac} step={0.05} min={0} max={1}
                onChange={(v) => void update('minEntryPriceFrac', v)} />
            </Field>
          </div>
        </Card>
      </Section>

      <NameDialog
        open={saveProfileOpen}
        title="Save these settings as a profile"
        label="Saves a snapshot of your current trader config."
        placeholder="Profile name"
        confirmLabel="Save"
        onSubmit={(name) => void saveAsProfile(name)}
        onClose={() => setSaveProfileOpen(false)}
      />
    </Page>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1">
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <div className="font-mono text-[11px] text-white">{value}</div>
    </div>
  );
}

function Field({
  label, hint, children,
}: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="krypt-label">{label}</label>
      {children}
      {hint && <p className="krypt-help">{hint}</p>}
    </div>
  );
}

function SourceCategoryPicker({
  value, onChange,
}: { value: string[] | null; onChange: (v: string[] | null) => void }) {
  const any = value === null;
  const selected = new Set(value ?? []);

  const toggle = (id: string): void => {
    if (any) {
      onChange(KRYPT_CATEGORIES.map((c) => c.id).filter((c) => c !== id));
      return;
    }
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    if (next.size === KRYPT_CATEGORIES.length) onChange(null);
    else onChange(Array.from(next));
  };

  const empty = !any && selected.size === 0;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          onClick={() => onChange(null)}
          className={cls(
            'rounded-md border px-2.5 py-1 text-xs transition-colors',
            any
              ? 'border-krypt-purple bg-krypt-purple/10 text-krypt-purple'
              : 'border-krypt-border text-krypt-dim hover:text-white',
          )}
        >
          Any
        </button>
        {KRYPT_CATEGORIES.map(({ id, label }) => {
          const active = any || selected.has(id);
          return (
            <button
              key={id}
              onClick={() => toggle(id)}
              className={cls(
                'rounded-md border px-2.5 py-1 text-xs transition-colors',
                active && !any
                  ? 'border-krypt-purple bg-krypt-purple/10 text-white'
                  : 'border-krypt-border text-krypt-dim hover:text-white',
              )}
            >
              {label}
            </button>
          );
        })}
      </div>
      {empty && (
        <div className="text-xs text-krypt-loss">
          Nothing selected — this engine will skip every signal.
        </div>
      )}
    </div>
  );
}

function CategoryPicker({
  value, onChange,
}: { value: string[] | null; onChange: (v: string[] | null) => void }) {
  const allEnabled = value === null;
  const selected = new Set(value ?? []);

  const toggleOne = (id: string): void => {
    if (allEnabled) {
      onChange(KRYPT_CATEGORIES.map((c) => c.id).filter((c) => c !== id));
      return;
    }
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    if (next.size === KRYPT_CATEGORIES.length) {
      onChange(null);
    } else {
      onChange(Array.from(next));
    }
  };

  const empty = !allEnabled && selected.size === 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-krypt-muted">
          {allEnabled
            ? 'No filter — every category is allowed.'
            : empty
              ? 'Nothing selected.'
              : `Allowing ${selected.size} of ${KRYPT_CATEGORIES.length} categories.`}
        </div>
        {!allEnabled && (
          <button onClick={() => onChange(null)} className="krypt-btn-default text-xs">
            Allow all
          </button>
        )}
      </div>
      {empty && (
        <div className="flex items-start gap-2 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <b>The bot will not trade.</b> An empty list means every signal is
            skipped. Pick at least one category, or choose “Allow all”.
          </span>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {KRYPT_CATEGORIES.map(({ id, label, Icon }) => {
          const active = allEnabled || selected.has(id);
          return (
            <button
              key={id}
              onClick={() => toggleOne(id)}
              className={cls(
                'flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors',
                active
                  ? 'border-krypt-purple bg-krypt-purple/10 text-white'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
              )}
            >
              <Icon className={cls('h-4 w-4', active ? 'text-krypt-purple' : 'text-krypt-dim')} />
              <span>{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

const DAYS = [
  { id: 'mon', label: 'Mon' },
  { id: 'tue', label: 'Tue' },
  { id: 'wed', label: 'Wed' },
  { id: 'thu', label: 'Thu' },
  { id: 'fri', label: 'Fri' },
  { id: 'sat', label: 'Sat' },
  { id: 'sun', label: 'Sun' },
] as const;

function DayPicker({
  value, onChange,
}: { value: string[]; onChange: (v: string[]) => void }) {
  const set = new Set(value);
  const toggle = (id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(DAYS.filter((d) => next.has(d.id)).map((d) => d.id));
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {DAYS.map((d) => {
        const active = set.has(d.id);
        return (
          <button
            key={d.id}
            onClick={() => toggle(d.id)}
            className={cls(
              'rounded-md border px-3 py-1.5 text-xs font-medium uppercase tracking-wider transition-colors',
              active
                ? 'border-krypt-purple bg-krypt-purple/15 text-white'
                : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
            )}
          >
            {d.label}
          </button>
        );
      })}
    </div>
  );
}
