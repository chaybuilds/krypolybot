import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type {
  ActionResult,
  AppState,
  BotPosition,
  CredentialsInput,
  CredentialsState,
  McpClientId,
  McpStatus,
  PositionFilter,
  Profile,
  ProfileScope,
  SignalFilter,
  TraderConfig,
} from '../shared/types';
import { setStartWithWindows } from './system/autostart';
import { startDiscordRpc, stopDiscordRpc } from './system/discord';
import { pythonBackend } from './system/python-backend';
import * as accounts from './system/accounts';
import * as mcpClients from './system/mcp-clients';
import * as store from './system/settings-store';
import { findStrategy, listStrategies } from './system/strategies';

const ok = <T>(data?: T, message?: string): ActionResult<T> => ({
  ok: true,
  data,
  message,
});
const err = (message: string): ActionResult => ({ ok: false, message });

function broadcastState(state: AppState): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('state:changed', state);
    }
  }
}

function genId(): string {
  return `p_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

function scopeOfKey(key: string): ProfileScope {
  if (key.startsWith('crypto15m')) return 'crypto';
  if (key.startsWith('copy')) return 'copy';
  if (key.startsWith('perps')) return 'perps';
  return 'main';
}

function normScope(s: unknown): ProfileScope {
  return s === 'crypto' || s === 'copy' || s === 'perps' ? s : 'main';
}

function scopeLabel(sc: ProfileScope): string {
  return sc === 'crypto' ? 'crypto' : sc === 'copy' ? 'copy-trading'
    : sc === 'perps' ? 'perps' : 'main-engine';
}

function activeKeyFor(scope: ProfileScope): keyof AppState {
  return scope === 'crypto'
    ? 'activeCryptoProfileId'
    : scope === 'copy'
      ? 'activeCopyProfileId'
      : scope === 'perps'
        ? 'activePerpsProfileId'
        : 'activeProfileId';
}

function scopedApplyPatch(cfg: TraderConfig, scope: ProfileScope): Partial<TraderConfig> {
  const patch: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(cfg)) {
    if (scopeOfKey(k) === scope) patch[k] = v;
  }
  if (scope === 'main') {
    delete patch.enableTrading;
    delete patch.network;
  } else if (scope === 'crypto') {
    delete patch.crypto15mEnabled;
  } else if (scope === 'perps') {
    delete patch.perpsEnabled;
  } else {
    delete patch.copyEnabled;
  }
  return patch as Partial<TraderConfig>;
}

async function pushConfigToBackend(): Promise<void> {
  if (!pythonBackend.isRunning()) return;
  const state = store.get();
  try {
    await pythonBackend.request('setConfig', { config: state.config });
  } catch (e) {
  }
}

export function registerIpc(): void {
  ipcMain.handle('app:version', () => app.getVersion());
  ipcMain.handle('app:openExternal', async (_e, url: string) => {
    if (typeof url === 'string' && /^(https?|mailto):/i.test(url)) {
      await shell.openExternal(url);
    }
  });
  ipcMain.handle('app:showItemInFolder', async (_e, p: string) => {
    shell.showItemInFolder(p);
  });
  ipcMain.handle('app:getUserDataPath', () => app.getPath('userData'));

  ipcMain.handle('accounts:current', () => accounts.currentAccount());
  ipcMain.handle('accounts:list', () => accounts.listAccounts());
  ipcMain.handle('accounts:create', (_e, name: string) => accounts.createAccount(name));
  ipcMain.handle('accounts:launch', (_e, name: string) => accounts.launchAccount(name));

  ipcMain.handle('state:get', () => store.get());
  ipcMain.handle('state:setStartMinimized', (_e, v: boolean) => {
    const next = store.save({ ...store.get(), startMinimized: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setStartWithWindows', (_e, v: boolean) => {
    setStartWithWindows(!!v);
    const next = store.save({ ...store.get(), startWithWindows: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setEnableDiscordRpc', (_e, v: boolean) => {
    const on = !!v;
    const next = store.save({ ...store.get(), enableDiscordRpc: on });
    if (on) void startDiscordRpc();
    else stopDiscordRpc();
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:acceptDisclaimer', () => {
    const next = store.save({ ...store.get(), acceptedDisclaimer: true });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setUiMode', (_e, v: 'simple' | 'advanced') => {
    const mode = v === 'advanced' ? 'advanced' : 'simple';
    const next = store.save({ ...store.get(), uiMode: mode });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:resetOnboarding', () => {
    const next = store.save({ ...store.get(), acceptedDisclaimer: false });
    broadcastState(next);
    return ok();
  });

  ipcMain.handle('config:get', () => store.get().config);
  ipcMain.handle('config:update', async (_e, patch: Partial<TraderConfig>) => {
    const next = store.patchConfig(patch);
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:replace', async (_e, cfg: TraderConfig) => {
    const next = store.replaceConfig(cfg);
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:reset', async () => {
    const next = store.resetConfig();
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:listStrategies', () => listStrategies());
  ipcMain.handle('config:applyStrategy', async (_e, id: string) => {
    const s = findStrategy(id);
    if (!s) return store.get().config;
    const cur = store.get();
    const PRESERVE_PREFIXES = ['crypto15m', 'copy', 'script', 'ai', 'remote', 'terminal', 'mcp', 'perps'];
    const PRESERVE_KEYS = new Set([
      'statsChartWindowHours',
      'lifetimeLossLimitPct',
      'lifetimeLossLimitUsd',
    ]);
    const preserved = Object.fromEntries(
      Object.entries(cur.config).filter(
        ([k]) => PRESERVE_PREFIXES.some((p) => k.startsWith(p)) || PRESERVE_KEYS.has(k),
      ),
    ) as Partial<TraderConfig>;
    const next = store.replaceConfig({
      ...s.config,
      ...preserved,
      network: cur.config.network,
      enableTrading: cur.config.enableTrading,
    });
    const stateNext = store.save({ ...store.get(), activeProfileId: id });
    broadcastState(stateNext);
    await pushConfigToBackend();
    return next.config;
  });

  ipcMain.handle('profiles:list', () => store.get().customProfiles);
  ipcMain.handle('profiles:save', (_e, name: string, description?: string, scope?: ProfileScope) => {
    if (!name?.trim()) return err('Profile name required');
    const sc = normScope(scope);
    const cur = store.get();
    const now = new Date().toISOString();
    const profile: Profile = {
      id: genId(),
      name: name.trim(),
      description: description?.trim() || undefined,
      scope: sc,
      createdAt: now,
      updatedAt: now,
      config: structuredClone(cur.config),
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, profile],
      [activeKeyFor(sc)]: profile.id,
    });
    broadcastState(next);
    return ok(profile, `Saved ${scopeLabel(sc)} profile "${profile.name}"`);
  });
  ipcMain.handle('profiles:apply', async (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) {
      const s = findStrategy(id);
      if (s) {
        const next = store.patchConfig(scopedApplyPatch(s.config, 'main'));
        const stateNext = store.save({ ...store.get(), activeProfileId: id });
        broadcastState(stateNext);
        await pushConfigToBackend();
        return ok(next.config, `Applied "${s.name}"`);
      }
      return err('Profile not found');
    }
    const sc = normScope(p.scope);
    const next = store.patchConfig(scopedApplyPatch(p.config, sc));
    const stateNext = store.save({ ...store.get(), [activeKeyFor(sc)]: id });
    broadcastState(stateNext);
    await pushConfigToBackend();
    return ok(next.config, `Applied ${scopeLabel(sc)} profile "${p.name}"`);
  });
  ipcMain.handle('profiles:rename', (_e, id: string, name: string) => {
    if (!name?.trim()) return err('Name required');
    const cur = store.get();
    const idx = cur.customProfiles.findIndex((p) => p.id === id);
    if (idx < 0) return err('Profile not found');
    const updated = [...cur.customProfiles];
    updated[idx] = { ...updated[idx], name: name.trim(), updatedAt: new Date().toISOString() };
    const next = store.save({ ...cur, customProfiles: updated });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:delete', (_e, id: string) => {
    const cur = store.get();
    const next = store.save({
      ...cur,
      customProfiles: cur.customProfiles.filter((p) => p.id !== id),
      activeProfileId: cur.activeProfileId === id ? null : cur.activeProfileId,
      activeCryptoProfileId: cur.activeCryptoProfileId === id ? null : cur.activeCryptoProfileId,
      activeCopyProfileId: cur.activeCopyProfileId === id ? null : cur.activeCopyProfileId,
      activePerpsProfileId: cur.activePerpsProfileId === id ? null : cur.activePerpsProfileId,
    });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:duplicate', (_e, id: string) => {
    const cur = store.get();
    const orig = cur.customProfiles.find((p) => p.id === id);
    if (!orig) return err('Profile not found');
    const dup: Profile = {
      ...orig,
      id: genId(),
      name: `${orig.name} (copy)`,
      config: structuredClone(orig.config),
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, dup],
    });
    broadcastState(next);
    return ok(dup);
  });
  ipcMain.handle('profiles:export', (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) return err('Profile not found');
    const json = JSON.stringify(
      { kryptTraderProfile: 1, profile: p },
      null,
      2,
    );
    return ok(json);
  });
  ipcMain.handle('profiles:import', (_e, json: string) => {
    try {
      const parsed = JSON.parse(json);
      const norm = store.mergeProfile(parsed?.profile);
      if (!norm) return err('Not a valid Krypt PolyBot profile');
      const cur = store.get();
      const dup: Profile = {
        ...norm,
        id: genId(),
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      const next = store.save({ ...cur, customProfiles: [...cur.customProfiles, dup] });
      broadcastState(next);
      return ok(dup, `Imported "${dup.name}"`);
    } catch (e: any) {
      return err(`Invalid profile JSON: ${e?.message || e}`);
    }
  });

  const emptyCred = () => ({
    env: 'mainnet' as const, hasWalletKey: false, hasApiCreds: false,
    address: '', addressPreview: '',
  });
  const emptyAllCreds = () => ({
    current: 'mainnet' as const,
    mainnet: emptyCred(),
  });
  ipcMain.handle('credentials:status', async () => {
    if (!pythonBackend.isRunning()) {
      return emptyCred() satisfies CredentialsState;
    }
    const all = await pythonBackend.request('credentialStatus', {}) as any;
    if (all && all.current && all[all.current]) {
      return all[all.current] as CredentialsState;
    }
    return all as CredentialsState;
  });
  ipcMain.handle('credentials:statusAll', async () => {
    if (!pythonBackend.isRunning()) return emptyAllCreds();
    return await pythonBackend.request('credentialStatus', {});
  });
  ipcMain.handle('credentials:save', async (_e, input: CredentialsInput) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('setCredentials', input);
      return ok();
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('credentials:test', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('testCredentials', env ? { env } : {});
      return ok(data, 'Connected to Polymarket');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('credentials:clear', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('clearCredentials', env ? { env } : {});
      return ok();
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('backend:info', () => pythonBackend.info());
  ipcMain.handle('backend:start', async () => {
    await pythonBackend.start();
    return ok();
  });
  ipcMain.handle('backend:stop', async () => {
    await pythonBackend.stop();
    return ok();
  });
  ipcMain.handle('backend:restart', async () => {
    await pythonBackend.restart();
    return ok();
  });
  ipcMain.handle('backend:runOnce', async (_e, action: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('runOnce', { action });
      return ok(data, (data as any)?.summary || 'Done');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('trading:setEnabled', async (_e, enabled: boolean) => {
    const next = store.patchConfig({ enableTrading: !!enabled });
    broadcastState(next);
    await pushConfigToBackend();
    return ok();
  });
  ipcMain.handle('trading:cancelAllOpen', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('cancelAllOpen', {});
      return ok(data, `Canceled ${data.canceled} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('trading:flatten', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('flatten', {});
      return ok(data, `Flattened ${data.closed} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('trading:status', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        main: [{ id: 'backend', label: 'Backend running', state: 'blocked', reason: 'backend not running' }],
        mainFilterCounts: {}, mainCandidates: 0, mainPlaced: 0,
        c15: { enabled: false, live: false, authed: false, env: 'mainnet', blockReasons: {} },
      };
    }
    return await pythonBackend.request('tradingStatus', {});
  });

  ipcMain.handle('app:factoryReset', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('factoryReset', {}) as any;
      const total = Object.values(data?.deleted || {}).reduce(
        (a: number, b: any) => a + (Number(b) || 0), 0,
      );
      return ok(data, `Cleared ${total} row(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('app:clearHistory', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('clearHistory', {}) as any;
      const total = Object.values(data?.deleted || {}).reduce(
        (a: number, b: any) => a + (Number(b) || 0), 0,
      );
      return ok(data, `Cleared ${total} row(s) of history`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('data:account', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        backendDown: true,
        cashUsd: 0, portfolioUsd: 0, totalUsd: 0,
        startBankrollUsd: store.get().config.startBankrollUsd,
        roiPct: 0, realizedPnlUsd: 0, unrealizedPnlUsd: 0,
        openCostUsd: 0, feesUsd: 0, wins: 0, losses: 0, winRate: 0,
        pendingCount: 0, openCount: 0, resolvedCount: 0, totalOpened: 0,
        byNetwork: {
          mainnet: { wins: 0, losses: 0, realizedPnl: 0 },
        },
      };
    }
    return await pythonBackend.request('account', {});
  });
  ipcMain.handle('data:pnlSeries', async (_e, sinceHours?: number) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('pnlSeries', { sinceHours });
  });
  ipcMain.handle('data:positions', async (_e, filter?: PositionFilter) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('positions', filter || {});
  });
  ipcMain.handle('data:signals', async (_e, filter?: SignalFilter) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('signals', filter || {});
  });
  ipcMain.handle('data:scannerStats', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        whales: { total: 0, sent: 0, resolved: 0, winRate: 0 },
        momentum: { total: 0, sent: 0, resolved: 0, winRate: 0 },
        marketsTracked: 0,
        lastWhaleScanAt: null,
        lastMomentumScanAt: null,
        lastTradeScanAt: null,
      };
    }
    return await pythonBackend.request('scannerStats', {});
  });
  ipcMain.handle('data:botRuns', async (_e, env?: string | null, limit?: number) => {
    if (!pythonBackend.isRunning()) {
      return { runs: [], activeRunId: 0, activeRun: null };
    }
    const params: Record<string, unknown> = {};
    if (env) params.env = env;
    if (limit) params.limit = limit;
    return await pythonBackend.request('botRuns', params);
  });

  const dataDown = { ok: false, message: 'Backend is not running.' };
  ipcMain.handle('data:userPnl', async (_e, interval?: string) => {
    if (!pythonBackend.isRunning()) return dataDown;
    return await pythonBackend.request('dataUserPnl', { interval }, 30_000);
  });
  ipcMain.handle('data:resolutions', async (_e, conditionIds?: string[]) => {
    if (!pythonBackend.isRunning()) return { ...dataDown, resolutions: {} };
    return await pythonBackend.request('dataResolutions', { conditionIds: conditionIds || [] }, 30_000);
  });
  ipcMain.handle('data:holders', async (_e, conditionId?: string, limit?: number) => {
    if (!pythonBackend.isRunning()) return { ...dataDown, holders: [] };
    return await pythonBackend.request('dataHolders', { conditionId, limit }, 30_000);
  });
  ipcMain.handle('data:biggestWinners', async (_e, period?: string, category?: string) => {
    if (!pythonBackend.isRunning()) return { ...dataDown, winners: [] };
    return await pythonBackend.request('dataBiggestWinners', { period, category }, 30_000);
  });

  const perpsDown = { ok: false, message: 'Backend is not running.' };
  ipcMain.handle('perps:board', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        ok: false, error: 'Backend is not running.', markets: [], poolPerMarketUsd: 0,
        dailyPoolUsd: 0, activeMarkets: 0, exchangeVol7dUsd: 0, volumeCoverage: 0,
        makerVol7dNeededUsd: 0, myQuoteScore: 0, fetchedAt: Date.now(),
      };
    }
    return await pythonBackend.request('perpsBoard', {}, 30_000);
  });
  ipcMain.handle('perps:status', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsStatus', {});
  });
  ipcMain.handle('perps:link', async () => {
    if (!pythonBackend.isRunning()) return perpsDown;
    return await pythonBackend.request('perpsLink', {}, 30_000);
  });
  ipcMain.handle('perps:unlink', async () => {
    if (!pythonBackend.isRunning()) return perpsDown;
    return await pythonBackend.request('perpsUnlink', {}, 30_000);
  });
  ipcMain.handle('perps:flatten', async () => {
    if (!pythonBackend.isRunning()) return perpsDown;
    return await pythonBackend.request('perpsFlatten', {}, 60_000);
  });
  ipcMain.handle('perps:backtest', async (_e, args?: Record<string, unknown>) => {
    if (!pythonBackend.isRunning()) return perpsDown;
    return await pythonBackend.request('perpsBacktest', args || {}, 900_000);
  });
  ipcMain.handle('perps:cancelAll', async () => {
    if (!pythonBackend.isRunning()) return perpsDown;
    return await pythonBackend.request('perpsCancelAll', {}, 30_000);
  });

  ipcMain.handle('crypto15m:snapshot', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        fetchedAt: new Date().toISOString(),
        spotOk: false,
        spotSource: 'unavailable',
        constants: {
          timeDelayMin: 8, entryThreshold: 0.95, entryMax: 0.98,
          exitThreshold: 0.4, minDeltaPct: 0, entryDiff: 0.02, directionMode: 'favorite',
          entryStyle: 'maker', hoursStartUtc: 0, hoursEndUtc: 24,
        },
        assets: [],
      };
    }
    return await pythonBackend.request('crypto15m', {});
  });
  ipcMain.handle('crypto15m:status', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        enabled: false, authed: false, trading: false,
        orderSize: 1, maxConcurrent: 7, env: 'mainnet',
        sizing: {
          mode: 'fixed', balancePct: 0.02, maxLossPct: 0, balanceUsd: 0,
          estPriceCents: 0, estContracts: 1, estCostUsd: 0, note: '',
        },
        stats: { openCount: 0, wins: 0, losses: 0, realizedPnlUsd: 0, total: 0 },
        open: [], recent: [],
      };
    }
    return await pythonBackend.request('crypto15mStatus', {});
  });
  ipcMain.handle('crypto15m:history', async (_e, opts?: { limit?: number }) => {
    if (!pythonBackend.isRunning()) return { rows: [] };
    return await pythonBackend.request('c15History', { limit: opts?.limit ?? 300 });
  });
  ipcMain.handle('crypto15m:backtest', async (_e, args?: { sinceDays?: number; config?: Record<string, unknown> }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('c15Backtest', args || {}, 120_000);
  });
  ipcMain.handle('main:backtest', async (_e, args?: { sinceDays?: number; config?: Record<string, unknown> }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('mainBacktest', args || {}, 120_000);
  });
  ipcMain.handle('backtest:collection', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('collectionStats', {});
  });
  ipcMain.handle('backtest:export', async () => {
    if (!pythonBackend.isRunning()) return null;
    const r = await pythonBackend.request('exportResearch', {}, 120_000) as { dir?: string } | null;
    if (r?.dir) shell.showItemInFolder(r.dir);
    return r;
  });
  ipcMain.handle('scripts:list', async () => {
    if (!pythonBackend.isRunning()) return { scripts: [] };
    return await pythonBackend.request('scriptsList', {});
  });
  ipcMain.handle('scripts:save', async (_e, s: Record<string, unknown>) => {
    return await pythonBackend.request('scriptSave', s || {});
  });
  ipcMain.handle('scripts:delete', async (_e, id: string) => {
    return await pythonBackend.request('scriptDelete', { id });
  });
  ipcMain.handle('scripts:setEnabled', async (_e, id: string, enabled: boolean) => {
    return await pythonBackend.request('scriptSetEnabled', { id, enabled });
  });
  ipcMain.handle('scripts:setAssets', async (_e, id: string, assets: string[] | null) => {
    return await pythonBackend.request('scriptSetAssets', { id, assets });
  });
  ipcMain.handle('scripts:exportFile', async (e, name: string, code: string) => {
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const safe = (name || 'strategy').replace(/[^\w.-]+/g, '-').slice(0, 60);
    const res = await dialog.showSaveDialog(win!, {
      title: 'Export strategy script',
      defaultPath: join(app.getPath('documents'), `${safe}.py`),
      filters: [{ name: 'Python script', extensions: ['py'] }],
    });
    if (res.canceled || !res.filePath) return err('canceled');
    writeFileSync(res.filePath, code, 'utf-8');
    shell.showItemInFolder(res.filePath);
    return ok(res.filePath);
  });
  ipcMain.handle('scripts:importFile', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const res = await dialog.showOpenDialog(win!, {
      title: 'Import strategy script',
      properties: ['openFile'],
      filters: [{ name: 'Python script', extensions: ['py', 'txt'] }],
    });
    if (res.canceled || !res.filePaths?.[0]) return err('canceled');
    try {
      const code = readFileSync(res.filePaths[0], 'utf-8');
      if (code.length > 128 * 1024) return err('Script exceeds 128KB');
      return ok(code);
    } catch (e2: any) {
      return err(e2?.message || 'Could not read that file');
    }
  });
  ipcMain.handle('scripts:setDryRun', async (_e, id: string, dryRun: boolean) => {
    return await pythonBackend.request('scriptSetDryRun', { id, dryRun });
  });
  ipcMain.handle('scripts:shadowOrders', async (_e, id: string, limit?: number) => {
    return await pythonBackend.request('scriptShadowOrders', { id, limit });
  });
  ipcMain.handle('scripts:validate', async (_e, code: string) => {
    return await pythonBackend.request('scriptValidate', { code });
  });
  ipcMain.handle('scripts:backtest', async (_e, args?: Record<string, unknown>) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('scriptBacktest', args || {}, 120_000);
  });
  ipcMain.handle('scripts:contextPack', async () => {
    return await pythonBackend.request('scriptContextPack', {}, 60_000);
  });
  ipcMain.handle('scripts:docs', async () => {
    return await pythonBackend.request('scriptApiDocs', {}, 60_000);
  });
  ipcMain.handle('scripts:exportPack', async (e) => {
    if (!pythonBackend.isRunning()) return err('Engine not running');
    const r = (await pythonBackend.request('scriptContextPack', {})) as { text?: string } | null;
    if (!r?.text) return err('Context pack generation failed');
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const res = await dialog.showSaveDialog(win!, {
      title: 'Save AI context pack',
      defaultPath: join(app.getPath('documents'), 'krypt-ai-context-pack.txt'),
      filters: [{ name: 'Text file', extensions: ['txt'] }],
    });
    if (res.canceled || !res.filePath) return err('canceled');
    writeFileSync(res.filePath, r.text, 'utf-8');
    shell.showItemInFolder(res.filePath);
    return ok(res.filePath);
  });

  ipcMain.handle('copy:status', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        enabled: false, authed: false, trading: false,
        wallets: [], openCopies: 0, todayPnlUsd: 0, lossLimitHit: false,
        sizing: { mode: 'fixed', fixedUsd: 10, balancePct: 0.02 },
        maxConcurrent: 10, entryMaxCents: 95,
      };
    }
    return await pythonBackend.request('copyStatus', {});
  });
  ipcMain.handle(
    'polymarket:marketUrl',
    async (_e, args?: { eventTicker?: string; ticker?: string; env?: string }) => {
      if (!pythonBackend.isRunning()) return { url: '' };
      return await pythonBackend.request('polymarketUrl', args || {});
    },
  );
  const termDown = (what: string) =>
    `The trading backend is not running, so ${what} could not be read. ` +
    `Start it from the status bar.`;

  const term = async <T>(method: string, params: unknown, offline: T): Promise<T> => {
    if (!pythonBackend.isRunning()) return offline;
    return (await pythonBackend.request(method, params ?? {})) as T;
  };

  ipcMain.handle(
    'terminal:discover',
    async (_e, args?: { column?: string; limit?: number; filters?: unknown }) =>
      term('terminalDiscover', args, {
        column: args?.column ?? 'volume', rows: [],
        fetchedAt: new Date().toISOString(),
        skippedForUnknown: 0, excluded: 0,
        note: termDown('the market list'),
      }),
  );
  ipcMain.handle(
    'terminal:search',
    async (_e, args?: { query?: string; limit?: number }) =>
      term('terminalSearch', args, {
        column: 'volume', rows: [], fetchedAt: new Date().toISOString(),
        skippedForUnknown: 0, excluded: 0, note: termDown('search'),
      }),
  );
  ipcMain.handle(
    'terminal:market',
    async (_e, args?: { conditionId?: string }) =>
      term('terminalMarket', args, { market: null }),
  );
  ipcMain.handle(
    'terminal:book',
    async (_e, args?: { conditionId?: string; side?: string }) =>
      term('terminalBook', args, {
        conditionId: args?.conditionId ?? '', tokenSide: args?.side ?? 'yes',
        bids: [], asks: [], bestBid: null, bestAsk: null,
        spreadCents: null, midCents: null, bidDepthUsd: null, askDepthUsd: null,
        available: false, fetchedAt: new Date().toISOString(), source: 'clob',
        note: termDown('the order book'),
      }),
  );
  ipcMain.handle(
    'terminal:history',
    async (_e, args?: { conditionId?: string; side?: string; interval?: string }) =>
      term('terminalHistory', args, {
        conditionId: args?.conditionId ?? '', side: args?.side ?? 'yes',
        interval: args?.interval ?? '1d', points: [], source: 'clob',
        ownFeed: false, thin: false, note: termDown('price history'),
      }),
  );
  ipcMain.handle(
    'terminal:tape',
    async (_e, args?: { conditionId?: string; limit?: number }) =>
      term('terminalTape', args, {
        conditionId: args?.conditionId ?? '', trades: [],
        fetchedAt: new Date().toISOString(), note: termDown('the trade tape'),
      }),
  );
  ipcMain.handle(
    'terminal:resolution',
    async (_e, args?: { conditionId?: string }) =>
      term('terminalResolution', args, {
        conditionId: args?.conditionId ?? '', checks: [], resolvedCount: 0,
        totalCount: 0, score: null,
        scoreNote: termDown('the resolution checks'),
        resolverName: null, resolutionSource: null,
        fetchedAt: new Date().toISOString(),
      }),
  );
  ipcMain.handle(
    'terminal:micro',
    async (_e, args?: { conditionId?: string; side?: string }) =>
      term('terminalMicro', args, {
        conditionId: args?.conditionId ?? '', side: args?.side ?? 'yes',
        points: [], medianSpreadCents: null, sampleCount: 0,
        source: 'clob-ws', note: termDown('our own feed'),
      }),
  );
  ipcMain.handle(
    'terminal:related',
    async (_e, args?: { conditionId?: string; limit?: number }) =>
      term('terminalRelated', args, {
        conditionId: args?.conditionId ?? '', eventSlug: null, rows: [],
        sumProbability: null, negRisk: null,
        fetchedAt: new Date().toISOString(),
        note: termDown('related markets'),
      }),
  );
  ipcMain.handle(
    'terminal:crossVenue',
    async (_e, args?: { conditionId?: string; interval?: string }) =>
      term('terminalCrossVenue', args, {
        conditionId: args?.conditionId ?? '', interval: args?.interval ?? '1d',
        polymarket: null, kalshi: null, match: null,
        spreadCents: null, spreadNote: null, series: [], overlapPeriods: 0,
        historyNote: null,
        venueNote: '', caveats: [],
        fetchedAt: new Date().toISOString(),
        note: termDown('the Kalshi comparison'),
      }),
  );
  ipcMain.handle('ai:status', async () =>
    term('aiStatus', {}, {
      enabled: false, provider: 'anthropic' as const, webSearch: false,
      offline: false,
      models: { anthropic: '', openai: '' },
      keys: {
        anthropic: { present: false, masked: null },
        openai: { present: false, masked: null },
      },
    }),
  );
  ipcMain.handle(
    'ai:analyze',
    async (_e, args?: { conditionId?: string }) =>
      term('aiAnalyze', args, {
        ok: false, provider: 'anthropic', model: '',
        askedAt: new Date().toISOString(), latencyMs: null,
        webSearch: false, sources: [],
        summary: null, resolutionNotes: [], drivers: [],
        unknowns: [], reasoning: [], lean: null, confidence: null,
        redactions: 0,
        reason: termDown('the analysis'),
        fix: null,
      }),
  );
  ipcMain.handle(
    'ai:setKey',
    async (_e, args?: { provider?: string; key?: string }) =>
      term('aiSetKey', args, {
        ok: false,
        message: 'The trading backend is not running, so the key could not be verified.',
      }),
  );
  ipcMain.handle(
    'ai:listModels',
    async (_e, args?: { provider?: string }) =>
      term('aiListModels', args, {
        ok: false, models: [],
        message: termDown('the model list'),
      }),
  );

  ipcMain.handle('terminal:health', async () =>
    term('terminalHealth', {}, {
      providers: [], offline: false, fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle(
    'terminal:watchlist',
    async (_e, args?: { action?: string; conditionId?: string; title?: string }) =>
      term('terminalWatchlist', args, { entries: [] }),
  );

  ipcMain.handle('terminal:preview', async (_e, order?: unknown) =>
    term('terminalPreview', { order }, {
      conditionId: '', side: 'yes', action: 'buy',
      priceCents: null, requestedPriceCents: 0, size: null, requestedSize: 0,
      costUsd: null, fillableSize: null, estimatedAvgCents: null,
      slippageCents: null, feeUsd: null, maxPayoutUsd: null, maxLossUsd: null,
      tickSize: null, minOrderSize: null,
      bestBidCents: null, bestAskCents: null, crossesNow: null,
      availableUsd: null,
      warnings: [],
      blockers: [termDown('this order')],
    }),
  );
  ipcMain.handle('terminal:place', async (_e, order?: unknown) =>
    term('terminalPlace', { order }, {
      ok: false, orderId: null, status: null, filledSize: null,
      avgPriceCents: null, localId: null,
      message: termDown('this order'),
    }),
  );
  ipcMain.handle('terminal:cancel', async (_e, orderId?: string) =>
    term('terminalCancel', { orderId }, {
      ok: false, message: termDown('the cancel'),
    }),
  );
  ipcMain.handle('terminal:orders', async (_e, args?: { conditionId?: string }) =>
    term('terminalOrders', args ?? {}, {
      orders: [], fetchedAt: new Date().toISOString(),
      note: termDown('your resting orders'),
    }),
  );
  ipcMain.handle('terminal:flattenPreview', async () =>
    term('terminalFlattenWallet', { preview: true }, {
      ok: false, positions: [], count: 0, skipped: 0, settledSkipped: 0,
      proceedsUsd: null, engineTracked: null, paper: false,
      message: termDown('what a flatten would sell'),
    }),
  );
  ipcMain.handle('terminal:flattenWallet', async () =>
    term('terminalFlattenWallet', {}, {
      ok: false, attempted: 0, sold: 0, refused: 0, skipped: 0,
      settledSkipped: 0,
      proceedsUsd: null, engineTrackedSold: null, reason: 'flatten',
      results: [], message: termDown('the flatten'),
    }),
  );
  ipcMain.handle('terminal:sellPreview', async (_e, order?: unknown) =>
    term('terminalSellPreview', { order }, {
      conditionId: '', side: 'yes', sideLabel: null, title: null,
      paper: false, positionSize: null, requestedSize: null,
      size: null, fillableSize: null, bestBidCents: null, limitCents: null,
      estimatedAvgCents: null, worstFillCents: null, maxSlippageCents: 5,
      proceedsUsd: null, feeUsd: null, netProceedsUsd: null,
      avgCostCents: null, basisSource: null, costBasisUsd: null,
      estimatedPnlUsd: null, markCents: null, settled: false,
      resolvedOutcome: null, minOrderSize: null, tickSize: null,
      warnings: [],
      blockers: [termDown('this position')],
      note: 'Nothing was priced.',
    }),
  );
  ipcMain.handle('terminal:sellPosition', async (_e, order?: unknown) =>
    term('terminalSellPosition', { order }, {
      ok: false, orderId: null, status: null, localId: null,
      sentSize: null, limitCents: null, filledSize: null, avgPriceCents: null,
      proceedsUsd: null, killedSize: null, estimatedProceedsUsd: null,
      preview: null,
      message: termDown('this sell'),
    }),
  );
  ipcMain.handle('terminal:portfolio', async () =>
    term('terminalPortfolio', {}, {
      positions: [], walletReadable: false,
      totalValueUsd: null, totalCostUsd: null,
      totalUnrealizedPnlUsd: null, realizedPnlUsd: null, settledPnlUsd: null,
      venueCashPnlUsd: null,
      unreconciledCount: 0, unreconciledNote: termDown('your positions'),
      venueBasisCount: 0, settledWorthlessCount: 0,
      redeemableCount: 0, redeemableValueUsd: null, redeemableNote: null,
      cashUsd: null, fetchedAt: new Date().toISOString(),
      costBasisConvention: 'average',
    }),
  );
  ipcMain.handle('terminal:settled', async () =>
    term('terminalSettled', {}, {
      rows: [], totals: {
        won: 0, lost: 0, bothSides: 0, closedOut: 0, unknown: 0,
        markets: 0, settledMarkets: 0, rows: 0, pnlUsd: null,
        pnlMissingRows: 0, feesUsd: null, feesKnown: true, unknownRows: 0,
      },
      walletReadable: false, fetchedAt: new Date().toISOString(),
      note: termDown('your settled record'),
    }),
  );
  ipcMain.handle('terminal:fills', async (_e, limit?: number) =>
    term('terminalFills', { limit }, {
      fills: [], fetchedAt: new Date().toISOString(),
      note: termDown('your fill ledger'),
    }),
  );
  ipcMain.handle('terminal:orderLog', async (_e, limit?: number) =>
    term('terminalOrderLog', { limit }, {
      orders: [], fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle('terminal:reconcile', async () =>
    term('terminalReconcile', {}, {
      inserted: 0, matched: 0, corrected: 0, unmapped: 0,
      scanned: 0, complete: false,
      note: termDown('the reconcile'),
    }),
  );
  ipcMain.handle(
    'terminal:standing',
    async (_e, args?: { action?: string; id?: string; armed?: boolean; item?: unknown }) =>
      term('terminalStanding', args, {
        instructions: [], error: termDown('your standing instructions'),
      }),
  );

  ipcMain.handle('terminal:paperPlace', async (_e, order?: unknown) =>
    term('terminalPaperPlace', { order }, {
      ok: false, localId: null, status: null, filledSize: null,
      avgPriceCents: null, message: termDown('this paper order'),
    }),
  );
  ipcMain.handle('terminal:paperPortfolio', async () =>
    term('terminalPaperPortfolio', {}, {
      positions: [], walletReadable: false,
      totalValueUsd: null, totalCostUsd: null,
      totalUnrealizedPnlUsd: null, realizedPnlUsd: null, settledPnlUsd: null,
      unreconciledCount: 0, unreconciledNote: termDown('your paper account'),
      redeemableCount: 0, redeemableValueUsd: null, redeemableNote: null,
      cashUsd: null, bankrollUsd: 0, equityUsd: null, returnPct: null,
      fetchedAt: new Date().toISOString(), costBasisConvention: 'average',
    }),
  );
  ipcMain.handle('terminal:paperOrders', async (_e, limit?: number) =>
    term('terminalPaperOrders', { limit }, {
      orders: [], fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle('terminal:paperCancel', async (_e, localId?: number) =>
    term('terminalPaperCancel', { localId }, {
      ok: false, message: termDown('the cancel'),
    }),
  );
  ipcMain.handle('terminal:paperReset', async () =>
    term('terminalPaperReset', {}, {
      cleared: 0, message: termDown('the reset'),
    }),
  );

  const remoteDown = {
    discord: { connected: false, lastError: null, paired: [], hasToken: false },
    telegram: { connected: false, lastError: null, paired: [], hasToken: false },
    audit: [], fetchedAt: new Date().toISOString(),
  };
  ipcMain.handle('remote:status', async () =>
    term('remoteStatus', {}, remoteDown));
  ipcMain.handle(
    'remote:setToken',
    async (_e, args?: { platform?: string; token?: string }) =>
      term('remoteSetToken', args, {
        ok: false, message: termDown('the bot token'),
      }),
  );
  ipcMain.handle('remote:pairCode', async (_e, args?: { platform?: string }) =>
    term('remotePairCode', args, { code: '', expiresAt: '', ttlSec: 0 }));
  ipcMain.handle(
    'remote:addPaired',
    async (_e, args?: { platform?: string; id?: string; label?: string }) =>
      term('remoteAddPaired', args, {
        paired: [], error: termDown('the pairing'),
      }),
  );
  ipcMain.handle(
    'remote:unpair',
    async (_e, args?: { platform?: string; id?: string }) =>
      term('remoteUnpair', args, { paired: [] }),
  );
  ipcMain.handle('remote:sync', async () => term('remoteSync', {}, remoteDown));
  ipcMain.handle('remote:test', async () =>
    term('remoteTest', {}, { sent: 0, message: termDown('the test message') }));

  const mcpDown = (): McpStatus => ({
    enabled: false, running: false, port: 0, url: '', lastError: termDown('the AI agent server'),
    hasToken: false, tokenMasked: null, sessions: [], toolCount: 0,
    spentTodayUsd: null, audit: [], fetchedAt: new Date().toISOString(),
  });
  ipcMain.handle('mcp:status', async () => term('mcpStatus', {}, mcpDown()));
  ipcMain.handle('mcp:sync', async () => term('mcpSync', {}, mcpDown()));
  ipcMain.handle('mcp:rotateToken', async () => term('mcpRotateToken', {}, mcpDown()));
  ipcMain.handle('mcp:revealToken', async () => term('mcpRevealToken', {}, { token: '' }));
  ipcMain.handle('mcp:paper', async (_e, args?: { reset?: boolean }) =>
    term('mcpPaper', { reset: !!args?.reset }, { portfolio: null, orders: null }));
  const mcpUrl = () => `http://127.0.0.1:${store.get().config.mcpPort ?? 47615}/mcp`;
  ipcMain.handle('mcp:clients', async () =>
    mcpClients.listClients(pythonBackend.relaySpec(), mcpUrl()));
  ipcMain.handle('mcp:install', async (_e, args?: { id?: McpClientId }) =>
    args?.id
      ? mcpClients.install(args.id, pythonBackend.relaySpec())
      : { ok: false, message: 'No client selected.' });
  ipcMain.handle('mcp:uninstall', async (_e, args?: { id?: McpClientId }) =>
    args?.id ? mcpClients.uninstall(args.id) : { ok: false, message: 'No client selected.' });

  ipcMain.handle('logs:tail', async (_e, _limit?: number) => {
    return logsBuffer.slice(-1 * (_limit || 500));
  });
  ipcMain.handle('logs:clear', () => {
    logsBuffer.length = 0;
    return ok();
  });
  ipcMain.handle('logs:openFolder', async () => {
    const p = join(app.getPath('userData'), 'logs');
    if (existsSync(p)) shell.openPath(p);
  });

  ipcMain.on('window:minimize', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.minimize();
  });
  ipcMain.on('window:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w) return;
    if (w.isMaximized()) w.unmaximize();
    else w.maximize();
  });
  ipcMain.on('window:close', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.close();
  });
  ipcMain.handle('window:isMaximized', (e) => {
    return BrowserWindow.fromWebContents(e.sender)?.isMaximized() ?? false;
  });
}

const MAX_LOGS = 5000;
export const logsBuffer: any[] = [];

export function appendLog(entry: any): void {
  logsBuffer.push(entry);
  if (logsBuffer.length > MAX_LOGS) logsBuffer.shift();
}

export { broadcastState };
