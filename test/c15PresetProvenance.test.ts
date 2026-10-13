import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const src = readFileSync(join(__dirname, '..', 'src', 'pages', 'Crypto15m.tsx'), 'utf8');
const presets = src.slice(src.indexOf('const C15_PRESETS'), src.indexOf('function StrategySettings'));
const hints = [...presets.matchAll(/hint: '((?:[^'\\]|\\.)*)'/g)].map((m) => m[1]);
const MEASURED = /[−+-]?\d+(?:\.\d+)?¢\/(?:contract|ct)/;

describe('Crypto-tab preset hints', () => {
  it('found the preset hints', () => {
    expect(hints.length).toBeGreaterThanOrEqual(4);
  });

  it('name the corpus behind every measured number', () => {
    for (const h of hints.filter((x) => MEASURED.test(x))) {
      expect(h, h).toMatch(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}[–-]/);
      expect(h, h).toMatch(/n=[\d,]+|~?[\d,]+ windows/);
    }
  });

  it('say pre-TWAP and in-sample for the Jul 3–5 panel', () => {
    for (const h of hints.filter((x) => x.includes('Jul 3–5'))) {
      expect(h, h).toMatch(/in-sample/);
      expect(h, h).toMatch(/before the Aug 7 switch/);
    }
  });

  it('drop the figures nobody can attribute', () => {
    for (const gone of ['+0.10¢', 't=0.3', '−1.4¢', 'ZERO edge']) {
      expect(presets).not.toContain(gone);
    }
  });
});
