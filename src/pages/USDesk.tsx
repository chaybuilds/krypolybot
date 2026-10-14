import { useCallback, useEffect, useMemo, useState } from 'react';
import type { USExchangeStatus, USOrderInput } from '../../shared/types';

type RawMarket = { slug: string; question?: string; active?: boolean; closed?: boolean };
const EMPTY: USExchangeStatus = {
  venue: 'polymarket.us', connected: false, keyIdPreview: null,
  liveConfigured: false, armed: false, armedSecondsRemaining: 0,
  maxOrderUsd: '5', maxSessionUsd: '15', maxDailyUsd: '30',
  sessionGrossUsd: '0', autoTrading: false, mode: 'READ_ONLY',
};
const inputClass = 'w-full rounded-xl border border-white/15 bg-black/30 px-3 py-2 text-white outline-none focus:border-sky-500/70';
const buttonClass = 'rounded-xl bg-sky-600 px-4 py-2 font-semibold text-white transition hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40';
const panel = 'rounded-2xl border border-white/10 bg-white/[.035] p-5';

export function USDeskPage() {
  const [status, setStatus] = useState<USExchangeStatus>(EMPTY);
  const [query, setQuery] = useState('bitcoin');
  const [markets, setMarkets] = useState<RawMarket[]>([]);
  const [marketSlug, setMarketSlug] = useState('cpc-btc-150k-12-31-2026');
  const [book, setBook] = useState<any>(null);
  const [keyId, setKeyId] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [side, setSide] = useState<USOrderInput['intent']>('ORDER_INTENT_BUY_LONG');
  const [price, setPrice] = useState('0.05');
  const [quantity, setQuantity] = useState('10');
  const [preview, setPreview] = useState<any>(null);
  const [lastOrder, setLastOrder] = useState<any>(null);
  const [account, setAccount] = useState<any>(null);
  const [reconciliation, setReconciliation] = useState<any>(null);
  const [localOrders, setLocalOrders] = useState<any[]>([]);
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const run = useCallback(async (work: () => Promise<unknown>, success = '') => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await work();
      if (success) setMessage(success);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    const [state, records] = await Promise.all([window.krypt.us.status(), window.krypt.us.journal()]);
    setStatus(state);
    setLocalOrders(records.orders || []);
  }, []);

  useEffect(() => { void run(refresh); }, [refresh, run]);

  const refreshMarkets = () => run(async () => {
    const result = await window.krypt.us.markets(query);
    const items: RawMarket[] = (result.events || [])
      .flatMap((event: any) => event.markets || [])
      .filter((item: any) => item.active && !item.closed && typeof item.slug === 'string');
    setMarkets(items.slice(0, 50));
    if (!items.length) setMessage('No active markets in this search. Search another term.');
  });
  const readBook = () => run(async () => {
    const result = await window.krypt.us.book(marketSlug);
    setBook(result.marketData || result);
  });
  const save = () => run(async () => {
    await window.krypt.us.saveKeys(keyId.trim(), secretKey.trim());
    setKeyId('');
    setSecretKey('');
    await refresh();
  }, 'Polymarket US account authenticated; secret stored in the OS credential manager.');
  const clear = () => run(async () => {
    await window.krypt.us.clearKeys();
    setAccount(null);
    await refresh();
  }, 'API credentials disconnected');
  const arm = () => run(async () => {
    await window.krypt.us.arm(confirmation);
    setConfirmation('');
    await refresh();
  }, 'Real US execution armed temporarily with strict pilot caps.');
  const disarm = () => run(async () => {
    await window.krypt.us.disarm();
    await refresh();
  }, 'US execution disarmed.');

  const order = useMemo<USOrderInput>(() => ({
    marketSlug: marketSlug.trim(), intent: side, price, quantity: Number(quantity),
  }), [marketSlug, side, price, quantity]);
  const previewOrder = () => run(async () => {
    setPreview(null);
    const result = await window.krypt.us.preview(order);
    setPreview({ ...result, fingerprint: JSON.stringify(order) });
  }, 'Quote retrieved. No order sent.');
  const submit = () => run(async () => {
    if (!status.armed || !preview || preview.fingerprint !== JSON.stringify(order)) {
      throw new Error('Refresh preview and arm a real-money session before submitting');
    }
    // One UUID per explicit submission. Reuse is rejected by the backend.
    const requestId = crypto.randomUUID();
    const result = await window.krypt.us.place({ ...order, requestId });
    setLastOrder(result);
    setPreview(null);
    await refresh();
  }, 'Order request sent. Verify the exchange response and fills before assuming execution.');
  const loadAccount = () => run(async () => {
    setAccount(await window.krypt.us.account());
    await refresh();
  });
  const reconcile = () => run(async () => {
    setReconciliation(await window.krypt.us.reconcile());
    await refresh();
  }, 'Reconciliation snapshot loaded (no fill inferred from absent open orders).');
  const cancel = (orderId: string, slug: string) => run(async () => {
    await window.krypt.us.cancel(orderId, slug);
    await reconcileSnapshot();
  }, 'Cancellation requested. Confirm remote order status.');
  const reconcileSnapshot = async () => {
    setReconciliation(await window.krypt.us.reconcile());
    await refresh();
  };

  return (
    <main className="h-full overflow-y-auto p-6 text-white">
      <div className="mx-auto flex max-w-6xl flex-col gap-5 pb-20">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[.25em] text-sky-400">ORACLE / Native US Execution</p>
            <h1 className="mt-1 text-3xl font-bold">Polymarket US</h1>
            <p className="text-sm text-white/55">Real exchange quotes, authenticated account, limit orders, cancellation and fills.</p>
          </div>
          <div className="rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-2 text-sm font-semibold">
            {status.armed ? 'LIVE ARMED' : 'READ ONLY'} · {status.connected ? 'Account connected' : 'No API key'}
          </div>
        </div>

        {error && <div role="alert" className="rounded-xl border border-red-400/40 bg-red-400/10 p-3 text-sm text-red-200">{error}</div>}
        {message && <div className="rounded-xl border border-sky-400/30 bg-sky-400/10 p-3 text-sm">{message}</div>}
        <div className="grid gap-3 md:grid-cols-3">
          <div className={panel}><p className="text-xs uppercase text-white/50">Max order</p><p className="mt-2 text-2xl font-bold">${status.maxOrderUsd}</p></div>
          <div className={panel}><p className="text-xs uppercase text-white/50">Session gross limit</p><p className="mt-2 text-2xl font-bold">${status.maxSessionUsd}</p></div>
          <div className={panel}><p className="text-xs uppercase text-white/50">Session spent</p><p className="mt-2 text-2xl font-bold">${status.sessionGrossUsd}</p></div>
        </div>
        <div className="grid gap-5 lg:grid-cols-2">
          <section className={panel}>
            <h2 className="text-lg font-bold">1. Official US API connection</h2>
            <p className="my-2 text-xs text-white/50">Developer API keys from polymarket.us/developer. Never use a Polygon wallet private key here.</p>
            {status.connected ? (
              <div className="space-y-3">
                <p className="text-sm text-sky-300">Connected key ID: {status.keyIdPreview}</p>
                <div className="flex gap-2">
                  <button disabled={busy} className={buttonClass} onClick={() => void loadAccount()}>Fetch real account</button>
                  <button disabled={busy} className="rounded-xl border border-white/15 px-3 py-2" onClick={() => void clear()}>Disconnect</button>
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <input className={inputClass} aria-label="US API key ID" value={keyId} onChange={e => setKeyId(e.target.value)} placeholder="US developer key ID (UUID)" autoComplete="off" />
                <input className={inputClass} aria-label="US API secret" type="password" value={secretKey} onChange={e => setSecretKey(e.target.value)} placeholder="US developer Ed25519 secret key" autoComplete="off" />
                <button disabled={busy || !keyId || !secretKey} className={buttonClass} onClick={() => void save()}>Verify and securely connect</button>
              </div>
            )}
          </section>

          <section className={panel}>
            <h2 className="text-lg font-bold">2. Execution authority</h2>
            <p className="mt-2 text-sm text-white/60">Account login never authorizes trading by itself. Live orders require the local deployment gate and short-lived manual arming.</p>
            <p className="mt-2 text-xs font-medium text-amber-300">Important: the global Paper/Live indicator governs the international exchange, not this separate US execution session.</p>
            <p className="my-3 text-xs text-white/50">Deployment gate: {status.liveConfigured ? 'Allowed' : 'Disabled'} · Session remaining: {status.armedSecondsRemaining}s</p>
            {status.armed ? (
              <button className="rounded-xl bg-red-600 px-4 py-2 font-semibold" disabled={busy} onClick={() => void disarm()}>Stop US order submissions</button>
            ) : (
              <div className="space-y-2">
                <input className={inputClass} aria-label="Live execution authorization" placeholder="I AUTHORIZE REAL MONEY US ORDERS" value={confirmation} onChange={e => setConfirmation(e.target.value)} />
                <button className={buttonClass} disabled={busy || !status.connected || !status.liveConfigured || confirmation !== 'I AUTHORIZE REAL MONEY US ORDERS'} onClick={() => void arm()}>Arm real-money session</button>
              </div>
            )}
          </section>
        </div>
        <section className={panel}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-bold">3. US market discovery / live order book</h2>
            <span className="text-xs text-white/40">US market slugs only</span>
          </div>
          <div className="mt-3 flex gap-2">
            <input className={inputClass} value={query} onChange={e => setQuery(e.target.value)} aria-label="Market search term" placeholder="bitcoin" />
            <button className={buttonClass} disabled={busy} onClick={() => void refreshMarkets()}>Search</button>
          </div>
          {markets.length > 0 && (
            <div className="mt-3 max-h-40 overflow-y-auto rounded-xl border border-white/10">
              {markets.map((m, idx) =>
                <button key={m.slug + idx} className="block w-full border-b border-white/5 px-3 py-2 text-left text-xs hover:bg-white/5" onClick={() => { setMarketSlug(m.slug); setBook(null); setPreview(null); }}>
                  {m.slug} — {m.question || 'Market'}
                </button>
              )}
            </div>
          )}
          <div className="mt-4 flex gap-2">
            <input className={inputClass} value={marketSlug} onChange={e => {setMarketSlug(e.target.value); setPreview(null);}} aria-label="US market slug"/>
            <button className={buttonClass} disabled={busy} onClick={() => void readBook()}>Live book</button>
          </div>
          {book && <div className="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p>State: {book.state}</p>
            <p>Bid: {book.bids?.[0]?.px?.value ?? '—'}</p>
            <p>Ask: {book.offers?.[0]?.px?.value ?? '—'}</p>
          </div>}
        </section>
        <section className={panel}>
          <h2 className="text-lg font-bold">4. Actual exchange order entry</h2>
          <p className="my-2 text-xs text-white/55">Limit orders only. Preview is not a fill. Real submission is blocked unless your US session is armed.</p>
          <div className="grid gap-3 md:grid-cols-3">
            <label className="text-xs text-white/60">Intent
              <select className={inputClass} value={side} onChange={e => {setSide(e.target.value as USOrderInput['intent']);setPreview(null);}}>
                <option value="ORDER_INTENT_BUY_LONG">Buy long</option>
                <option value="ORDER_INTENT_SELL_LONG">Sell owned long</option>
              </select>
            </label>
            <label className="text-xs text-white/60">Limit USD per share
              <input className={inputClass} inputMode="decimal" value={price} onChange={e => {setPrice(e.target.value);setPreview(null);}} />
            </label>
            <label className="text-xs text-white/60">Quantity
              <input className={inputClass} inputMode="numeric" value={quantity} onChange={e => {setQuantity(e.target.value);setPreview(null);}}/>
            </label>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <button className={buttonClass} disabled={busy} onClick={() => void previewOrder()}>Verify fresh executable quote</button>
            <button className="rounded-xl bg-red-600 px-4 py-2 font-semibold disabled:opacity-40" disabled={busy || !status.armed || !preview || preview.fingerprint !== JSON.stringify(order)} onClick={() => void submit()}>Submit REAL US order</button>
          </div>
          {preview && <p className="mt-3 rounded-xl border border-white/10 bg-black/20 p-3 text-sm">
            Gross ${preview.grossUsd} · Quoted {preview.bestQuote} · {preview.quantity} shares. Fees and net returns are not inferred.
          </p>}
          {lastOrder && <pre className="mt-3 max-h-40 overflow-auto text-xs text-white/65">{JSON.stringify(lastOrder, null, 2)}</pre>}
        </section>
        <div className="grid gap-5 lg:grid-cols-2">
          <section className={panel}>
            <h2 className="text-lg font-bold">5. Actual account balances / open positions</h2>
            <button className={buttonClass} disabled={busy || !status.connected} onClick={() => void loadAccount()}>Refresh authenticated account</button>
            <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs text-white/60">{account ? JSON.stringify(account, null, 2) : 'Connect a US developer key to retrieve verified account data.'}</pre>
          </section>
          <section className={panel}>
            <h2 className="text-lg font-bold">6. Orders / reconciliation</h2>
            <button className={buttonClass} disabled={busy || !status.connected} onClick={() => void reconcile()}>Reconcile exchange records</button>
            <p className="mt-2 text-xs text-white/50">Submission acknowledgment is not a fill. Remote activity and order status are the source of truth.</p>
            {localOrders.slice(0, 10).map((record) => <div key={record.requestId} className="my-2 rounded-lg bg-black/20 p-2 text-xs">
              {record.state} · {record.marketSlug} · ${record.grossUsd}
              {record.orderId && <button className="ml-3 text-sky-300 underline disabled:opacity-40" disabled={busy || !status.connected} onClick={() => void cancel(record.orderId, record.marketSlug)}>Request cancel</button>}
            </div>)}
            {reconciliation && <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs text-white/60">{JSON.stringify(reconciliation, null, 2)}</pre>}
          </section>
        </div>
        <p className="text-xs text-white/40">Pilot engineering build. Credential isolation, dependency security, live execution and fee-adjusted profitability must be independently certified before funded use or customer distribution.</p>
      </div>
    </main>
  );
}
