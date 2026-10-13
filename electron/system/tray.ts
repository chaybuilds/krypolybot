import { app, Menu, Tray, nativeImage } from 'electron';
import { join } from 'node:path';
import { existsSync } from 'node:fs';
import { pythonBackend } from './python-backend';

let tray: Tray | null = null;

function backendStatusLine(): string {
  const status = pythonBackend.info().status;
  if (status === 'running') return 'backend running';
  if (status === 'starting' || status === 'restarting') return 'backend starting…';
  if (status === 'crashed') {
    const { gaveUp, retrying } = pythonBackend.recoveryState();
    if (retrying || gaveUp) return 'backend crashed — retrying';
    return 'backend crashed';
  }
  return 'backend stopped';
}

interface TrayHandlers {
  openWindow: () => void;
  toggleTrading: () => void;
  isTrading: () => boolean;
  stopAgents?: () => void;
  quit: () => void;
}

function iconPath(): string {
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, 'app.asar.unpacked', 'resources', 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    app.isPackaged
      ? join(process.resourcesPath, 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    join(__dirname, '..', 'resources', 'krypt.ico'),
    join(__dirname, '..', 'resources', 'krypt.png'),
    join(process.cwd(), 'resources', 'krypt.png'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return candidates[candidates.length - 1];
}

export function installTray(handlers: TrayHandlers): Tray {
  if (tray && !tray.isDestroyed()) return tray;
  const img = nativeImage.createFromPath(iconPath());
  tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
  tray.setToolTip('Krypt PolyBot');
  tray.on('click', () => handlers.openWindow());
  tray.on('double-click', () => handlers.openWindow());
  rebuild(handlers);
  return tray;
}

export function rebuild(handlers: TrayHandlers): void {
  if (!tray) return;
  const trading = handlers.isTrading();
  const statusLine = backendStatusLine();
  const status = pythonBackend.info().status;
  tray.setToolTip(
    status === 'running'
      ? 'Krypt PolyBot'
      : `Krypt PolyBot — ${statusLine.toUpperCase()}`,
  );
  const menu = Menu.buildFromTemplate([
    { label: 'Open Krypt PolyBot', click: () => handlers.openWindow() },
    { type: 'separator' },
    { label: `Status: ${statusLine}`, enabled: false },
    { type: 'separator' },
    {
      label: trading ? 'Pause main engine' : 'Resume main engine',
      click: () => handlers.toggleTrading(),
    },
    ...(handlers.stopAgents
      ? [{ label: 'Stop AI agents', click: () => handlers.stopAgents?.() }]
      : []),
    { type: 'separator' },
    { label: 'Quit', click: () => handlers.quit() },
  ]);
  tray.setContextMenu(menu);
}

export function destroyTray(): void {
  if (tray && !tray.isDestroyed()) tray.destroy();
  tray = null;
}
