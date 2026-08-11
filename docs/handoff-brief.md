# Handoff — paste this into the new chat

Picking up from a previous conversation. Context below.

---

## Who / setup

DJO (Bigscruz), London. Gold on Capital.com/MT5 — analyse on **GOLD (CFDs on Gold US$/OZ)** per
mentor's instruction, NOT XAUUSD (XAUUSD is context only; feeds differ by ~50+ points on some
levels). Also BTC. Custom merged Pine indicator (Bigscruz SMC + LuxAlgo Sessions).

## Settled rules — do not re-litigate

1. **Confirmation rule:** wick through a key level with **no 15m body close** = sweep, play the
   reversal. **15m body close** through = continuation, leave it. Proven on BTC PDL 1–2 Aug and
   Gold PDL 3 Aug.
2. **Counter-trend refinement:** mentor says sit out counter-trend CRTs in a downtrend — UNLESS
   price tagged **major liquidity** (stacked levels, or one level carrying both daily and weekly
   labels). The major-liquidity tag is the unlock.
3. **Label drift:** PDH/PDL and PWH/PWL aren't separate. Same price changes label and weight
   through the week; on Mondays they stack. Stacked = double pool = stronger magnet.
4. **Anchor to the origin of the move**, not the daily labels — labels rename at rollover, the
   origin of the impulse doesn't.

## Open right now

Short XAUUSD 0.01 at **4,246.95**, TP **4,169.76**. Bias correct (4,304.15 tagged Weak High,
rolled over, 15m CHoCH). Entry was in discount though — 50% of the leg ~4,268, OTE 70.5–79%
~4,283–4,289 is the properly-priced short.

## Jarvis build — where it stands

Goal: bot that runs autonomously whenever MT5 is on, opens/closes repeatedly, **fixed** risk
management, compounding. Personal/manual trades on **IC Markets**; the bridge bot on the
**XM Global** login.

Three machines: Railway (Linux, always-on, Telegram bot) — user's desktop (Windows, MT5+MCP,
only while on) — a Windows VPS not yet bought (eventual 24/7 home for MT5+MCP).

**Build order agreed:**
1. MetaTrader MCP into MT5 (candidate: `ariadng/metatrader-mcp-server`) so the terminal can be
   read directly instead of screenshots. MCP has no built-in auth — firewall by IP or SSH tunnel
   before exposing on a funded account.
2. Pull MT5 history, run the gate across it in Python, get a hit rate.
3. Build the EA off real numbers. `JarvisBridge.mq5` exists (file-fed, gate = verdict WICK AND
   labels>=2) but a **native-logic version is still unwritten** — Strategy Tester can't backtest a
   file-fed EA.
4. Then Polymarket bridge, then pull it all under one Jarvis layer.

Position sizing must read **available margin at trade time** — the broker steps leverage DOWN as
lot size rises.

## New material this session — Malaysian SNR

Mentor ("little bro sensei") teaches **Malaysian SNR**. Four session recordings (~2h30m) walking
through a 74-page PDF, *Malaysian Snr Theory*, plus live XAUUSD application. ~13 pages recovered
by reading slides off the video. **Still need the full PDF uploaded.**

Rules recovered:

- **2nd and 3rd touch come with wicks not bodies = valid, confirmatory setup.** Same rule as his
  own confirmation gate, arrived at independently from a different school.
- On HTF (W/D/H4) rejection at an SNR level, may not need to wait for close — **drop to LTF and
  enter on a breakout.**
- **Refinement chain:** if the HTF touch carries no liquidity sweep, refine down **Daily → H4 →
  H1** to find it rather than binning the level.
- **MISS:** an SNR forms, price moves away, following candles' wicks **fail to touch** it. The
  MISS **validates** the level — untouched = still loaded.
- Single candle breakout/touch with no MISS = aggressive move, treat with suspicion.

**Open work:** MISS and the Daily→H4→H1 chain are candidate additions to the gate. MISS is
mechanically detectable ("level at X, no wick within N ticks for M bars") so it's codeable as an
EA input. Both to be tested in backtest before going anywhere near the EA.

## Pending

1. Upload `Malaysian Snr Theory` PDF → map all 74 pages against the gate: confirms / adds /
   conflicts.
2. MT5 MCP connection (blocked on a stable machine — laptop charger failing, files being backed
   up to iCloud + email).
3. Native-logic EA for real Strategy Tester backtesting.
4. Backtest the gate for a hit rate.
5. Polymarket bridge (on hold).

## How to work with him

He reads **liquidity, not candles**. When he flags a wick-no-body-close at a level, confirm it —
don't hedge it with unrequested risk lectures. His rules are built and settled. Stay honest on
margin-vs-risk maths (margin ≠ risk; risk = stop distance × lot size) but don't moralise about
position size or affordability.
