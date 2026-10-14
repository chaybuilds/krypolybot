# Polymarket US Native Execution Adapter

The official **Polymarket US** SDK (polymarket-us 2.4.1) powers a separate execution
path under the application's **Polymarket US** navigation tab. This is NOT an
international Polymarket Polygon/CLOB wallet connection. Do not paste a Polygon
wallet's private key here.

## What is implemented

- Public US market search, market detail, book and BBO reads using the official SDK
- US developer API-key verification through the authenticated account balances call
- Per-local-profile Ed25519 secret and key-ID storage in the operating-system
  credential manager (Windows Credential Manager; macOS Keychain where supported)
- Authenticated balances, positions, open orders and exchange activity reads
- Limit BUY_LONG and SELL_LONG with validation against **fresh exchange** market
  status, best bid/ask, exchange tick size and a per-order pilot cap
- Verified SELL_LONG inventory before permitting an exit
- Official authenticated order-preview check before sending a live POST
- Real CREATE and CANCEL SDK calls gated independently from international controls
- UUID deduplication, SQLite execution journal, and no automatic order POST retries
- Reconciliation through individual exchange order state, cumulative filled quantity,
  remaining quantity and commission fields; never assume submission is a fill

## Live order authority and conservative default limits

Fresh installs are **read-only**. Live requires all conditions simultaneously:

1. A properly issued US developer API key and secret (polymarket.us/developer).
2. A successful authenticated account check.
3. Local trusted process configuration `ORACLE_US_ALLOW_LIVE=1`.
4. User input of `I AUTHORIZE REAL MONEY US ORDERS` in the US desk.
5. Temporary arming (ten minutes; resets on backend restart).
6. Per-order gross USD <= 5, buy gross <= 15 per process session and <= 30 per
   UTC day, recorded durably. Caps cannot be increased from the UI.
7. Verified market open, public BBO, acceptable slippage, correct tick and valid
   positive quantity. SELL_LONG must have verified inventory.
8. A new UUID for each intended submission. Pending and uncertain submissions
   cannot be automatically retried.

Risk-reducing order cancellation is permitted when the order-sending session is
disarmed. A cancel acknowledgement is not proof of cancellation.

**Do not set the live deployment flag on a funded installation until a dedicated
security review and isolated-signing design are complete.** The original app runs
user-installed Python scripts in a non-sandboxed process. OS credential storage
alone does not isolate secrets from other processes under the same user identity.
The existing Electron/development dependency audit also needs remediation.

## How to run and inspect now (no funded account required)

On the authorized Windows PC, the development source is under
`Documents/PredictionMarkets/krypolybot` on branch
`feature/polymarket-us-native-adapter`.

```sh
npm ci --ignore-scripts
uv venv python/.venv --python 3.11
uv pip install --python python/.venv/Scripts/python.exe -r python/requirements.txt
npm run typecheck
npm run build
npm run test
python/.venv/Scripts/python.exe -m pytest -q python/tests/test_us_adapter.py
node scripts/check-us-rpc.mjs
npm run dev
```

From the UI, select **Polymarket US** in the terminal section. Search a market,
load its actual book, preview a limit order and inspect the read-only status.
Without the US developer keys no authenticated account or order is possible.
Without the live gate, order submission is refused *before* contacting the
authenticated venue. The smoke script uses a separate temporary profile and
deliberately disables live mode.

## Honest limitations

- US market discovery and paper analysis do not prove an edge. There is no
  guaranteed profit, and this build has not placed a live order.
- NEXUS strategy forecasts are not yet connected to the US order path.
- The US 15-minute instruments require independently verified market discovery,
  settlement rules and fee modeling.
- This does not yet provide a continuously operating, unattended US strategy.
- Order logs are independent from the older international P&L schema. Gross daily
  submission limits are conservative, not a complete account-risk engine.
- Ambiguous POST outcomes stay marked uncertain until actual venue records resolve
  them; a client must not send a new order simply because a timeout occurred.
- Exchange API access, eligibility, SDK parameters, and trading permissions must
  be checked against current official documentation and the actual US account.
- Before commercialization: protect signer in a separate least-privileged process,
  upgrade vulnerable Electron dependencies, isolate plugins/strategies, test with
  a dedicated limited-permission account, review compliance and verify real fills.

Reference: https://github.com/Polymarket/polymarket-us-python

Commercial reuse remains governed by this project's and all bundled assets'
respective license terms.
