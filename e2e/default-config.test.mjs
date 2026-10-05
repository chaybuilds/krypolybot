import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import test from 'node:test';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(root, p), 'utf-8').replace(/\r\n/g, '\n');

function blockBody(src, start) {
  const i = src.indexOf(start);
  assert.ok(i >= 0, `could not find ${start}`);
  const end = src.slice(i).search(/\n\}[;]?\n/);
  assert.ok(end > 0, `could not find the end of ${start}`);
  return src.slice(i, i + end);
}

const keysOf = (body, re) => [...body.matchAll(re)].map((m) => m[1]);

test('every TraderConfig key has a default in DEFAULT_CONFIG', () => {
  const iface = blockBody(read('shared/types.ts'), 'export interface TraderConfig {');
  const defaults = blockBody(
    read('electron/system/settings-store.ts'), 'export const DEFAULT_CONFIG');

  const declared = keysOf(iface, /^ {2}([A-Za-z][A-Za-z0-9]*)\??\s*:/gm);
  const defaulted = new Set(keysOf(defaults, /^ {2}([A-Za-z][A-Za-z0-9]*)\s*:/gm));

  assert.ok(declared.length > 100, `only parsed ${declared.length} TraderConfig keys`);
  assert.ok(defaulted.size > 100, `only parsed ${defaulted.size} DEFAULT_CONFIG keys`);
  assert.ok(declared.includes('enableTrading'), 'parser missed a known key');

  const missing = declared.filter((k) => !defaulted.has(k));
  assert.deepEqual(missing, [],
    `TraderConfig keys with no DEFAULT_CONFIG entry — a preset apply wipes each `
    + `of these: ${missing.join(', ')}`);
});

test('DEFAULT_CONFIG declares nothing TraderConfig does not', () => {
  const iface = blockBody(read('shared/types.ts'), 'export interface TraderConfig {');
  const defaults = blockBody(
    read('electron/system/settings-store.ts'), 'export const DEFAULT_CONFIG');

  const declared = new Set(keysOf(iface, /^ {2}([A-Za-z][A-Za-z0-9]*)\??\s*:/gm));
  const extra = keysOf(defaults, /^ {2}([A-Za-z][A-Za-z0-9]*)\s*:/gm)
    .filter((k) => !declared.has(k));
  assert.deepEqual(extra, [], `DEFAULT_CONFIG keys not in TraderConfig: ${extra.join(', ')}`);
});
