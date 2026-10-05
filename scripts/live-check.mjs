import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['live_check_terminal.py', ...process.argv.slice(2)]);
