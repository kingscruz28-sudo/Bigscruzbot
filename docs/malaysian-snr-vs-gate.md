# Malaysian SNR Theory mapped against the gate

Source: *Malaysian SNR Theory*, Iyanu Adegboruwa (Price Action Traders), 75 pages.
39 pages carry text; the other 36 are worked chart examples, read by rendering.

Page references are to the PDF. This is the "confirms / adds / conflicts" pass
the handoff brief listed as pending item 1.

---

## Confirms — the gate already matches the source

**Wick touch valid, body touch invalid.** p17, verbatim: *"First touch is
candle's body not the wick. So, it is INVALID setup and a non-confirmatory
touch. 2nd and 3rd touch come with wicks not bodies. So, it is a VALID and
confirmatory setup."* p21 rule 2 restates it: *"First Touch on SNR Level must
be SHADOW."*

Note on provenance: the brief recorded this rule as having been arrived at
independently from a different school. It was not — the mentor taught from
this document, so the gate and the book are a single source rather than two
that agree. The rule stands, but it carries one vote, not two. The backtest is
where a second one has to come from.

**Liquidity sweep is the quality filter.** p18 is titled *SNR + LIQUIDITY
SWEEP* and states plainly that it *"increases the odds of winning trades"*.
This is the major-liquidity unlock, in the book's own words.

**HTF rejection does not need the close.** p17: *"When you have HTF's
(Weekly/Daily/H4) price rejection at HTF SNR level, you may not need to wait
for close. Just go to lower timeframe and identify a breakout to enter
position."*

**Asian range into London killzone.** p74 diagrams exactly that sequence, and
adds the condition: *"especially when the key level is HTF (weekly or daily)
and the liquidity build-up toward it is in LTF (H4 or H1)."*

**Stacked levels are stronger.** p53 marks a *"1st touch at intersection of two
SNRs"* as the premium setup — the same reasoning as the label-stacking rule.

---

## Adds — new, and mechanically codeable

**MISS validation.** p20 defines it; p21 gives the significance rules:

1. A candle touching an SNR level **without MISS candles before it** is a
   risky/aggressive trade.
2. The first touch must be a shadow.
3. On a touch of a new level, **the first candle may close through it, but the
   second must not.**

p21 also annotates counts directly on the charts — "4 MISSED CANDLES", "5
MISSED CANDLES" — and concludes *"#2 touch is more safer and significant to
trade than #1 touch (has no MISS candle)"*. This is countable, so it is an EA
input (`MinMissCandles`).

**The 2-timeframe confirmation pairing.** p68 is specific where the brief was
general: *"Weekly Setup = H4 Confirmation. Daily Setup = H1 Confirmation."*
p69 adds the monitoring pair: Daily level → watch Daily-H4; H4 level → watch
H4-H1/M30.

**Structural stops.** p74, action plan 5: *"Place Stoploss at lower low
(buying) or higher high (selling)."* The book never uses a fixed distance.

**Level construction is exact.** p8-9: draw from one body's close to the next
body's open, ignore wicks. Resistance is a bullish-then-bearish pair ("A"
shape on a line chart); support is bearish-then-bullish ("V").

**Storyline hierarchy.** p66: Weekly is main direction, Daily is
retracement/roadblock, H4 is confirmation, and *"H1 is very special because
price decides whether direction is valid or not — make sure it gives wick/gap
candle."*

**Trendline rules.** p31: connect at least two SNRs; no candle may close
beyond point 3; only enter on a wick touch at point 3; never enter at point 2.
Trendlines do not apply to GAP SNR unless refined into an LTF engulfing.

**GAP SNR.** p14-15: a GAP is a hidden zone on HTF that becomes a breakout
when refined down. At the touch of an HTF level a H4 gap ("decision level") is
usually the breakout that reverses the move.

---

## Conflicts — where the source and the settled rules disagree

**Body close through: the book is looser.** The settled rule is *wick through,
no 15m body close* — a single body close kills the setup. p21 rule 3 allows
the first candle to close through provided the second does not. Both are
implemented in `JarvisSNR.mq5` behind `RequireSecondCandleHold`, so the
backtest can price the difference rather than the question being argued.

**Counter-trend gating is not in the book.** The mentor's refinement — sit out
counter-trend setups unless price tagged major liquidity — has no equivalent
here. The book gates on touch quality, MISS and confluence, never on trend
direction. That rule is an addition from outside this source.

**Fixed-distance targets have no basis in it.** The Python bot uses a flat
`TP_PIPS = 150` and `SL_PIPS = 50` per symbol. Nothing in the book supports a
fixed target; every stop it specifies is structural and every target is taken
from the next level or an R multiple. This is the same finding the geometry
report reached from the other direction.

**The Python CRT detector is a different strategy.** `detect_crt_signal` in
`Main.py` scans a 20-minute rolling window of 1-minute samples for a sweep and
recovery. That is not Malaysian SNR — no body close-to-open level, no MISS, no
HTF/LTF refinement. The two should be evaluated separately; `JarvisSNR.mq5`
implements the SNR gate, `Main.py` implements something else.

---

## The entry sequence (p28-29)

These two pages are the most mechanical in the book, and they change the shape
of the EA. The entry is not a single trigger on a level touch — it is a
sequence that has to complete in order.

p28, *"Simple rules to stardom"*:

```
IMPULSE            @ the engulfing zone
   ↓
SIDEWAYS (SW1)     refine to LTF here
   ↓
IMPULSE            toward the HTF zone
   ↓
SIDEWAYS (SW2)     inside the HTF zone
   ↓
ENGULFING BO       breakout confirmation
   ↓
ENTER TRADE        safe stop at the higher high
```

The same page frames the swing structure as alternating ranges off one HTF
engulfing zone: upward impulse into Range 1, sell the engulfing break there;
downward impulse into Range 2, buy the engulfing break there. Re-entry is
explicitly allowed — *"engulfing → retest → BO → re-entry"*.

p29 gives the confirmation chain as a single line: **HTF setup →
confirmation → entry at LTF breakout.**

- **HTF**: a bearish (selling) or bullish (buying) candle at the SNR.
- **Confirmation**: an HTF price-rejection candle — pin bar or engulfing.
- **Entry**: drop to LTF, enter on the breakout of the engulfing zone.
- **Stop**: at the engulfing zone boundary, above for a sell, below for a buy.

p29 also splits the setups into **continuation** and **reversal** patterns,
both drawn as a 1→2 leg structure, so the same machinery covers with-trend and
counter-trend entries.

### What this means for `JarvisSNR.mq5`

The EA currently implements the **trigger** — level construction, MISS
validation, wick-touch, stacking, session — and then enters on the touch with
a swing-based stop. That is the risk entry from p17, not the confirmatory one.

The confirmation chain above is not implemented. Adding it means a per-level
state machine (impulse → SW1 → impulse → SW2 → engulfing breakout) and moving
the stop from the swing extreme to the engulfing zone edge. Worth doing before
reading much into a backtest, since the book's own numbers assume the
confirmed entry rather than the touch.

## Still unread

The remaining chart pages are worked examples across GBPJPY, Step Index, V75
and Gold — useful for calibrating what a valid touch looks like by eye, but
they carry no rules that are not already stated in the text pages above.
