import { contextBridge, ipcRenderer } from 'electron';
import type {
  AccountSnapshot, ActionResult, AppState, BackendInfo, BotPosition, CredentialsInput,
  CredentialsState, KryptApi, LogEntry, PnlPoint, PositionFilter, Profile,
  ScannerStats, SignalFilter, SignalRow, StrategyPreset, TraderConfig,
} from '../shared/types';

const sub = <T>(channel: string, cb: (val: T) => void): (() => void) => {
  const handler = (_e: unknown, val: T) => cb(val);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

const api: KryptApi = {
  app: {
    version: () => ipcRenderer.invoke('app:version'),
    openExternal: (url) => ipcRenderer.invoke('app:openExternal', url),
    showItemInFolder: (p) => ipcRenderer.invoke('app:showItemInFolder', p),
    getUserDataPath: () => ipcRenderer.invoke('app:getUserDataPath'),
    factoryReset: () => ipcRenderer.invoke('app:factoryReset'),
    clearHistory: () => ipcRenderer.invoke('app:clearHistory'),
    onDataReset: (cb) => sub<unknown>('data:reset', cb),
  },
  accounts: {
    current: () => ipcRenderer.invoke('accounts:current'),
    list: () => ipcRenderer.invoke('accounts:list'),
    create: (name: string) => ipcRenderer.invoke('accounts:create', name),
    launch: (name: string) => ipcRenderer.invoke('accounts:launch', name),
  },
  state: {
    get: (): Promise<AppState> => ipcRenderer.invoke('state:get'),
    onChange: (cb) => sub<AppState>('state:changed', cb),
    setStartMinimized: (v) => ipcRenderer.invoke('state:setStartMinimized', v),
    setStartWithWindows: (v) => ipcRenderer.invoke('state:setStartWithWindows', v),
    setEnableDiscordRpc: (v) => ipcRenderer.invoke('state:setEnableDiscordRpc', v),
    acceptDisclaimer: () => ipcRenderer.invoke('state:acceptDisclaimer'),
    setUiMode: (v: 'simple' | 'advanced') => ipcRenderer.invoke('state:setUiMode', v),
    resetOnboarding: () => ipcRenderer.invoke('state:resetOnboarding'),
  },
  config: {
    get: (): Promise<TraderConfig> => ipcRenderer.invoke('config:get'),
    update: (patch) => ipcRenderer.invoke('config:update', patch),
    replace: (cfg) => ipcRenderer.invoke('config:replace', cfg),
    reset: () => ipcRenderer.invoke('config:reset'),
    listStrategies: (): Promise<StrategyPreset[]> =>
      ipcRenderer.invoke('config:listStrategies'),
    applyStrategy: (id) => ipcRenderer.invoke('config:applyStrategy', id),
  },
  profiles: {
    list: (): Promise<Profile[]> => ipcRenderer.invoke('profiles:list'),
    save: (name, description, scope) => ipcRenderer.invoke('profiles:save', name, description, scope),
    apply: (id) => ipcRenderer.invoke('profiles:apply', id),
    rename: (id, name) => ipcRenderer.invoke('profiles:rename', id, name),
    delete: (id) => ipcRenderer.invoke('profiles:delete', id),
    duplicate: (id) => ipcRenderer.invoke('profiles:duplicate', id),
    export: (id) => ipcRenderer.invoke('profiles:export', id),
    import: (json) => ipcRenderer.invoke('profiles:import', json),
  },
  credentials: {
    status: (): Promise<CredentialsState> => ipcRenderer.invoke('credentials:status'),
    statusAll: () => ipcRenderer.invoke('credentials:statusAll'),
    save: (input: CredentialsInput) => ipcRenderer.invoke('credentials:save', input),
    test: (env?: string) => ipcRenderer.invoke('credentials:test', env),
    clear: (env?: string) => ipcRenderer.invoke('credentials:clear', env),
    onChanged: (cb) => sub<unknown>('credentials:changed', cb),
  },
  backend: {
    info: (): Promise<BackendInfo> => ipcRenderer.invoke('backend:info'),
    start: () => ipcRenderer.invoke('backend:start'),
    stop: () => ipcRenderer.invoke('backend:stop'),
    restart: () => ipcRenderer.invoke('backend:restart'),
    onInfo: (cb) => sub<BackendInfo>('backend:info', cb),
    runOnce: (action) => ipcRenderer.invoke('backend:runOnce', action),
  },
  trading: {
    setEnabled: (v: boolean): Promise<ActionResult> =>
      ipcRenderer.invoke('trading:setEnabled', v),
    cancelAllOpen: () => ipcRenderer.invoke('trading:cancelAllOpen'),
    flatten: () => ipcRenderer.invoke('trading:flatten'),
    status: () => ipcRenderer.invoke('trading:status'),
    collection: () => ipcRenderer.invoke('backtest:collection'),
    exportData: () => ipcRenderer.invoke('backtest:export'),
  },
  data: {
    account: (): Promise<AccountSnapshot> => ipcRenderer.invoke('data:account'),
    pnlSeries: (sinceHours?: number): Promise<PnlPoint[]> =>
      ipcRenderer.invoke('data:pnlSeries', sinceHours),
    positions: (filter?: PositionFilter): Promise<BotPosition[]> =>
      ipcRenderer.invoke('data:positions', filter),
    signals: (filter?: SignalFilter): Promise<SignalRow[]> =>
      ipcRenderer.invoke('data:signals', filter),
    scannerStats: (): Promise<ScannerStats> => ipcRenderer.invoke('data:scannerStats'),
    botRuns: (env, limit) =>
      ipcRenderer.invoke('data:botRuns', env ?? null, limit),
    onAccount: (cb) => sub<AccountSnapshot>('data:account', cb),
    onPosition: (cb) => sub<BotPosition>('data:position', cb),
    onSignal: (cb) => sub<SignalRow>('data:signal', cb),
    onSignalBatch: (cb) => sub<{ signals: SignalRow[] }>('data:signalBatch', cb),
  },
  polydata: {
    userPnl: (interval) => ipcRenderer.invoke('data:userPnl', interval),
    resolutions: (conditionIds) => ipcRenderer.invoke('data:resolutions', conditionIds),
    holders: (conditionId, limit) => ipcRenderer.invoke('data:holders', conditionId, limit),
    biggestWinners: (period, category) => ipcRenderer.invoke('data:biggestWinners', period, category),
  },
  perps: {
    board: () => ipcRenderer.invoke('perps:board'),
    status: () => ipcRenderer.invoke('perps:status'),
    link: () => ipcRenderer.invoke('perps:link'),
    unlink: () => ipcRenderer.invoke('perps:unlink'),
    flatten: () => ipcRenderer.invoke('perps:flatten'),
    cancelAll: () => ipcRenderer.invoke('perps:cancelAll'),
    backtest: (args) => ipcRenderer.invoke('perps:backtest', args),
  },
  crypto15m: {
    snapshot: () => ipcRenderer.invoke('crypto15m:snapshot'),
    status: () => ipcRenderer.invoke('crypto15m:status'),
    history: (opts?: { limit?: number }) => ipcRenderer.invoke('crypto15m:history', opts),
    backtest: (args?: { sinceDays?: number; config?: Record<string, unknown> }) =>
      ipcRenderer.invoke('crypto15m:backtest', args),
    backtestMain: (args?: { sinceDays?: number; config?: Record<string, unknown> }) =>
      ipcRenderer.invoke('main:backtest', args),
    onAutoOff: (cb) => sub<{ reason: string; gained: number; target: number }>('crypto15m:autoOff', cb),
  },
  scripts: {
    list: () => ipcRenderer.invoke('scripts:list'),
    save: (s) => ipcRenderer.invoke('scripts:save', s),
    delete: (id: string) => ipcRenderer.invoke('scripts:delete', id),
    setEnabled: (id: string, enabled: boolean) => ipcRenderer.invoke('scripts:setEnabled', id, enabled),
    setAssets: (id: string, assets: string[] | null) => ipcRenderer.invoke('scripts:setAssets', id, assets),
    setDryRun: (id: string, dryRun: boolean) => ipcRenderer.invoke('scripts:setDryRun', id, dryRun),
    exportFile: (name: string, code: string) => ipcRenderer.invoke('scripts:exportFile', name, code),
    importFile: () => ipcRenderer.invoke('scripts:importFile'),
    shadowOrders: (id: string, limit?: number) => ipcRenderer.invoke('scripts:shadowOrders', id, limit),
    validate: (code: string) => ipcRenderer.invoke('scripts:validate', code),
    backtest: (args) => ipcRenderer.invoke('scripts:backtest', args),
    contextPack: () => ipcRenderer.invoke('scripts:contextPack'),
    docs: () => ipcRenderer.invoke('scripts:docs'),
    exportPack: () => ipcRenderer.invoke('scripts:exportPack'),
    onStatus: (cb) => sub<{ id: string; enabled: boolean; lastError?: string }>('scripts:status', cb),
    onLog: (cb) => sub<{ id: string; lines: string[] }>('scripts:log', cb),
  },
  copy: {
    status: () => ipcRenderer.invoke('copy:status'),
  },
  polymarket: {
    marketUrl: (args) => ipcRenderer.invoke('polymarket:marketUrl', args),
  },
  terminal: {
    discover: (args) => ipcRenderer.invoke('terminal:discover', args),
    search: (args) => ipcRenderer.invoke('terminal:search', args),
    market: (conditionId) => ipcRenderer.invoke('terminal:market', { conditionId }),
    book: (conditionId, side) => ipcRenderer.invoke('terminal:book', { conditionId, side }),
    history: (conditionId, side, interval) =>
      ipcRenderer.invoke('terminal:history', { conditionId, side, interval }),
    tape: (conditionId, limit) => ipcRenderer.invoke('terminal:tape', { conditionId, limit }),
    resolution: (conditionId) => ipcRenderer.invoke('terminal:resolution', { conditionId }),
    micro: (conditionId, side) => ipcRenderer.invoke('terminal:micro', { conditionId, side }),
    related: (conditionId, limit) => ipcRenderer.invoke('terminal:related', { conditionId, limit }),
    crossVenue: (conditionId, interval) =>
      ipcRenderer.invoke('terminal:crossVenue', { conditionId, interval }),
    health: () => ipcRenderer.invoke('terminal:health'),
    watchlist: (args) => ipcRenderer.invoke('terminal:watchlist', args),
    preview: (order) => ipcRenderer.invoke('terminal:preview', order),
    place: (order) => ipcRenderer.invoke('terminal:place', order),
    cancel: (orderId) => ipcRenderer.invoke('terminal:cancel', orderId),
    orders: (conditionId) => ipcRenderer.invoke('terminal:orders', { conditionId }),
    standing: (args) => ipcRenderer.invoke('terminal:standing', args),
    flattenPreview: () => ipcRenderer.invoke('terminal:flattenPreview'),
    flattenWallet: () => ipcRenderer.invoke('terminal:flattenWallet'),
    sellPreview: (order) => ipcRenderer.invoke('terminal:sellPreview', order),
    sellPosition: (order) => ipcRenderer.invoke('terminal:sellPosition', order),
    portfolio: () => ipcRenderer.invoke('terminal:portfolio'),
    settled: () => ipcRenderer.invoke('terminal:settled'),
    fills: (limit) => ipcRenderer.invoke('terminal:fills', limit),
    orderLog: (limit) => ipcRenderer.invoke('terminal:orderLog', limit),
    reconcile: () => ipcRenderer.invoke('terminal:reconcile'),
    onStanding: (cb) => sub('terminal:standing', cb),
    paperPlace: (order) => ipcRenderer.invoke('terminal:paperPlace', order),
    paperPortfolio: () => ipcRenderer.invoke('terminal:paperPortfolio'),
    paperOrders: (limit) => ipcRenderer.invoke('terminal:paperOrders', limit),
    paperCancel: (localId) => ipcRenderer.invoke('terminal:paperCancel', localId),
    paperReset: () => ipcRenderer.invoke('terminal:paperReset'),
  },
  ai: {
    status: () => ipcRenderer.invoke('ai:status'),
    analyze: (conditionId) => ipcRenderer.invoke('ai:analyze', { conditionId }),
    setKey: (provider, key) => ipcRenderer.invoke('ai:setKey', { provider, key }),
    listModels: (provider) => ipcRenderer.invoke('ai:listModels', { provider }),
  },
  remote: {
    status: () => ipcRenderer.invoke('remote:status'),
    setToken: (platform, token) => ipcRenderer.invoke('remote:setToken', { platform, token }),
    pairCode: (platform) => ipcRenderer.invoke('remote:pairCode', { platform }),
    addPaired: (platform, id, label) =>
      ipcRenderer.invoke('remote:addPaired', { platform, id, label }),
    unpair: (platform, id) => ipcRenderer.invoke('remote:unpair', { platform, id }),
    sync: () => ipcRenderer.invoke('remote:sync'),
    test: () => ipcRenderer.invoke('remote:test'),
  },
  mcp: {
    status: () => ipcRenderer.invoke('mcp:status'),
    sync: () => ipcRenderer.invoke('mcp:sync'),
    rotateToken: () => ipcRenderer.invoke('mcp:rotateToken'),
    revealToken: () => ipcRenderer.invoke('mcp:revealToken'),
    paper: (reset) => ipcRenderer.invoke('mcp:paper', { reset: !!reset }),
    clients: () => ipcRenderer.invoke('mcp:clients'),
    install: (id) => ipcRenderer.invoke('mcp:install', { id }),
    uninstall: (id) => ipcRenderer.invoke('mcp:uninstall', { id }),
  },
  logs: {
    tail: (limit?: number): Promise<LogEntry[]> => ipcRenderer.invoke('logs:tail', limit),
    onAppend: (cb) => sub<LogEntry>('logs:append', cb),
    clear: () => ipcRenderer.invoke('logs:clear'),
    openFolder: () => ipcRenderer.invoke('logs:openFolder'),
  },
  window: {
    minimize: () => ipcRenderer.send('window:minimize'),
    maximize: () => ipcRenderer.send('window:maximize'),
    close: () => ipcRenderer.send('window:close'),
    isMaximized: () => ipcRenderer.invoke('window:isMaximized'),
    onMaximizeChange: (cb) => sub<boolean>('window:maximizeChange', cb),
  },
};

contextBridge.exposeInMainWorld('krypt', api);
