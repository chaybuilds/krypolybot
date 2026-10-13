<div align="center">

<img src="resources/krypt.png" alt="Krypt PolyBot" width="108" />

# Krypt PolyBot

**A free, open-source Polymarket trading desktop app.**

Your own AI agents that trade Polymarket under hard rails, plus a manual trading terminal, whale tracker, momentum scanner, short-term crypto engine, copy trading, a Perps market maker and your own Python strategies in one clean app — your wallet key never leaves your machine.

[![CI](https://github.com/scripflipped/krypt-polybot/actions/workflows/ci.yml/badge.svg)](https://github.com/scripflipped/krypt-polybot/actions/workflows/ci.yml)
&nbsp;[![License: MIT](https://img.shields.io/badge/License-MIT-6366F1)](LICENSE)
&nbsp;[![Windows 10 | 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)](#download)
&nbsp;[![macOS | Linux](https://img.shields.io/badge/macOS%20%7C%20Linux-unsigned-6B7280)](#download)
&nbsp;[![Status: beta](https://img.shields.io/badge/status-beta-F59E0B)](#download)
&nbsp;[![Price: free](https://img.shields.io/badge/Price-free-EC4899)](#download)

<a href="../../releases/latest"><img src="https://img.shields.io/badge/Download-Krypt%20PolyBot-A855F7?style=for-the-badge&logo=github&logoColor=white" alt="Download Krypt PolyBot" /></a>
&nbsp;<a href="https://discord.gg/muzFKR657F"><img src="https://img.shields.io/badge/Join%20the-Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join the Discord" /></a>
&nbsp;<a href="https://krypt.cc/tools"><img src="https://img.shields.io/badge/More%20free%20tools-krypt.cc%2Ftools-EC4899?style=for-the-badge&logo=googlechrome&logoColor=white" alt="More free tools at krypt.cc/tools" /></a>

</div>

---

Krypt PolyBot lets you connect Claude, Cursor, Codex or any MCP client as a named AI trader (or let Autopilot run one for you), every one of them fenced in by caps, a loss stop and a forecast it has to beat, and scored against the market. It also lets you look up and trade any Polymarket market by hand, and runs automated engines that watch the public trade feed for whales and momentum, trade the short-term crypto Up/Down markets, mirror wallets you choose, or make markets on Polymarket Perps. All wrapped in a modern desktop UI instead of a script you have to babysit. Your wallet key and every trade stay in a local SQLite database on your machine. It talks to Polymarket, public crypto-price feeds and Kalshi (read-only), and sends nothing about you or your trading back to its authors (see the [Disclaimer](DISCLAIMER.md)).

> [!WARNING]
> **This app places real orders from your Polymarket wallet — trading carries real financial risk.** Polymarket trades on **Polygon mainnet with real USDC**; there is no demo exchange. The bundled strategies have **no proven, fee-adjusted edge** — on the recorded data every one measures net-negative — and may lose money. This is **not financial advice**, and you are responsible for following [Polymarket's Terms](https://polymarket.com/terms) and your local laws. The app starts on **Paper** and every engine ships **off**. Paper covers your own orders and the AI agents, but the engines are **not simulated**: in Paper they send nothing, and once you go Live, a switched-on engine trades for real. Please read the full [**Disclaimer**](DISCLAIMER.md).

## What it does

- **AI agents** — connect Claude Desktop, Claude Code, Cursor, Codex, Windsurf or VS Code over MCP, with no token in any config file. Build named agents, each with your own guide, rules and caps. A buy needs the agent's own forecast to beat the price after fees; live orders wait for your approval by default; an agent can only sell what it opened. A scoreboard checks whether each agent actually beats the market.
- **Autopilot** — the app runs one of your agents on a schedule with your own AI key, under the same rails, with daily run and token budgets.
- **Paper | Live** — one switch for the whole app. Paper trades the **real** order books with imaginary money, no wallet needed; the engines send nothing until you go Live.
- **Trading terminal** — search or browse any Polymarket market (four live discovery columns or your watchlist), then work it from a full market page: probability chart, order book, trade tape, your position and an order ticket. A number nobody could produce renders as `—`, never as a `0` you might trade on.
- **Resolution risk** — who resolves a market, on what source, how ambiguous the wording is, and whether a related market contradicts it.
- **Cross-venue prices** — the same question priced on Kalshi next to Polymarket. Read-only, and a weak match shows nothing rather than a fictional arbitrage.
- **Standing rules** — stop loss, take profit and price alerts in probability points, all on one page.
- **Whale tracker & momentum scanner** — flags $2.5k+ taker orders, volume spikes and trade clusters, and can trade them under your gates and sizing.
- **Short-term crypto** — the 5-minute, 15-minute, hourly and 4-hour BTC/ETH/SOL/XRP and other Up/Down markets, with a configurable strategy, a settlement model that matches how these markets actually resolve, and its own live switch.
- **Copy trading** — follow chosen wallets and mirror their entries at your own size.
- **Perps market maker** — quotes both sides on Polymarket Perps for spread and liquidity rewards, with inventory caps, loss limits and a dead man's switch that pulls quotes if the app dies.
- **Strategy scripts** — write your own Python strategies, backtest them on the data the app records, and run them in shadow mode before real money. Hard money rails cap what any script can spend.
- **AI analysis** — have a model read a market's resolution rules and point out the traps: Anthropic, OpenAI, OpenRouter, Gemini, or a local model through Ollama or LM Studio. It gives a reading, never a made-up probability.
- **Remote control** — check on the bot, or trade, from your phone over a Discord or Telegram bot you own. Paired to your account only; trading from chat is off by default and every order needs a confirmation code.
- **Local-first** — keys and trade history live under `%APPDATA%/Krypt PolyBot/`. No account required. **No telemetry** — no analytics, no usage stats, no leaderboard; **Settings → Data & privacy** lists every outbound host with live call counts and one switch to turn them off.

The full detail on every feature is in the [**Guide**](docs/GUIDE.md).

## Safe by default

- **The app starts on Paper** — nothing real is sent until you go Live from a checklist that tests your wallet and lists everything that will start spending.
- **Every engine starts off** — main engine, crypto, copy trading, Perps and live scripts each have their own switch, and nothing trades until you turn one on.
- **Breakers on the engines** — daily stop-loss / take-profit, a lifetime drawdown breaker, trading-hours windows and a pause-everything switch (also from your phone with `/pause`).
- **Your wallet key is encrypted at rest** on Windows (DPAPI), tied to your user account, and only ever used to sign orders locally.
- **New scripts start in shadow mode** — they record what they *would* have traded until you explicitly arm them.
- **A fee-aware backtest** (`npm run py:backtest`) measures the net-of-fee result on *your own* recorded data — run it before trusting any setting.
- **AI agents are fenced in by the backend, not by their prompt** — trade mode, caps, a daily loss stop, forecast-before-buy and approvals are enforced on every order, whatever the agent says. Agents start on paper, and whole-account powers are a separate switch, off by default.
- **Remote trading is a separate switch**, off by default and separate from reading — turning on alerts or research never turns on spending.
- **Missing data is shown as missing.** A position with no known cost basis reports unknown P&L and is left out of your totals rather than counted at a guess.

## Download

<a href="../../releases"><img src="https://img.shields.io/badge/Download%20for%20Windows-Krypt%20PolyBot-A855F7?style=for-the-badge&logo=windows&logoColor=white" alt="Download Krypt PolyBot" /></a>

Grab the latest installer from the [**Releases**](../../releases) page and run it. It starts on Paper, so you can connect an agent and try everything with no wallet. To trade for real, open **Wallet** and paste the private key of a Polygon wallet holding USDC — use a dedicated trading wallet funded only with what you intend to trade — then **Settings → Account → Go live**. Every engine starts off, so nothing trades until you turn one on. Releases also carry **unsigned Linux (AppImage/deb) and macOS (dmg, Intel + Apple Silicon)** builds — they get less testing than the Windows one.

> Builds aren't code-signed yet, so the first launch may show a SmartScreen prompt — click **More info → Run anyway**. The full source is right here if you'd rather build it yourself.

### Opening it on macOS

The mac builds are not notarized (Apple charges $99/yr for the certificate that would make this unnecessary), so the first launch needs one manual approval:

1. Open the `.dmg` and drag **Krypt PolyBot** to Applications.
2. Launch it once. macOS refuses — pick **Done**.
3. **System Settings → Privacy & Security**, scroll to Security: *"Krypt PolyBot was blocked"* → **Open Anyway**, then confirm.

## Build from source

```bash
git clone https://github.com/scripflipped/krypt-polybot.git
cd krypt-polybot
npm install
npm run dev      # auto-creates python/.venv on first run (~30s)
npm run dist     # build the installer for your OS into /release
```

Requires [Node.js](https://nodejs.org) 22+ and [Python](https://python.org) 3.11+ on PATH. Development runs on macOS/Linux/Windows. `npm run dist` builds the installer for the OS you're on (the build aborts if the bundled backend fails its self-test); CI builds Windows, Linux (AppImage/deb) and macOS (dmg, both architectures) installers and attaches them to a GitHub Release. Tests, backtests and the Windows build gotchas are in the [Guide](docs/GUIDE.md#testing--backtesting).

## Need a Polymarket account?

Sign up through our link, then deposit **$20** and place your first trade — Polymarket's current new-user offer is **up to $50 in trading credits** (trading credit, not instantly withdrawable; a small playthrough applies, and the amount, minimum deposit and terms are set by Polymarket and can change). The link is our referral, at no extra cost to you. Eligibility varies by region:
<https://polymarket.com?via=yuhgo>

## Links

- **Website** — [krypt.cc](https://krypt.cc) · more free tools at [krypt.cc/tools](https://krypt.cc/tools)
- **Support and community** — [Discord](https://discord.gg/muzFKR657F)
- **Guide** — [docs/GUIDE.md](docs/GUIDE.md) · **Security** — [SECURITY.md](SECURITY.md)
- **Contributing** — see [CONTRIBUTING.md](CONTRIBUTING.md)

## License

Released under the [MIT License](LICENSE) — free to use, fork and share. Please don't rebrand and resell it. Provided **as-is, with no warranty**; see the [Disclaimer](DISCLAIMER.md).

<div align="center"><sub>Built by the Krypt team · <a href="https://krypt.cc">krypt.cc</a></sub></div>
