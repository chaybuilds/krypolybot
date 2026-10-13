import type { CredentialsStatusAll, RegionStatus, TraderConfig } from '@shared/types';
import type { AgentPaper, ForecastScoreboard } from '@shared/market';
import { agentsOf, DEFAULT_AGENT_ID, type McpAgent } from '@shared/agents';

type Cfg = Partial<TraderConfig> | null | undefined;

const usd = (v: number): string => `$${Math.abs(v).toFixed(2)}`;

export interface AgentCaps {
  mcpMaxOrderUsd: number;
  mcpDailySpendUsd: number;
  mcpMaxPositions: number;
  mcpDailyLossUsd: number;
}

export const CAP_KEYS = [
  'mcpMaxOrderUsd', 'mcpDailySpendUsd', 'mcpMaxPositions', 'mcpDailyLossUsd',
] as const satisfies readonly (keyof AgentCaps)[];

export const STARTER_CAPS: Readonly<AgentCaps> = Object.freeze({
  mcpMaxOrderUsd: 5,
  mcpDailySpendUsd: 20,
  mcpMaxPositions: 3,
  mcpDailyLossUsd: 10,
});

const CAP_DEFAULTS: Readonly<AgentCaps> = {
  mcpMaxOrderUsd: 25, mcpDailySpendUsd: 100, mcpMaxPositions: 10, mcpDailyLossUsd: 50,
};

export function currentCaps(c: Cfg): AgentCaps {
  return {
    mcpMaxOrderUsd: c?.mcpMaxOrderUsd ?? CAP_DEFAULTS.mcpMaxOrderUsd,
    mcpDailySpendUsd: c?.mcpDailySpendUsd ?? CAP_DEFAULTS.mcpDailySpendUsd,
    mcpMaxPositions: c?.mcpMaxPositions ?? CAP_DEFAULTS.mcpMaxPositions,
    mcpDailyLossUsd: c?.mcpDailyLossUsd ?? CAP_DEFAULTS.mcpDailyLossUsd,
  };
}

export function starterCapsPatch(): Partial<TraderConfig> {
  return { ...STARTER_CAPS };
}

export function capsProblem(caps: AgentCaps): string | null {
  const money = (v: number): boolean => Number.isFinite(v) && v >= 1;
  if (!money(caps.mcpMaxOrderUsd)) return 'Max per order must be at least $1.';
  if (!money(caps.mcpDailySpendUsd)) return 'Max spend per day must be at least $1.';
  if (!money(caps.mcpDailyLossUsd)) return 'Daily loss stop must be at least $1.';
  if (!Number.isInteger(caps.mcpMaxPositions) || caps.mcpMaxPositions < 1 || caps.mcpMaxPositions > 200) {
    return 'Max open positions must be a whole number from 1 to 200.';
  }
  return null;
}

export function paperPnlOf(p: AgentPaper | null | undefined): number | null {
  const pf = p?.portfolio;
  if (!pf) return null;
  const real = pf.realizedPnlUsd;
  const unreal = pf.totalUnrealizedPnlUsd;
  if (typeof real !== 'number' || !Number.isFinite(real)) return null;
  if (unreal === null) return null;
  if (unreal === undefined) {
    let sum = 0;
    for (const pos of pf.positions ?? []) {
      if (pos.unrealizedPnlUsd === null || !Number.isFinite(pos.unrealizedPnlUsd)) return null;
      sum += pos.unrealizedPnlUsd;
    }
    return real + sum;
  }
  return Number.isFinite(unreal) ? real + unreal : null;
}

export function paperTraded(p: AgentPaper | null | undefined): boolean {
  const pf = p?.portfolio;
  if (!pf) return false;
  if ((pf.positions ?? []).length > 0) return true;
  if ((p?.orders?.orders ?? []).some((o) => (o.filledSize ?? 0) > 0)) return true;
  return typeof pf.realizedPnlUsd === 'number' && Math.abs(pf.realizedPnlUsd) > 1e-9;
}

export type RecordVerdict = 'unavailable' | 'none' | 'too-few' | 'no-edge' | 'worse' | 'beats';

export interface AgentRecord {
  verdict: RecordVerdict;
  forecasts: number | null;
  settled: number;
  minScored: number;
  skill: number | null;
  paperPnlUsd: number | null;
  tone: 'good' | 'warn' | 'bad';
  headline: string;
  notes: string[];
  needsAck: boolean;
}

export function agentRecord(
  board: ForecastScoreboard | null,
  papers: Record<string, AgentPaper | null> | null,
): AgentRecord {
  let paperPnlUsd: number | null = null;
  const notes: string[] = [];
  if (papers === null) {
    notes.push('Paper accounts not loaded, so paper P&L is unknown.');
  } else {
    const traded = Object.values(papers).filter((p) => paperTraded(p));
    if (!traded.length) {
      notes.push('No paper trades yet.');
    } else {
      let sum = 0;
      let known = true;
      for (const p of traded) {
        const v = paperPnlOf(p);
        if (v === null) { known = false; break; }
        sum += v;
      }
      paperPnlUsd = known ? sum : null;
      if (!known) notes.push('Paper P&L unknown: a paper position has no bid to mark it at.');
      else if (sum < 0) notes.push(`Paper is DOWN ${usd(sum)} (realised + open, net of fees).`);
    }
  }
  const minScored = board?.minScored ?? 30;
  const base = { minScored, paperPnlUsd, notes };
  if (!board) {
    return {
      ...base, verdict: 'unavailable', forecasts: null, settled: 0, skill: null, tone: 'bad',
      headline: 'The scoreboard is unavailable, so there is no evidence your agents beat the market.',
      needsAck: true,
    };
  }
  const forecasts = board.totalForecasts;
  const o = board.overall;
  const settled = o?.nPaired ?? 0;
  const skill = o?.skill ?? null;
  const rest = { ...base, forecasts, settled, skill };
  if (forecasts === 0) {
    return {
      ...rest, verdict: 'none', tone: 'bad', needsAck: true,
      headline: 'Your agents have not recorded a single forecast yet. There is no evidence they beat the market.',
    };
  }
  const v = o?.verdict ?? 'too-few';
  if (v === 'too-few' || settled < minScored) {
    return {
      ...rest, verdict: 'too-few', tone: 'warn', needsAck: true,
      headline: `Not enough resolved forecasts yet to tell if they beat the market (${settled} of ${minScored} needed).`,
    };
  }
  if (v === 'market-better') {
    return {
      ...rest, verdict: 'worse', tone: 'bad', needsAck: true,
      headline: `So far they do NOT beat the market price: the market was more accurate (n=${settled}).`,
    };
  }
  if (v === 'indistinguishable') {
    return {
      ...rest, verdict: 'no-edge', tone: 'warn', needsAck: true,
      headline: `So far they do NOT beat the market price: no measurable difference (n=${settled}).`,
    };
  }
  const losing = paperPnlUsd !== null && paperPnlUsd < 0;
  return {
    ...rest, verdict: 'beats', tone: losing ? 'warn' : 'good', needsAck: losing || paperPnlUsd === null,
    headline: `So far they beat the market (n=${settled}). Past markets, not a promise.`,
  };
}

export function agentRecordFor(
  board: ForecastScoreboard | null, paper: AgentPaper | null | undefined, agentId: string,
): AgentRecord {
  const traded = paperTraded(paper);
  const paperPnlUsd = traded ? paperPnlOf(paper) : null;
  const notes: string[] = [];
  if (!traded) notes.push('No paper trades yet.');
  else if (paperPnlUsd === null) notes.push('Paper P&L unknown: a position has no bid to mark it at.');
  else if (paperPnlUsd < 0) notes.push(`Paper is DOWN ${usd(paperPnlUsd)} (realised + open, net of fees).`);
  const minScored = board?.minScored ?? 30;
  const base = { minScored, paperPnlUsd, notes };
  if (!board || !board.byAgent) {
    return {
      ...base, verdict: 'unavailable', forecasts: null, settled: 0, skill: null, tone: 'bad',
      headline: 'No scoreboard for this agent.', needsAck: true,
    };
  }
  const row = board.byAgent.find((r) => r.agentId === agentId) ?? null;
  const forecasts = row?.total ?? 0;
  const settled = row?.nPaired ?? 0;
  const skill = row?.skill ?? null;
  const rest = { ...base, forecasts, settled, skill };
  if (!row || forecasts === 0) {
    return { ...rest, verdict: 'none', tone: 'bad', needsAck: true, headline: 'No forecasts yet.' };
  }
  if (row.verdict === 'too-few' || settled < minScored) {
    return { ...rest, verdict: 'too-few', tone: 'warn', needsAck: true, headline: `Too few resolved (${settled} of ${minScored}).` };
  }
  if (row.verdict === 'market-better') {
    return { ...rest, verdict: 'worse', tone: 'bad', needsAck: true, headline: `The market beat it (n=${settled}).` };
  }
  if (row.verdict === 'indistinguishable') {
    return { ...rest, verdict: 'no-edge', tone: 'warn', needsAck: true, headline: `No measurable edge (n=${settled}).` };
  }
  const losing = paperPnlUsd !== null && paperPnlUsd < 0;
  return {
    ...rest, verdict: 'beats', tone: losing ? 'warn' : 'good', needsAck: losing,
    headline: `Beat the market so far (n=${settled}). Not a promise.`,
  };
}

export interface KeyVerify {
  ok: boolean;
  message: string;
  fingerprint: string;
  balanceUsd?: number | null;
  ready?: boolean;
  issues?: string[];
  fatalIssues?: string[];
  region?: RegionStatus | null;
}

export const REGION_BLOCKED_COPY =
  "Polymarket doesn't allow trading from the region this connection is in, so live orders would all be refused.";
export const REGION_CLOSE_ONLY_COPY =
  'From the region this connection is in, Polymarket only lets you close existing positions: new buys would be refused.';

export function freshestRegion(
  ...rs: (RegionStatus | null | undefined)[]
): RegionStatus | null {
  let best: RegionStatus | null = null;
  for (const r of rs) {
    if (!r) continue;
    if (!best) { best = r; continue; }
    if (r.status === 'unknown' && best.status !== 'unknown') continue;
    if (best.status === 'unknown' && r.status !== 'unknown') { best = r; continue; }
    if ((r.checkedAt ?? 0) > (best.checkedAt ?? 0)) best = r;
  }
  return best;
}

export function regionLine(r: RegionStatus | null | undefined): { tone: 'good' | 'warn' | 'bad'; text: string } | null {
  if (!r) return null;
  const where = regionWhere(r);
  if (r.refusedOrder || r.status === 'blocked') return { tone: 'bad', text: `${REGION_BLOCKED_COPY}${where}` };
  if (r.status === 'close_only') return { tone: 'warn', text: `${REGION_CLOSE_ONLY_COPY}${where}` };
  if (r.status === 'allowed') return { tone: 'good', text: `Polymarket allows trading from this connection${where}.` };
  return { tone: 'warn', text: "Polymarket's region check could not be reached, so whether this connection may trade is unconfirmed." };
}

function regionWhere(r: RegionStatus | null | undefined): string {
  const w = r?.region || r?.country;
  return w ? ` (${w})` : '';
}

export function walletFingerprint(creds: CredentialsStatusAll | null | undefined): string {
  const m = creds?.mainnet;
  if (!m?.hasWalletKey) return '';
  return `${(m.address ?? '').toLowerCase()}|${(m.funder ?? '').toLowerCase()}`;
}

function walletState(
  creds: CredentialsStatusAll | null, verify: KeyVerify | null,
  opts: { buys: boolean; liveRegion?: RegionStatus | null } = { buys: true },
): GoLiveStep {
  const base = { id: 'keys' as const, title: 'Polymarket wallet' };
  if (!creds) {
    return { ...base, state: 'block', detail: 'Wallet status is unavailable (is the backend running?).' };
  }
  const m = creds.mainnet;
  if (!m?.hasWalletKey) {
    return { ...base, state: 'block', detail: 'No wallet connected. Connect one on the Wallet page — Paper never needed one, Live does.' };
  }
  if (!verify || verify.fingerprint !== walletFingerprint(creds)) {
    return { ...base, state: 'block', detail: 'Connected, not tested yet. Test derives the trading credentials and reads your balance.' };
  }
  if (!verify.ok) {
    return { ...base, state: 'block', detail: `Polymarket did not accept this wallet: ${verify.message}` };
  }
  const bal = typeof verify.balanceUsd === 'number' ? ` · balance ${usd(verify.balanceUsd)}` : '';
  const region = freshestRegion(verify.region, opts.liveRegion);
  if (region?.status === 'blocked' || verify.region?.refusedOrder) {
    return { ...base, state: 'block', detail: `${REGION_BLOCKED_COPY}${regionWhere(region)}` };
  }
  const closeOnly = region?.status === 'close_only';
  if (closeOnly && opts.buys) {
    return { ...base, state: 'block', detail: `${REGION_CLOSE_ONLY_COPY}${regionWhere(region)}` };
  }
  const regionLines = new Set([verify.region?.message, region?.message].filter(Boolean));
  const fatal = (verify.fatalIssues ?? []).filter((f) => !regionLines.has(f));
  if (fatal.length) {
    return { ...base, state: 'block', detail: `Connected${bal}, but no order can go through yet: ${fatal.join('; ')}.` };
  }
  if (closeOnly) {
    return { ...base, state: 'warn', detail: `${REGION_CLOSE_ONLY_COPY}${regionWhere(region)}` };
  }
  if (verify.ready === false) {
    const rest = (verify.issues ?? []).filter((f) => !regionLines.has(f));
    const why = rest.length ? `: ${rest.join('; ')}` : '';
    return { ...base, state: 'warn', detail: `Connected${bal}, but Polymarket says it is not ready to trade${why}.` };
  }
  if (region?.status === 'unknown') {
    return {
      ...base, state: 'warn',
      detail: `Tested with Polymarket${bal}, but its region check could not be reached, so whether this connection may trade is unconfirmed.`,
    };
  }
  return { ...base, state: 'ok', detail: `Tested with Polymarket${bal}.` };
}

export type StepState = 'ok' | 'warn' | 'apply' | 'block';
export type StepId = 'keys' | 'record' | 'agents' | 'caps' | 'approval' | 'env';
export type AccountStepId = 'keys' | 'engines' | 'ack';

export interface GoLiveStep {
  id: StepId;
  state: StepState;
  title: string;
  detail: string;
}

export interface GoLiveInput {
  config: Cfg;
  creds: CredentialsStatusAll | null;
  verify: KeyVerify | null;
  record: AgentRecord;
  recordAck: boolean;
  caps: AgentCaps;
  approval: boolean;
  approvalOffAck: boolean;
  liveAgentIds: string[];
  region?: RegionStatus | null;
}

export interface GoLiveChecklist {
  steps: GoLiveStep[];
  ready: boolean;
}

export function goLiveChecklist(i: GoLiveInput): GoLiveChecklist {
  const steps: GoLiveStep[] = [
    walletState(i.creds, i.verify, { buys: true, liveRegion: i.region }), recordStep(i), agentsStep(i), capsStep(i), approvalStep(i), envStep(i),
  ];
  return { steps, ready: steps.every((s) => s.state !== 'block') };
}

export function liveCandidates(c: Cfg): McpAgent[] {
  return agentsOf(c).filter((a) => a.enabled);
}

export function defaultLiveSelection(c: Cfg): string[] {
  const cands = liveCandidates(c);
  const already = cands.filter((a) => a.mode === 'live').map((a) => a.id);
  if (already.length) return already;
  return cands.length === 1 ? [cands[0].id] : [];
}

function selectedAgents(c: Cfg, ids: string[]): McpAgent[] {
  return liveCandidates(c).filter((a) => ids.includes(a.id));
}

function agentsStep({ config, liveAgentIds }: GoLiveInput): GoLiveStep {
  const base = { id: 'agents' as const, title: 'Which agents go live' };
  const sel = selectedAgents(config, liveAgentIds);
  if (!sel.length) {
    return { ...base, state: 'block', detail: 'Tick at least one agent. Unticked agents keep trading on paper.' };
  }
  const rest = liveCandidates(config).length - sel.length;
  return {
    ...base, state: 'ok',
    detail: `${sel.map((a) => a.name).join(', ')} ${sel.length === 1 ? 'goes' : 'go'} live`
      + (rest ? `; ${rest} other${rest === 1 ? '' : 's'} stay${rest === 1 ? 's' : ''} on paper.` : '.'),
  };
}

function recordStep({ record, recordAck }: GoLiveInput): GoLiveStep {
  const base = { id: 'record' as const, title: "Your agents' record", detail: record.headline };
  if (!record.needsAck) return { ...base, state: 'ok' };
  return { ...base, state: recordAck ? 'warn' : 'block' };
}

function capsStep({ caps }: GoLiveInput): GoLiveStep {
  const problem = capsProblem(caps);
  const base = { id: 'caps' as const, title: 'Caps' };
  if (problem) return { ...base, state: 'block', detail: problem };
  return {
    ...base, state: 'ok',
    detail: `${usd(caps.mcpMaxOrderUsd)}/order · ${usd(caps.mcpDailySpendUsd)}/day · ${caps.mcpMaxPositions} positions · stop after ${usd(caps.mcpDailyLossUsd)} lost in a day`,
  };
}

function approvalStep({ approval, approvalOffAck }: GoLiveInput): GoLiveStep {
  const base = { id: 'approval' as const, title: 'Approvals' };
  if (approval) return { ...base, state: 'ok', detail: 'Every live order waits for your approval.' };
  if (approvalOffAck) {
    return { ...base, state: 'warn', detail: 'OFF: orders that pass the rails are sent without asking you.' };
  }
  return { ...base, state: 'block', detail: 'Turning approvals off needs its own confirmation.' };
}

function envStep({ config }: GoLiveInput): GoLiveStep {
  const base = { id: 'env' as const, title: 'Account mode' };
  if (config?.accountMode === 'live') {
    return { ...base, state: 'ok', detail: 'The app is already Live.' };
  }
  return { ...base, state: 'apply', detail: 'In Paper. Go live switches the whole app to Live — every armed engine listed below starts spending real money.' };
}

export function remoteTradingArmed(c: Cfg): boolean {
  return !!c?.remoteEnabled && !!c?.remoteTradingEnabled
    && !!(c.remoteDiscordEnabled || c.remoteTelegramEnabled);
}

export function reachableLiveAgents(c: Cfg): McpAgent[] {
  const live = liveAgents(c);
  if (c?.mcpEnabled) return live;
  if (c?.autopilotEnabled) return live.filter((a) => a.id === (c.autopilotAgentId || DEFAULT_AGENT_ID));
  return [];
}

export function liveSwitchLines(c: Cfg): string[] {
  if (!c) return [];
  const out: string[] = [];
  if (c.enableTrading) out.push('Main engine (whales + momentum): auto-trading on');
  if (c.crypto15mEnabled) out.push('Crypto (up/down markets): trading on');
  if (c.copyEnabled) out.push(`Copy trading: on${c.copyWallets?.length ? ` (${c.copyWallets.length} wallet${c.copyWallets.length === 1 ? '' : 's'})` : ''}`);
  if (c.perpsEnabled) out.push('Perps market maker: quoting on');
  if (c.scriptsLiveEnabled) out.push('Scripts: live orders on (armed scripts send real orders)');
  const reach = reachableLiveAgents(c);
  if (reach.length) {
    const approval = c.mcpLiveApproval === false ? 'approval OFF' : 'each order waits for your approval';
    const names = reach.map((a) => a.name);
    const who = names.length <= 3 ? names.join(', ') : `${names.slice(0, 3).join(', ')} +${names.length - 3}`;
    out.push(`AI agents LIVE: ${who} (${approval}${c.autopilotEnabled ? ', Autopilot on' : ''})`);
  }
  if (remoteTradingArmed(c)) out.push('Remote trading from Discord / Telegram');
  return out;
}

export function liveSwitchMessage(c: Cfg): string {
  const lines = liveSwitchLines(c).map((l) => `  • ${l}`);
  const head = 'Switch to LIVE (real money)?';
  if (!lines.length) {
    return `${head}\n\nNothing is armed to trade on its own. Orders you place in Live use real funds.`;
  }
  return `${head}\n\nThese are armed and will use REAL money as soon as you switch:\n`
    + `${lines.join('\n')}\n\nCancel and turn any of them off first if that is not what you want.`;
}

export interface AccountGoLiveInput {
  config: Cfg;
  creds: CredentialsStatusAll | null;
  verify: KeyVerify | null;
  armed: string[];
  ack: boolean;
  region?: RegionStatus | null;
}

export interface AccountGoLiveStep {
  id: AccountStepId;
  state: StepState;
  title: string;
  detail: string;
}

export function accountGoLiveChecklist(i: AccountGoLiveInput): { steps: AccountGoLiveStep[]; ready: boolean } {
  const keys = walletState(i.creds, i.verify, { buys: i.armed.length > 0, liveRegion: i.region });
  const engines: AccountGoLiveStep = i.armed.length
    ? { id: 'engines', state: 'warn', title: 'What starts using real money',
      detail: `${i.armed.length} armed: ${i.armed.join(' · ')}` }
    : { id: 'engines', state: 'ok', title: 'What starts using real money',
      detail: 'Nothing is armed to trade on its own. Orders you place yourself will use real funds.' };
  const ack: AccountGoLiveStep = i.ack
    ? { id: 'ack', state: 'ok', title: 'Real money', detail: 'You confirmed that Live orders spend the real USDC in your wallet.' }
    : { id: 'ack', state: 'block', title: 'Real money', detail: 'Tick the box to confirm Live orders spend the real USDC in your wallet.' };
  const steps: AccountGoLiveStep[] = [{ ...keys, id: 'keys' }, engines, ack];
  return { steps, ready: steps.every((s) => s.state !== 'block') };
}

export function accountGoLivePatch(): Partial<TraderConfig> {
  return { accountMode: 'live' };
}

export function backToPaperAccountPatch(): Partial<TraderConfig> {
  return { accountMode: 'paper' };
}

export interface GoLiveDraft {
  caps: AgentCaps;
  approval: boolean;
  approvalOffAck: boolean;
  liveAgentIds: string[];
}

export function goLivePatch(config: Cfg, draft: GoLiveDraft): Partial<TraderConfig> | null {
  if (capsProblem(draft.caps)) return null;
  const live = new Set(selectedAgents(config, draft.liveAgentIds).map((a) => a.id));
  if (!live.size) return null;
  const patch: Partial<TraderConfig> = { mcpTradeMode: 'live' };
  if (config?.accountMode !== 'live') patch.accountMode = 'live';
  const cur = currentCaps(config);
  for (const k of CAP_KEYS) {
    if (draft.caps[k] !== cur[k]) patch[k] = draft.caps[k];
  }
  patch.mcpLiveApproval = !(draft.approval === false && draft.approvalOffAck);
  patch.mcpAgents = agentsOf(config).map((a) => ({ ...a, mode: live.has(a.id) ? 'live' : 'paper' }));
  return patch;
}

export function goLiveAgentNames(config: Cfg, draft: Pick<GoLiveDraft, 'liveAgentIds'>): string[] {
  return selectedAgents(config, draft.liveAgentIds).map((a) => a.name);
}

export function backToPaperPatch(): Partial<TraderConfig> {
  return { mcpTradeMode: 'paper' };
}

export function goLiveSummary(draft: Omit<GoLiveDraft, 'liveAgentIds'> & { liveAgentIds?: string[] },
  agentNames: string[] = []): string {
  const ask = draft.approval || !draft.approvalOffAck
    ? 'each needing your approval'
    : 'WITHOUT asking you per order';
  const head = `Your agents can place real orders on Polymarket, up to ${usd(draft.caps.mcpDailySpendUsd)}/day `
    + `(${usd(draft.caps.mcpMaxOrderUsd)} per order, at most ${draft.caps.mcpMaxPositions} open positions, `
    + `buys stop after ${usd(draft.caps.mcpDailyLossUsd)} lost in a day), ${ask}.`;
  if (!agentNames.length) return head;
  return `${head} Live: ${agentNames.join(', ')}. Every other agent trades on paper, and each live `
    + 'agent\'s own rules and caps still apply on top.';
}

export function liveAgents(c: Cfg): McpAgent[] {
  if (c?.mcpTradeMode !== 'live') return [];
  return agentsOf(c).filter((a) => a.enabled && a.mode === 'live');
}

export function agentsLiveOnLive(c: Cfg): boolean {
  return c?.accountMode === 'live' && liveAgents(c).length > 0;
}

export function noAgentReachable(c: Cfg): boolean {
  return !c?.mcpEnabled && !c?.autopilotEnabled;
}
