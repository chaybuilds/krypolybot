const COIN_WORDS: [RegExp, string][] = [
  [/^(bitcoin|btc)\b/i, 'BTC'], [/^(ethereum|eth)\b/i, 'ETH'], [/^(solana|sol)\b/i, 'SOL'],
  [/^(xrp|ripple)\b/i, 'XRP'], [/^(dogecoin|doge)\b/i, 'DOGE'], [/^(hyperliquid|hype)\b/i, 'HYPE'],
  [/^(bnb|binance coin)\b/i, 'BNB'],
];

export function marketTag(id: string | null | undefined): string {
  const s = (id ?? '').trim();
  if (/^0x[0-9a-fA-F]{16,}$/.test(s)) return `${s.slice(0, 6)}…${s.slice(-4)}`;
  return s.length > 16 ? `${s.slice(0, 15)}…` : s || 'market';
}

export function subject(ticker: string, title: string): string {
  const raw = (title ?? '').trim();
  if (!raw || raw === ticker) return marketTag(ticker);
  if (/\bup or down\b/i.test(raw)) {
    for (const [re, coin] of COIN_WORDS) if (re.test(raw)) return coin;
  }
  let t = raw.replace(/^(Will|Is|Does|Do|Can|Are)\s+(there\s+be\s+)?(the\s+|an?\s+)?/i, '').replace(/\?$/, '');
  t = t.replace(/\s+(tonight|today|this week|this month|in 20\d\d|by .*)$/i, '');
  t = t.replace(/\bthe\s+/gi, '');
  return t.length > 24 ? `${t.slice(0, 23).trimEnd()}…` : t;
}

export function cacheMeasureText(ctx: CanvasRenderingContext2D): CanvasRenderingContext2D {
  const orig = ctx.measureText.bind(ctx);
  const cache = new Map<string, TextMetrics>();
  ctx.measureText = (text: string): TextMetrics => {
    const key = `${ctx.font}\u0000${text}`;
    let m = cache.get(key);
    if (!m) {
      if (cache.size > 4000) cache.clear();
      m = orig(text);
      cache.set(key, m);
    }
    return m;
  };
  return ctx;
}
