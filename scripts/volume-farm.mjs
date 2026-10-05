import { join } from 'node:path';
import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

function userDataDir() {
  if (process.env.KRYPT_POLYBOT_USERDATA) return process.env.KRYPT_POLYBOT_USERDATA;
  if (process.platform === 'win32' && process.env.APPDATA) {
    return join(process.env.APPDATA, 'Krypt PolyBot');
  }
  if (process.platform === 'darwin' && process.env.HOME) {
    return join(process.env.HOME, 'Library', 'Application Support', 'Krypt PolyBot');
  }
  if (process.env.HOME) return join(process.env.HOME, '.config', 'Krypt PolyBot');
  return '';
}

ensureVenv();
const userData = userDataDir();
if (userData) console.log(`> using credentials from: ${userData}`);
run(VENV_PY, ['volume_farm.py', ...process.argv.slice(2)], {
  env: { ...process.env, KRYPT_POLYBOT_USERDATA: userData },
});
