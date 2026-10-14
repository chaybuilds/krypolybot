// Isolated Polymarket US page smoke via real Electron + IPC + Python backend.
// Disabled live flag, fresh test profile. Does not connect any key or submit any order.
import { _electron as electron } from 'playwright-core';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const packaged = Boolean(process.env.ORACLE_SMOKE_EXE);
const exe = process.env.ORACLE_SMOKE_EXE || (process.platform === 'win32'
  ? join(root, 'node_modules', 'electron', 'dist', 'electron.exe')
  : join(root, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron'));
const app = await electron.launch({
  executablePath: exe,
  args: packaged ? ['--account=OracleUSUISmokePackaged'] : [root, '--account=OracleUSUISmoke'],
  env: { ...process.env, ORACLE_US_ALLOW_LIVE: '0' },
  timeout: 45000,
});
try {
  const page = await app.firstWindow();
  await page.waitForLoadState('domcontentloaded');
  const api = await page.evaluate(async () => window.krypt?.us?.status());
  if (!api || api.armed || api.liveConfigured || api.connected) {
    throw new Error('Unsafe native UI status: ' + JSON.stringify(api));
  }
  console.log('ELECTRON IPC US STATUS: PASS');
  await page.evaluate(() => {
    window.localStorage.setItem('krypt.polybot.lastPage', 'usdesk');
    window.location.reload();
  });
  await page.getByText('ORACLE / Native US Execution').first().waitFor({state: 'visible', timeout: 30000});
  console.log('US DESK SCREEN: PASS');
  const disabled = await page.getByRole('button', {name: 'Submit REAL US order'}).isDisabled();
  if (!disabled) throw new Error('Danger: unarmed real-money submit button enabled');
  console.log('UNARMED BUY CONTROL: PASS');
  const response = await page.evaluate(async () => window.krypt.us.book('cpc-btc-150k-12-31-2026'));
  if (!response.marketData) throw new Error('No official US book behind Electron IPC');
  console.log('ELECTRON US ORDER BOOK: PASS - ' + response.marketData.state);
  console.log('ELECTRON US DESK SMOKE: PASS');
} finally {
  await app.close();
}
