import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ensureVenv, run, VENV_PY, PY_DIR } from './python-utils.mjs';

ensureVenv();

console.log('>> Installing PyInstaller');
run(VENV_PY, ['-m', 'pip', 'install', 'pyinstaller>=6.6,<7', '--disable-pip-version-check']);

console.log('>> Cleaning previous build');
for (const d of ['build', 'dist']) {
  const p = join(PY_DIR, d);
  if (existsSync(p)) {
    rmSync(p, { recursive: true, force: true });
  }
}

console.log('>> Running PyInstaller from spec (this takes ~60s)');
run(VENV_PY, ['-m', 'PyInstaller', '--noconfirm', 'krypt-polybot-backend.spec']);

const out = join(PY_DIR, 'dist', 'krypt-polybot-backend');
if (!existsSync(out)) {
  console.error('!! PyInstaller did not produce', out);
  process.exit(1);
}

const exe = join(out, process.platform === 'win32'
  ? 'krypt-polybot-backend.exe'
  : 'krypt-polybot-backend');
console.log('>> Selftest: verifying frozen bundle can import deps + sign an order');
const profile = mkdtempSync(join(tmpdir(), 'kp-build-selftest-'));
const st = spawnSync(exe, ['--selftest'], {
  stdio: 'inherit',
  windowsHide: true,
  env: {
    ...process.env,
    KRYPT_POLYBOT_USERDATA: join(profile, 'userdata'),
    KRYPT_POLYBOT_VAULT: join(profile, 'vault'),
  },
});
rmSync(profile, { recursive: true, force: true });
if (st.error) {
  console.error(`!! Could not run frozen backend selftest: ${st.error.message}`);
  process.exit(1);
}
if (st.status !== 0) {
  console.error(
    '!! Frozen backend FAILED selftest (see report above). The bundle is missing\n' +
    '!! a runtime dependency or signer \u2014 refusing to ship it. Check that the build\n' +
    '!! venv has all of requirements.txt and that the .spec collects the failing\n' +
    '!! package, then rebuild. Aborting before electron-builder.',
  );
  process.exit(1);
}

for (const d of [join(out, 'data'), join(out, '_internal', 'data')]) {
  if (existsSync(d)) {
    console.log(`>> Removing ${d} (runtime data must never ship)`);
    rmSync(d, { recursive: true, force: true });
  }
}
const stray = [];
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(db|db-wal|db-shm|sqlite3?)$/i.test(name)) stray.push(p);
  }
};
walk(out);
if (stray.length) {
  console.error('!! The bundle contains database files; refusing to ship user data:');
  for (const p of stray) console.error(`!!   ${p}`);
  process.exit(1);
}

console.log('>> OK \u2014 backend bundled + selftest PASSED at python/dist/krypt-polybot-backend');
