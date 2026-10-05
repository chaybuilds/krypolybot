import { app, BrowserWindow, Notification, screen, session, shell } from 'electron';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { appendLog, broadcastState, registerIpc } from './ipc';
import { applyAccountFromArgv } from './system/accounts';
import { setStartWithWindows } from './system/autostart';
import { startDiscordRpc, stopDiscordRpc } from './system/discord';
import { pythonBackend } from './system/python-backend';
import * as store from './system/settings-store';
import { destroyTray, installTray, rebuild as rebuildTray } from './system/tray';

process.env.DIST_ELECTRON = __dirname;
process.env.DIST = join(__dirname, '..', 'dist');
process.env.VITE_PUBLIC = app.isPackaged
  ? process.env.DIST
  : join(__dirname, '..', 'public');

if (process.platform === 'win32') {
  app.setAppUserModelId('cc.krypt.polybot');
}

applyAccountFromArgv();

let mainWindow: BrowserWindow | null = null;

function iconPath(): string {
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, 'app.asar.unpacked', 'resources', 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    app.isPackaged
      ? join(process.resourcesPath, 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    join(__dirname, '..', 'resources', 'krypt.ico'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return candidates[0];
}

function createMainWindow(): BrowserWindow {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return mainWindow;
  }
  const state = store.get();
  const bounds = state.windowBounds;
  const primary = screen.getPrimaryDisplay();
  const width = bounds?.width ?? Math.min(1380, primary.workArea.width - 40);
  const height = bounds?.height ?? Math.min(900, primary.workArea.height - 40);
  const x = bounds?.x;
  const y = bounds?.y;

  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: 1100,
    minHeight: 720,
    x,
    y,
    backgroundColor: '#0A0A0F',
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    icon: iconPath(),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      devTools: !app.isPackaged,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (e, target) => {
    const dev = process.env['VITE_DEV_SERVER_URL'];
    if ((dev && target.startsWith(dev)) || target.startsWith('file://')) return;
    e.preventDefault();
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
  });

  const url = process.env['VITE_DEV_SERVER_URL'];
  if (url) {
    void mainWindow.loadURL(url);
  } else {
    void mainWindow.loadFile(join(process.env.DIST!, 'index.html'));
  }

  mainWindow.once('ready-to-show', () => {
    if (state.startMinimized && process.argv.includes('--autostart')) {
    } else {
      mainWindow?.show();
    }
  });

  let boundsTimer: NodeJS.Timeout | null = null;
  const persistBounds = (): void => {
    if (boundsTimer) clearTimeout(boundsTimer);
    boundsTimer = setTimeout(() => {
      boundsTimer = null;
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (mainWindow.isMaximized() || mainWindow.isMinimized()) return;
      const b = mainWindow.getBounds();
      const cur = store.get();
      store.save({ ...cur, windowBounds: b });
    }, 500);
  };
  mainWindow.on('move', persistBounds);
  mainWindow.on('resize', persistBounds);
  mainWindow.on('maximize', () => mainWindow?.webContents.send('window:maximizeChange', true));
  mainWindow.on('unmaximize', () => mainWindow?.webContents.send('window:maximizeChange', false));

  mainWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      mainWindow?.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

function notifyStanding(data: unknown): void {
  try {
    if (!store.get().config.terminalNotifications) return;
    if (!Notification.isSupported()) return;
    const e = (data ?? {}) as {
      kind?: string; title?: string; conditionId?: string;
      priceCents?: number; triggerCents?: number;
      blocked?: boolean; result?: string;
    };
    const market = e.title || (e.conditionId ?? '').slice(0, 12) || 'a market';
    const kind = ({
      stopLoss: 'Stop loss', takeProfit: 'Take profit',
      alertAbove: 'Alert', alertBelow: 'Alert',
    } as Record<string, string>)[e.kind ?? ''] ?? 'Instruction';
    const price = typeof e.priceCents === 'number' ? `${e.priceCents}¢` : '—';

    const n = new Notification({
      title: e.blocked
        ? `${kind} did NOT fire — ${market}`
        : `${kind} fired at ${price} — ${market}`,
      body: e.blocked
        ? (e.result || 'The trigger was hit but nothing was sold. Open Krypt PolyBot to see why.')
        : (e.result || `Trigger ${e.triggerCents ?? '—'}¢, market at ${price}.`),
      icon: iconPath(),
      urgency: e.blocked ? 'critical' : 'normal',
    });
    n.on('click', () => {
      const w = createMainWindow();
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    });
    n.show();
  } catch {
  }
}

let quitting = false;

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const w = createMainWindow();
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
  });

  app.whenReady().then(bootstrap);
}

function installCsp(): void {
  if (!app.isPackaged) return;
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    cb({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self'; script-src 'self'; " +
          "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
          "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; " +
          "connect-src 'self'",
        ],
      },
    });
  });
}

const CONFIG_PATCH_ALLOW: Record<string, 'boolean' | 'number' | 'directionMode'> = {
  enableTrading: 'boolean',
  tradeWhales: 'boolean',
  tradeMomentum: 'boolean',
  crypto15mEnabled: 'boolean',
  copyEnabled: 'boolean',
  scriptsLiveEnabled: 'boolean',
  crypto15mDirectionMode: 'directionMode',
  crypto15mEntryThreshold: 'number',
  crypto15mEntryMax: 'number',
  crypto15mMinDeltaPct: 'number',
  crypto15mTimeDelayMin: 'number',
};

function sanitizeConfigPatch(
  source: string, patch: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const drop = (k: string, why: string) =>
    console.error(`[${source}] dropped config key '${k}': ${why}`);
  const agentLive = !!store.get().config.mcpLiveTradingEnabled;
  for (const [k, v] of Object.entries(patch)) {
    if (source === 'mcp') {
      const def = (store.DEFAULT_CONFIG as unknown as Record<string, unknown>)[k];
      const safe = v === false || (k === 'terminalPaperMode' && v === true);
      if (/^(mcp|remote|ai)[A-Z]/.test(k) || k === 'network') {
        drop(k, 'never patchable by an AI agent'); continue;
      }
      if (k === 'terminalPaperMode' && v !== true) {
        drop(k, 'only the user can turn paper mode off'); continue;
      }
      if (!agentLive && !safe) { drop(k, 'AI agent live trading is off'); continue; }
      if (def === undefined || def === null) { drop(k, 'not a known setting'); continue; }
      const ok = Array.isArray(def) ? Array.isArray(v)
        : typeof def === 'object' ? (!!v && typeof v === 'object' && !Array.isArray(v))
          : typeof def === 'number' ? (typeof v === 'number' && Number.isFinite(v))
            : typeof v === typeof def;
      if (!ok) { drop(k, `expected ${Array.isArray(def) ? 'array' : typeof def}`); continue; }
      out[k] = v;
      continue;
    }
    const kind = CONFIG_PATCH_ALLOW[k];
    if (!kind) { drop(k, 'not patchable from this channel'); continue; }
    if (kind === 'boolean') {
      if (typeof v !== 'boolean') { drop(k, `expected boolean, got ${typeof v}`); continue; }
      if (k === 'enableTrading' && v && source !== 'remote' && source !== 'mcp') {
        drop(k, 'live trading can only be turned OFF from a script'); continue;
      }
      out[k] = v;
    } else if (kind === 'number') {
      if (typeof v !== 'number' || !Number.isFinite(v)) { drop(k, 'not a finite number'); continue; }
      out[k] = v;
    } else if (v === 'favorite' || v === 'contrarian' || v === 'model') {
      out[k] = v;
    } else {
      drop(k, 'not a valid direction mode');
    }
  }
  return out;
}

async function bootstrap(): Promise<void> {
  registerIpc();
  installCsp();

  const state = store.load();

  pythonBackend.onStatusChange((info) => {
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) {
        w.webContents.send('backend:info', info);
      }
    }
    rebuildTray({
      openWindow: () => createMainWindow(),
      toggleTrading: toggleTradingFromTray,
      isTrading: () => store.get().config.enableTrading,
      quit: () => {
        quitting = true;
        app.quit();
      },
    });
  });

  pythonBackend.onEvent((name, data) => {
    if (name === 'backend:ready') {
      void (async () => {
        for (let attempt = 1; attempt <= 3; attempt++) {
          try {
            await pythonBackend.request('setConfig', { config: store.get().config });
            return;
          } catch (e) {
            if (attempt === 3) {
              console.error('config re-push failed after 3 attempts:', e);
            } else {
              await new Promise((r) => setTimeout(r, 2000));
            }
          }
        }
      })();
    }
    if (name === 'remote:configPatch') {
      try {
        const patch = sanitizeConfigPatch(
          'remote', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => {});
        }
      } catch {
      }
    }
    if (name === 'mcp:configPatch') {
      try {
        const patch = sanitizeConfigPatch(
          'mcp', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => {});
        }
      } catch {
      }
    }
    if (name === 'script:configPatch') {
      try {
        const patch = sanitizeConfigPatch(
          'script', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => {});
        }
      } catch {
      }
    }
    if (name === 'crypto15m:autoOff') {
      try {
        const next = store.patchConfig({ crypto15mEnabled: false });
        broadcastState(next);
        void pythonBackend
          .request('setConfig', { config: next.config })
          .catch(() => {
          });
      } catch {
      }
    }
    for (const w of BrowserWindow.getAllWindows()) {
      if (w.isDestroyed()) continue;
      if (name === 'account:update') {
        w.webContents.send('data:account', data);
      } else if (name === 'position:new' || name === 'position:update') {
        w.webContents.send('data:position', data);
      } else if (name === 'signal:new') {
        w.webContents.send('data:signal', data);
      } else if (name === 'signal:batch') {
        w.webContents.send('data:signalBatch', data);
      } else if (name === 'credentials:changed') {
        w.webContents.send('credentials:changed', data);
      } else if (name === 'data:reset') {
        w.webContents.send('data:reset', data);
      } else if (name === 'crypto15m:autoOff') {
        w.webContents.send('crypto15m:autoOff', data);
      } else if (name === 'script:status') {
        w.webContents.send('scripts:status', data);
      } else if (name === 'script:log') {
        w.webContents.send('scripts:log', data);
      } else if (name === 'terminal:standing') {
        notifyStanding(data);
        w.webContents.send('terminal:standing', data);
      } else if (name === 'backend:authChanged' || name === 'backend:reconciled' || name === 'backend:ready' || name === 'backend:shutdown') {
      }
    }
  });

  pythonBackend.onLog((entry) => {
    appendLog(entry);
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) {
        w.webContents.send('logs:append', entry);
      }
    }
  });

  void pythonBackend.start().then(async () => {
    const start = Date.now();
    while (!pythonBackend.isRunning() && Date.now() - start < 8000) {
      await new Promise((r) => setTimeout(r, 200));
    }
    try {
      await pythonBackend.request('setConfig', { config: state.config });
    } catch {
    }
  });

  if (state.enableDiscordRpc) void startDiscordRpc();

  installTray({
    openWindow: () => createMainWindow(),
    toggleTrading: toggleTradingFromTray,
    isTrading: () => store.get().config.enableTrading,
    quit: () => {
      quitting = true;
      app.quit();
    },
  });

  if (app.isPackaged) {
    setStartWithWindows(state.startWithWindows);
  }

  const launchedAtLogin =
    (() => {
      try {
        return app.getLoginItemSettings().wasOpenedAtLogin === true;
      } catch {
        return false;
      }
    })() ||
    process.argv.some((a) => a === '--autostart' || a === '--hidden');

  if (!(state.startMinimized && launchedAtLogin)) {
    createMainWindow();
  }
}

async function toggleTradingFromTray(): Promise<void> {
  const cur = store.get();
  const next = store.patchConfig({ enableTrading: !cur.config.enableTrading });
  broadcastState(next);
  if (pythonBackend.isRunning()) {
    try {
      await pythonBackend.request('setConfig', { config: next.config });
    } catch {
    }
  }
  rebuildTray({
    openWindow: () => createMainWindow(),
    toggleTrading: toggleTradingFromTray,
    isTrading: () => store.get().config.enableTrading,
    quit: () => {
      quitting = true;
      app.quit();
    },
  });
}

app.on('window-all-closed', (e: Electron.Event) => {
  e.preventDefault();
});

app.on('before-quit', async () => {
  quitting = true;
  stopDiscordRpc();
  destroyTray();
  await pythonBackend.stop();
});
