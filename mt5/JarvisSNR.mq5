//+------------------------------------------------------------------+
//|                                                   JarvisSNR.mq5   |
//|                    BIGSCRUZ FX — native-logic Malaysian SNR EA    |
//|            PATIENCE | DISCIPLINE | FEARLESS                       |
//+------------------------------------------------------------------+
//| Why this exists                                                   |
//|                                                                   |
//| JarvisBridge.mq5 reads signals from a file, so Strategy Tester    |
//| cannot backtest it — the tester has no file being written to it.  |
//| This EA carries the same gate in native logic, so it runs in the  |
//| tester and can be optimised.                                      |
//|                                                                   |
//| The gate, and where each rule comes from:                         |
//|                                                                   |
//|  1. SNR levels are drawn body close -> next body open, wicks      |
//|     ignored (Malaysian SNR Theory, p8-9).                         |
//|  2. A touch must be a wick. A first touch that is a body close is |
//|     invalid and non-confirmatory (p17, p21 rule 2).               |
//|  3. A level is only tradable once MISS candles have formed after  |
//|     it — candles whose wicks failed to reach it. A touch with no  |
//|     MISS before it is aggressive/risky (p20, p21 rule 1).         |
//|  4. The first candle on a new level may close through it, but the |
//|     second must not (p21 rule 3). This is the book's version of   |
//|     the no-body-close rule; RequireSecondCandleHold below toggles |
//|     between it and the stricter "no body close at all".           |
//|  5. Stacked levels are stronger — a level carrying more than one  |
//|     label is major liquidity (settled rule, and p18 SNR+LS).      |
//|  6. Stop goes at the swing low when buying, swing high when       |
//|     selling — structural, not a fixed distance (p74 plan 5).      |
//|                                                                   |
//| Every rule is an input so the tester can turn it off and price    |
//| its contribution rather than taking it on faith.                  |
//+------------------------------------------------------------------+
#property copyright "BIGSCRUZ FX"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- level construction
input ENUM_TIMEFRAMES  LevelTF            = PERIOD_D1;  // where SNR levels are drawn
input ENUM_TIMEFRAMES  EntryTF            = PERIOD_M15; // where touches are judged
input int              LevelLookback      = 120;        // HTF bars scanned for levels
input double           ZoneBufferPoints   = 0;          // widen each zone by this many points

//--- the gate
input bool             RequireWickTouch   = true;       // body-only touch is invalid
input int              MinMissCandles     = 2;          // MISS candles needed to validate
input bool             RequireSecondCandleHold = true;  // p21 r3; false = no body close at all
input int              MinStackedLabels   = 1;          // 2+ = major liquidity only
input double           StackToleranceATR  = 0.25;       // levels within this are "stacked"

//--- session filter (server time hours)
input bool             UseSessionFilter   = true;
input int              SessionStartHour   = 7;          // London killzone open
input int              SessionEndHour     = 17;

//--- risk
input double           RiskPercent        = 0.5;        // 0 = use FixedLot
input double           FixedLot           = 0.01;
input int              SwingLookback      = 12;         // bars for the structural stop
input double           StopBufferPoints   = 20;
input double           RewardMultiple     = 3.0;        // TP = R * stop distance
input int              MaxOpenPositions   = 1;

input int              MagicNumber        = 280029;
input int              MaxSlippage        = 30;

//+------------------------------------------------------------------+
struct SnrLevel
  {
   double            lo;
   double            hi;
   datetime          formed;
   bool              isResistance;
   int               missCandles;   // candles since forming whose wicks missed
   int               stacked;       // how many labels sit on this price
   bool              spent;         // already traded
  };

SnrLevel  g_levels[];
datetime  g_lastLevelBuild = 0;
datetime  g_lastEntryBar   = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaxSlippage);
   PrintFormat("JarvisSNR armed. Levels=%s Entry=%s MinMiss=%d Stacked>=%d",
               EnumToString(LevelTF), EnumToString(EntryTF),
               MinMissCandles, MinStackedLabels);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { }

//+------------------------------------------------------------------+
double Atr(ENUM_TIMEFRAMES tf, int period)
  {
   int h = iATR(_Symbol, tf, period);
   if(h == INVALID_HANDLE) return(0.0);
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(h, 0, 0, 1, buf) < 1) { IndicatorRelease(h); return(0.0); }
   double v = buf[0];
   IndicatorRelease(h);
   return(v);
  }

//+------------------------------------------------------------------+
//| Build SNR levels from the HTF: a bullish body followed by a       |
//| bearish body is resistance ("A"), the reverse is support ("V").   |
//| The zone spans the first candle's close and the next one's open;  |
//| wicks are ignored entirely.                                       |
//+------------------------------------------------------------------+
void BuildLevels()
  {
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int copied = CopyRates(_Symbol, LevelTF, 0, LevelLookback, r);
   if(copied < 10) return;

   ArrayResize(g_levels, 0);
   double buffer = ZoneBufferPoints * _Point;
   double atr    = Atr(LevelTF, 14);
   double tol    = (atr > 0 ? atr * StackToleranceATR : 10 * _Point);

   // i is the older candle, i-1 the one that follows it.
   for(int i = copied - 2; i >= 1; i--)
     {
      bool firstBull = (r[i].close   > r[i].open);
      bool nextBear  = (r[i-1].close < r[i-1].open);
      bool firstBear = (r[i].close   < r[i].open);
      bool nextBull  = (r[i-1].close > r[i-1].open);

      bool isRes = (firstBull && nextBear);
      bool isSup = (firstBear && nextBull);
      if(!isRes && !isSup) continue;

      double a = r[i].close;
      double b = r[i-1].open;

      SnrLevel lvl;
      lvl.lo           = MathMin(a, b) - buffer;
      lvl.hi           = MathMax(a, b) + buffer;
      lvl.formed       = r[i-1].time;
      lvl.isResistance = isRes;
      lvl.missCandles  = 0;
      lvl.stacked      = 1;
      lvl.spent        = false;

      // MISS: candles after formation whose wicks failed to reach the zone,
      // counted up to the first candle that actually touches it.
      for(int k = i - 2; k >= 0; k--)
        {
         bool touched = (r[k].low <= lvl.hi && r[k].high >= lvl.lo);
         if(touched) break;
         lvl.missCandles++;
        }

      // Stacking: another level already sitting on this price is a second
      // label on the same pool.
      double mid   = (lvl.lo + lvl.hi) / 2.0;
      bool   merged = false;
      for(int j = 0; j < ArraySize(g_levels); j++)
        {
         double existing = (g_levels[j].lo + g_levels[j].hi) / 2.0;
         if(MathAbs(existing - mid) <= tol)
           {
            g_levels[j].stacked++;
            g_levels[j].missCandles = MathMax(g_levels[j].missCandles, lvl.missCandles);
            merged = true;
            break;
           }
        }
      if(merged) continue;

      int n = ArraySize(g_levels);
      ArrayResize(g_levels, n + 1);
      g_levels[n] = lvl;
     }
  }

//+------------------------------------------------------------------+
bool InSession(datetime t)
  {
   if(!UseSessionFilter) return(true);
   MqlDateTime dt;
   TimeToStruct(t, dt);
   if(SessionStartHour <= SessionEndHour)
      return(dt.hour >= SessionStartHour && dt.hour < SessionEndHour);
   return(dt.hour >= SessionStartHour || dt.hour < SessionEndHour); // wraps midnight
  }

//+------------------------------------------------------------------+
int OpenPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber) n++;
     }
   return(n);
  }

//+------------------------------------------------------------------+
double SwingLow(const MqlRates &r[], int from, int count)
  {
   double v = r[from].low;
   for(int i = from; i < from + count && i < ArraySize(r); i++)
      v = MathMin(v, r[i].low);
   return(v);
  }

double SwingHigh(const MqlRates &r[], int from, int count)
  {
   double v = r[from].high;
   for(int i = from; i < from + count && i < ArraySize(r); i++)
      v = MathMax(v, r[i].high);
   return(v);
  }

//+------------------------------------------------------------------+
double LotFor(double stopDistance)
  {
   if(RiskPercent <= 0 || stopDistance <= 0) return(FixedLot);

   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash  = equity * RiskPercent / 100.0;
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0 || tickSize <= 0) return(FixedLot);

   double lossPerLot = (stopDistance / tickSize) * tickValue;
   if(lossPerLot <= 0) return(FixedLot);

   double lot  = riskCash / lossPerLot;
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step > 0) lot = MathFloor(lot / step) * step;
   lot = MathMax(minL, MathMin(maxL, lot));

   // Margin check — the broker steps leverage down as size rises, so ask
   // rather than assume.
   double margin = 0.0;
   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lot, ask, margin))
     {
      double free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
      while(lot > minL && margin > free * 0.9)
        {
         lot -= (step > 0 ? step : 0.01);
         if(!OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lot, ask, margin)) break;
        }
     }
   return(lot);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   MqlRates e[];
   ArraySetAsSeries(e, true);
   int copied = CopyRates(_Symbol, EntryTF, 0, SwingLookback + 5, e);
   if(copied < SwingLookback + 3) return;

   // Act once per closed entry bar; e[1] is the last completed candle.
   if(e[1].time == g_lastEntryBar) return;
   g_lastEntryBar = e[1].time;

   // Rebuild levels once per HTF bar.
   datetime htfBar = iTime(_Symbol, LevelTF, 0);
   if(htfBar != g_lastLevelBuild)
     {
      BuildLevels();
      g_lastLevelBuild = htfBar;
     }
   if(ArraySize(g_levels) == 0) return;

   if(!InSession(e[1].time)) return;
   if(OpenPositions() >= MaxOpenPositions) return;

   MqlRates touch = e[1];   // the candle that did the touching
   MqlRates prior = e[2];   // the one before it

   for(int i = 0; i < ArraySize(g_levels); i++)
     {
      if(g_levels[i].spent) continue;
      if(g_levels[i].missCandles < MinMissCandles) continue;   // rule 3
      if(g_levels[i].stacked     < MinStackedLabels) continue; // rule 5

      double lo = g_levels[i].lo, hi = g_levels[i].hi;

      bool wickIn = (touch.low <= hi && touch.high >= lo);
      if(!wickIn) continue;

      double bodyLo = MathMin(touch.open, touch.close);
      double bodyHi = MathMax(touch.open, touch.close);
      bool   bodyIn = (bodyLo <= hi && bodyHi >= lo);

      // rule 2 — a body-only touch is not a rejection
      if(RequireWickTouch && bodyIn && !(touch.low < lo || touch.high > hi)) continue;

      if(g_levels[i].isResistance)
        {
         // Selling a resistance touch: the wick must poke above and the body
         // must close back below the level.
         if(touch.high < hi) continue;
         bool closedThrough = (touch.close > hi);
         if(RequireSecondCandleHold)
           {
            // p21 r3 — first candle may close through, the next must not.
            if(closedThrough && prior.close > hi) continue;
           }
         else if(closedThrough) continue;   // stricter: no body close at all

         double stop  = SwingHigh(e, 1, SwingLookback) + StopBufferPoints * _Point;
         double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double dist  = stop - bid;
         if(dist <= 0) continue;
         double tp    = bid - dist * RewardMultiple;
         double lot   = LotFor(dist);

         if(trade.Sell(lot, _Symbol, 0.0, stop, tp, "JarvisSNR res"))
           {
            g_levels[i].spent = true;
            PrintFormat("SELL %s lot=%.2f stop=%.5f tp=%.5f miss=%d stacked=%d",
                        _Symbol, lot, stop, tp,
                        g_levels[i].missCandles, g_levels[i].stacked);
           }
         return;
        }
      else
        {
         if(touch.low > lo) continue;
         bool closedThrough = (touch.close < lo);
         if(RequireSecondCandleHold)
           {
            if(closedThrough && prior.close < lo) continue;
           }
         else if(closedThrough) continue;

         double stop  = SwingLow(e, 1, SwingLookback) - StopBufferPoints * _Point;
         double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double dist  = ask - stop;
         if(dist <= 0) continue;
         double tp    = ask + dist * RewardMultiple;
         double lot   = LotFor(dist);

         if(trade.Buy(lot, _Symbol, 0.0, stop, tp, "JarvisSNR sup"))
           {
            g_levels[i].spent = true;
            PrintFormat("BUY %s lot=%.2f stop=%.5f tp=%.5f miss=%d stacked=%d",
                        _Symbol, lot, stop, tp,
                        g_levels[i].missCandles, g_levels[i].stacked);
           }
         return;
        }
     }
  }
//+------------------------------------------------------------------+
