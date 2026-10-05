import { useCallback, useEffect, useState } from 'react';
import {
  Bot, Check, Copy, Eye, Loader2, Plug, RefreshCw, RotateCcw, ShieldAlert, Trash2,
} from 'lucide-react';
import type {
  McpClientId, McpClientSetup, McpStatus, TraderConfig,
} from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, Empty, Page, Section, Switch } from '../components/common';
import { CommitInput, Note } from '../components/terminal/TerminalBits';
import { cls } from '../utils/format';

type PaperView = {
  portfolio: {
    cashUsd?: number; equityUsd?: number | null; returnPct?: number | null;
    realizedPnlUsd?: number; bankrollUsd?: number;
    positions?: { conditionId: string; title: string | null; side: string; size: number;
      avgCostCents: number | null; markCents: number | null; unrealizedPnlUsd: number | null }[];
  } | null;
  orders: { orders?: { localId: number; title: string | null; side: string; action: string;
    filledSize: number; avgFillPriceCents: number | null; status: string; createdAt: string }[] } | null;
};

const money = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? '—' : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function AiAgentsPage() {
  const { config, refresh } = useApp();
  const toast = useToast();
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [clients, setClients] = useState<McpClientSetup[]>([]);
  const [paper, setPaper] = useState<PaperView | null>(null);

  const load = useCallback(async () => {
    try { setStatus(await window.krypt.mcp.status()); } catch {}
    try { setPaper(await window.krypt.mcp.paper() as PaperView); } catch {}
  }, []);
  const loadClients = useCallback(async () => {
    try { setClients(await window.krypt.mcp.clients()); } catch {}
  }, []);

  useEffect(() => {
    void load();
    void loadClients();
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
  }, [load, loadClients]);

  const update = useCallback(async <K extends keyof TraderConfig>(
    key: K, value: TraderConfig[K],
  ) => {
    await window.krypt.config.update({ [key]: value } as Partial<TraderConfig>);
    await refresh.state();
    try { setStatus(await window.krypt.mcp.sync()); } catch {}
  }, [refresh]);

  if (!config) {
    return <Page title="AI Agents"><div className="text-krypt-muted">Loading…</div></Page>;
  }

  const on = !!config.mcpEnabled;
  const live = !!config.mcpLiveTradingEnabled;
  const spent = status?.spentTodayUsd;
  const daily = config.mcpDailyLimitUsd ?? 100;

  return (
    <Page
      title="AI Agents"
      subtitle="Let Claude, Cursor, Codex or any MCP client read markets, paper trade, backtest — and, if you allow it, trade."
      actions={
        <button
          onClick={() => { void load(); void loadClients(); }}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-sm text-krypt-muted hover:text-white"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      }
    >
      <Section title="Before you turn this on">
        <Card>
          <div className="space-y-3 text-xs text-krypt-muted">
            <p>
              This runs a local <strong className="text-white">MCP server</strong> inside
              the app. An AI client on <strong className="text-white">this computer</strong> connects
              to it and gets tools: market data, your account, paper trading, backtests,
              strategy scripts, the Perps market maker. It listens on 127.0.0.1 only, behind a token, and refuses
              requests from web pages.
            </p>
            <p>
              <strong className="text-white">What the agent reads is sent to its AI
              provider</strong> (Anthropic, OpenAI, Cursor…): positions, balances and
              orders, as well as markets. That is how these clients work. Everything else in
              this app stays on your machine.
            </p>
            <p>
              A language model can be wrong with total confidence, and market text can carry
              instructions meant for it. Start with paper trading: the agent gets its own
              paper account, separate from yours, so its record stands on its own.
            </p>
          </div>
          <div className="mt-4 border-t border-krypt-border pt-4">
            <Switch
              label="Allow AI agents"
              description="Starts the server. Agents can read, paper trade and backtest. Nothing live until you allow it below."
              checked={on}
              onChange={(v) => void update('mcpEnabled', v)}
            />
          </div>
        </Card>
      </Section>

      {on && (
        <>
          <ServerCard status={status} onChanged={load} config={config} update={update} />

          <Section title="Connect a client" description="One click writes the entry into the client's own config file (a backup is kept). Restart the client afterwards.">
            <div className="grid gap-3 lg:grid-cols-2">
              {clients.map((c) => (
                <ClientCard key={c.id} client={c} onChanged={loadClients} />
              ))}
            </div>
          </Section>

          <Section title="Live trading by agents">
            <Card className={cls(live && 'ring-1 ring-krypt-loss/40')}>
              <div className="mb-3 flex items-start gap-2 text-xs text-krypt-warn">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                <p>
                  With this on, an agent can send real, signed orders from your wallet
                  without asking you first. The caps below are enforced in the backend:
                  an agent cannot raise them, and two parallel calls cannot both slip
                  under them. Paper mode in Settings still blocks every live order.
                </p>
              </div>
              <Switch
                label="Allow live trading"
                description="Real buys and sells, cancels, stops, flattening, switching engines on (including the Perps market maker), changing engine settings (sizing, risk limits, copy wallets, Perps quoting), linking the Perps account, flattening Perps positions and arming the parlay schedule. Turning things off and pausing are always allowed."
                checked={live}
                onChange={(v) => void update('mcpLiveTradingEnabled', v)}
              />
              <div className="mt-4 grid max-w-xl gap-3 sm:grid-cols-2">
                <CapInput
                  label="Max per order (USD)"
                  value={config.mcpMaxOrderUsd ?? 25}
                  onCommit={(n) => void update('mcpMaxOrderUsd', n)}
                />
                <CapInput
                  label="Max per UTC day (USD)"
                  value={daily}
                  onCommit={(n) => void update('mcpDailyLimitUsd', n)}
                />
              </div>
              <p className="mt-2 text-[11px] text-krypt-dim">
                Caps apply to buys and count the size and price sent, including orders
                still resting. Exits are never capped: a cap on getting out is a cap on
                safety. 0 means no cap. The caps cover the agent's own orders; an
                engine it switches on trades with your engine settings.
                {' '}Spent today: <span className="font-mono text-white">
                  {spent == null || !Number.isFinite(spent) ? '—' : money(spent)}
                </span>{daily > 0 && <> of {money(daily)}</>}.
              </p>
            </Card>
          </Section>

          <Section title="Strategy scripts">
            <Card>
              <Switch
                label="Allow agents to write and run scripts"
                description="Agents can save, backtest and enable scripts (shadow mode first). Arming a script for real orders also needs live trading."
                checked={!!config.mcpScriptsEnabled}
                onChange={(v) => void update('mcpScriptsEnabled', v)}
              />
              <p className="mt-2 text-[11px] text-krypt-dim">
                Scripts run as full Python in the process holding your wallet key. A script
                from an agent is refused outright if the security audit finds anything
                critical (network access, credential reads, obfuscation, exec). That audit
                catches the obvious cases, not every case.
              </p>
            </Card>
          </Section>

          <PaperCard paper={paper} config={config} update={update} onReset={async () => {
            setPaper(await window.krypt.mcp.paper(true) as PaperView);
            toast.info('Agent paper account reset.');
          }} />

          <AuditLog rows={status?.audit ?? []} />
        </>
      )}
    </Page>
  );
}

function ServerCard({ status, onChanged, config, update }: {
  status: McpStatus | null;
  onChanged: () => void;
  config: TraderConfig;
  update: <K extends keyof TraderConfig>(k: K, v: TraderConfig[K]) => Promise<void>;
}) {
  const toast = useToast();
  const [token, setToken] = useState<string | null>(null);
  return (
    <Section title="Server">
      <Card>
        <div className="flex flex-wrap items-center gap-3">
          <span className={cls(
            'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
            status?.running
              ? 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win'
              : 'border-krypt-border text-krypt-dim')}
          >
            {status?.running ? 'listening' : 'not running'}
          </span>
          <span className="font-mono text-xs text-white">{status?.url ?? '—'}</span>
          <span className="text-[11px] text-krypt-dim">{status?.toolCount ?? 0} tools</span>
          <label className="ml-auto flex items-center gap-2 text-[11px] text-krypt-dim">
            Port
            <CommitInput
              type="number" min={1024}
              value={config.mcpPort ?? 47615}
              onCommit={(raw) => {
                const n = Math.round(Number(raw));
                if (Number.isFinite(n) && n >= 1024 && n <= 65535) void update('mcpPort', n);
              }}
              className="w-20 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1 font-mono text-xs text-white focus:outline-none"
            />
          </label>
        </div>
        {status?.lastError && <div className="mt-3"><Note tone="warn">{status.lastError}</Note></div>}

        <div className="mt-4 border-t border-krypt-border pt-4">
          <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
            Access token {status?.tokenMasked && <span className="ml-2 font-mono normal-case text-krypt-dim">{status.tokenMasked}</span>}
          </div>
          <p className="mb-2 text-[11px] text-krypt-dim">
            The one-click setups below don't need it: the relay reads it from the encrypted
            store. You only need it for a client you connect over HTTP yourself.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={async () => setToken((await window.krypt.mcp.revealToken()).token || null)}
              className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white"
            >
              <Eye className="h-3.5 w-3.5" /> Reveal
            </button>
            <button
              onClick={async () => {
                await window.krypt.mcp.rotateToken();
                setToken(null);
                toast.success('Token rotated. HTTP clients need the new one; one-click setups keep working.');
                onChanged();
              }}
              className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Rotate
            </button>
            {token && (
              <div className="flex min-w-0 items-center gap-2 rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 px-3 py-1.5">
                <span className="truncate font-mono text-xs text-white">{token}</span>
                <button onClick={() => { void navigator.clipboard?.writeText(token); toast.info('Copied.'); }}
                  className="text-krypt-dim hover:text-white"><Copy className="h-3.5 w-3.5" /></button>
              </div>
            )}
          </div>
        </div>

        <div className="mt-4 border-t border-krypt-border pt-4">
          <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">Connected agents</div>
          {!status?.sessions.length ? (
            <p className="text-[11px] text-krypt-dim">
              None yet. A client appears here once it connects. The app has to stay open
              while an agent uses it.
            </p>
          ) : (
            <div className="space-y-1">
              {status.sessions.map((s) => (
                <div key={s.id || s.connectedAt} className="flex items-center gap-2 rounded-lg border border-krypt-border px-2.5 py-1.5 text-[11px]">
                  <Bot className="h-3.5 w-3.5 text-krypt-win" />
                  <span className="text-white">{s.client}</span>
                  {s.version && <span className="text-krypt-dim">{s.version}</span>}
                  <span className="ml-auto text-krypt-dim">
                    {s.reads} reads · {s.writes} actions · {s.refused} refused · seen {new Date(s.lastSeen).toLocaleTimeString()}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>
    </Section>
  );
}

function ClientCard({ client, onChanged }: { client: McpClientSetup; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);
  const act = async (fn: (id: McpClientId) => Promise<{ ok: boolean; message: string }>) => {
    setBusy(true);
    try {
      const r = await fn(client.id);
      toast[r.ok ? 'success' : 'error'](r.message);
      onChanged();
    } finally { setBusy(false); }
  };
  return (
    <Card>
      <div className="flex items-center gap-2">
        <Plug className="h-4 w-4 text-krypt-muted" />
        <span className="text-sm font-medium text-white">{client.label}</span>
        {client.installed ? (
          <span className="rounded-full border border-krypt-win/40 bg-krypt-win/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-win">added</span>
        ) : client.detected ? (
          <span className="rounded-full border border-krypt-border px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-dim">found</span>
        ) : null}
        <div className="ml-auto flex gap-1.5">
          {client.configPath && (
            client.installed ? (
              <button disabled={busy} onClick={() => void act(window.krypt.mcp.uninstall)}
                title="Remove the entry from this client's config"
                className="rounded-lg border border-krypt-border px-2 py-1 text-krypt-dim hover:text-krypt-loss disabled:opacity-40">
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            ) : (
              <button disabled={busy} onClick={() => void act(window.krypt.mcp.install)}
                className="flex items-center gap-1.5 rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 px-3 py-1 text-xs text-white disabled:opacity-40">
                {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                Add
              </button>
            )
          )}
          <button onClick={() => setShow((v) => !v)}
            className="rounded-lg border border-krypt-border px-2 py-1 text-xs text-krypt-muted hover:text-white">
            {show ? 'Hide' : client.configPath ? 'Manual' : 'Setup'}
          </button>
        </div>
      </div>
      {client.note && <p className="mt-2 text-[11px] text-krypt-dim">{client.note}</p>}
      {show && (
        <div className="mt-2">
          {client.configPath && (
            <div className="mb-1 truncate font-mono text-[10px] text-krypt-dim" title={client.configPath}>
              {client.configPath}
            </div>
          )}
          <div className="relative">
            <pre className="max-h-56 overflow-auto rounded-lg border border-krypt-border bg-krypt-surface p-2 font-mono text-[10px] text-krypt-muted">
              {client.snippet}
            </pre>
            <button
              onClick={() => { void navigator.clipboard?.writeText(client.snippet); toast.info('Copied.'); }}
              className="absolute right-1.5 top-1.5 rounded border border-krypt-border bg-krypt-surface px-1.5 py-1 text-krypt-dim hover:text-white"
            >
              <Copy className="h-3 w-3" />
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}

function CapInput({ label, value, onCommit }: {
  label: string; value: number; onCommit: (n: number) => void;
}) {
  return (
    <label className="block">
      <span className="text-[11px] text-krypt-dim">{label}</span>
      <CommitInput
        type="number" min={0}
        value={value}
        onCommit={(raw) => {
          const n = Number(raw);
          if (raw.trim() !== '' && Number.isFinite(n) && n >= 0) onCommit(n);
        }}
        className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-sm text-white focus:outline-none"
      />
    </label>
  );
}

function PaperCard({ paper, config, update, onReset }: {
  paper: PaperView | null;
  config: TraderConfig;
  update: <K extends keyof TraderConfig>(k: K, v: TraderConfig[K]) => Promise<void>;
  onReset: () => Promise<void>;
}) {
  const pf = paper?.portfolio;
  const positions = pf?.positions ?? [];
  const orders = paper?.orders?.orders ?? [];
  return (
    <Section title="Agent paper account" description="Fills walked out of the real book with real fees. Separate from your own paper account.">
      <Card>
        <div className="flex flex-wrap items-end gap-6">
          <Stat label="Equity" value={money(pf?.equityUsd)} />
          <Stat label="Cash" value={money(pf?.cashUsd)} />
          <Stat label="Realised P&L" value={money(pf?.realizedPnlUsd)} />
          <Stat label="Return" value={pf?.returnPct == null ? '—' : `${pf.returnPct.toFixed(2)}%`} />
          <div className="ml-auto flex items-end gap-2">
            <div className="w-36"><CapInput label="Starting bankroll (USD)"
              value={config.mcpPaperBankrollUsd ?? 1000}
              onCommit={(n) => { if (n >= 1) void update('mcpPaperBankrollUsd', n); }} /></div>
            <button onClick={() => void onReset()}
              className="rounded-lg border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-krypt-loss">
              Reset
            </button>
          </div>
        </div>
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wider text-krypt-muted">Positions</div>
            {!positions.length ? <p className="text-[11px] text-krypt-dim">None.</p> : positions.map((p) => (
              <div key={`${p.conditionId}-${p.side}`} className="flex gap-2 text-[11px]">
                <span className="min-w-0 flex-1 truncate text-white">{p.title ?? p.conditionId}</span>
                <span className="text-krypt-muted">{p.size} {p.side.toUpperCase()}</span>
                <span className={cls('w-16 text-right font-mono',
                  (p.unrealizedPnlUsd ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                  {money(p.unrealizedPnlUsd)}
                </span>
              </div>
            ))}
          </div>
          <div>
            <div className="mb-1 text-[11px] uppercase tracking-wider text-krypt-muted">Recent orders</div>
            {!orders.length ? <p className="text-[11px] text-krypt-dim">None.</p> : orders.slice(0, 12).map((o) => (
              <div key={o.localId} className="flex gap-2 text-[11px]">
                <span className="w-28 shrink-0 text-krypt-dim">{new Date(o.createdAt.replace(' ', 'T') + (o.createdAt.includes('Z') ? '' : 'Z')).toLocaleString()}</span>
                <span className="min-w-0 flex-1 truncate text-white">{o.action} {o.side.toUpperCase()} · {o.title ?? ''}</span>
                <span className="text-krypt-muted">{o.status}</span>
              </div>
            ))}
          </div>
        </div>
      </Card>
    </Section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <div className="font-mono text-lg text-white">{value}</div>
    </div>
  );
}

function AuditLog({ rows }: { rows: McpStatus['audit'] }) {
  return (
    <Section title="Agent activity" description="Every action and every refusal. Reads are counted per agent above, not listed.">
      <Card>
        {!rows.length ? (
          <Empty title="Nothing yet" description="Orders, cancels, engine changes, script saves and refusals appear here." />
        ) : (
          <div className="max-h-80 space-y-1 overflow-y-auto">
            {rows.map((r, i) => (
              <div key={`${r.at}-${i}`} className="flex items-center gap-2 text-[11px]">
                <span className="w-28 shrink-0 text-krypt-dim">{new Date(r.at).toLocaleTimeString()}</span>
                <span className="w-28 shrink-0 truncate text-krypt-muted">{r.client}</span>
                <span className="w-36 shrink-0 truncate font-mono text-white">{r.tool}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-krypt-dim" title={r.detail}>{r.detail}</span>
                <span className={cls('shrink-0 rounded px-1.5 py-0.5 uppercase tracking-wider',
                  r.outcome === 'accepted' ? 'text-krypt-win'
                    : r.outcome === 'refused' ? 'bg-krypt-loss/15 text-krypt-loss'
                      : 'text-krypt-warn')}>
                  {r.outcome}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </Section>
  );
}
