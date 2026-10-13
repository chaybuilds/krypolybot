# Disclaimer & Risk Notice

**Read this before using Krypt PolyBot with real money.**

Krypt PolyBot is free, open-source, experimental software for placing trades on
[Polymarket](https://polymarket.com). By downloading, building, or running it, you
acknowledge and accept everything below.

## Not financial advice
Krypt PolyBot, its strategies, signals, scores, and any documentation are for
informational and educational purposes only. Nothing here is financial,
investment, legal, or tax advice. The authors are **not** registered investment
advisors, commodity trading advisors, broker-dealers, or fiduciaries of any
kind, and nothing in this project creates such a relationship.

## Risk of loss
Trading event contracts involves substantial risk. **You can lose some or all
of the money in your account.** Automated trading can lose money quickly and at
scale, including while you are away from your computer. Only trade with money
you can afford to lose entirely.

## The strategies are unproven
The bundled strategies (whale tracker, momentum scanner, 15-minute crypto, etc.)
are **heuristics**. They:

- have **not** been validated with out-of-sample backtesting;
- do **not** currently account for Polymarket trading fees in their entry/sizing
  decisions, which can erode or eliminate any apparent edge;
- carry **no guarantee of profitability**.

Any performance figures, "edge" scores, or calibration claims are illustrative
and are **not** a promise of future results. Past or simulated performance does
not indicate future performance.

## No warranty
The software is provided "AS IS", without warranty of any kind, as stated in the
[LICENSE](./LICENSE). It may contain bugs that cause incorrect orders, missed
orders, or inaccurate P&L. The authors are not liable for any losses, damages,
or claims arising from its use.

## Your responsibilities
You are solely responsible for:

- Complying with [Polymarket's Terms of Service](https://polymarket.com/terms) and API
  terms, including any rules on automated/algorithmic trading. **Confirm that
  automated trading with your account is permitted before enabling it.**
- Complying with all laws and regulations in your jurisdiction, including
  eligibility, age, and licensing requirements.
- The security of your Polygon wallet private key and the machine you run this on.
  Anyone with that key controls the wallet's funds — use a dedicated trading wallet.
- Every order the software places on your behalf.

## Start small — the engines have no practice mode
Polymarket is mainnet-only (real USDC) and there is no demo exchange, so **every
engine places real orders with real money the moment it is enabled while the app is
Live**. All engines
ship **OFF by default**. Before turning any engine on, understand exactly what it
does, and begin with a small amount you can afford to lose entirely.

The app has one **Paper | Live** account mode, and a new install starts on Paper.
In Paper, orders you place by hand, from the phone, by standing stops and by AI
agents are simulated, with fills walked out of the real order book and no wallet
required. **The engines are not simulated.** In Paper the whale, momentum, crypto,
copy-trading, Perps and live-script engines send nothing; once the app is Live, each
one you have switched on trades for real. Do not mistake an engine that waited
quietly in Paper for one that has been tested. (User scripts have their own
separate *shadow mode*, which is a paper mode in all but name.)

## AI agents trade on your behalf
If you connect an AI agent or switch on Autopilot, **every order it places is an
order you placed.** Language models are often wrong, can be confidently wrong, and
can be talked into things by text they read. The app enforces caps, a daily loss
stop, approvals and other rules in its backend, but those limit the damage; they do
not make an agent's decisions good. No model has a proven edge. Keep agents on
paper until their scored record says otherwise, and keep live caps small.

## No usage data leaves your machine
Krypt PolyBot sends **no** usage data, telemetry or statistics anywhere. Earlier
versions posted anonymous profit/loss figures to a community leaderboard; that
has been **removed entirely**, along with the optional Discord webhooks.

The app talks to Polymarket (and Kalshi, for read-only price comparison), and to
nobody else — unless you switch on one of these yourself: the optional
Discord/Telegram remote-control bots, which necessarily send what you ask them for
to that chat platform; AI analysis or Autopilot, which send market text (and, for
Autopilot, whatever its agent reads) to the AI provider you chose; or an AI agent
client, which sends whatever the agent reads, including your positions and balance,
to that client's provider. Every outbound host is listed with live call counts in
**Settings → Data & privacy**, with one switch that turns market data off.

## Affiliate disclosure
Links to Polymarket in this app and its documentation are **referral links**. If
you sign up through one, Polymarket may credit both you and the authors. This is
a material connection; using a referral link is optional and costs you nothing
extra.

## No affiliation
Krypt PolyBot is an independent project and is **not affiliated with, endorsed
by, or sponsored by** Polymarket, Discord, or any data provider. Your use of
those services is governed by their own terms, and you are responsible for
complying with them.

## Limitation of liability
To the maximum extent permitted by law, the authors and contributors shall not
be liable for any direct, indirect, incidental, special, consequential, or
exemplary damages — including, without limitation, trading losses, lost profits,
missed or erroneous orders, loss of funds, data loss, or actions taken by
Polymarket — arising out of or relating to your use of (or inability to use)
this software, even if advised of the possibility of such damages. Your sole
remedy is to stop using the software.

If you do not agree with any of the above, do not use this software.
