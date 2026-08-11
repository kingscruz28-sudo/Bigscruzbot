//+------------------------------------------------------------------+
//|                                              JarvisBridge.mq5     |
//|                          BIGSCRUZ FX — file-bridge execution EA   |
//|         PATIENCE | DISCIPLINE | FEARLESS                          |
//+------------------------------------------------------------------+
#property copyright "BIGSCRUZ FX"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- inputs
input string SignalFile      = "jarvis_signal.txt"; // file in MQL5\Files (common folder)
input bool   UseCommonFolder = true;                // true = \Terminal\Common\Files
input int    PollSeconds     = 5;                   // how often to check the file
input double LotSize         = 0.01;                // fixed lot
input int    MagicNumber     = 280028;
input int    MaxSlippage     = 30;                  // points
input bool   RequireStacked  = true;                // only trade levels with 2+ labels
input bool   RequireSweep    = true;                // only trade WICK (no body close)
input int    MaxSignalAgeSec = 300;                 // ignore stale signals

string lastSignalID = "";

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaxSlippage);
   EventSetTimer(PollSeconds);
   Print("JarvisBridge armed. Watching: ", SignalFile);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }

//+------------------------------------------------------------------+
void OnTimer()
  {
   string raw = ReadSignalFile();
   if(raw == "") return;

   // expected pipe-delimited line:
   // id|timestamp|symbol|direction|verdict|labels|entry|sl|tp
   string f[];
   int n = StringSplit(raw, '|', f);
   if(n < 9) { Print("Bad signal format: ", raw); return; }

   string id        = f[0];
   long   ts        = (long)StringToInteger(f[1]);
   string symbol    = f[2];
   string direction = f[3];                    // BUY / SELL
   string verdict   = f[4];                    // WICK / BODY
   int    labels    = (int)StringToInteger(f[5]);
   double entry     = StringToDouble(f[6]);
   double sl        = StringToDouble(f[7]);
   double tp        = StringToDouble(f[8]);

   if(id == lastSignalID) return;              // already handled

   //--- freshness
   if(MaxSignalAgeSec > 0 && (TimeCurrent() - ts) > MaxSignalAgeSec)
     { Print("Stale signal ignored: ", id); lastSignalID = id; return; }

   //--- THE GATE ------------------------------------------------
   // 1. verdict must be a sweep: wick through, no 15m body close
   if(RequireSweep && verdict != "WICK")
     { Print("Body close = continuation. Standing down: ", id); lastSignalID = id; return; }

   // 2. level must be major: carrying 2+ labels (e.g. PDL + PWL)
   if(RequireStacked && labels < 2)
     { Print("Single-label level, not major liquidity. Standing down: ", id); lastSignalID = id; return; }
   //--------------------------------------------------------------

   if(PositionSelect(symbol))
     { Print("Position already open on ", symbol); lastSignalID = id; return; }

   bool ok = false;
   if(direction == "BUY")
      ok = trade.Buy(LotSize, symbol, 0.0, sl, tp, "Jarvis " + id);
   else if(direction == "SELL")
      ok = trade.Sell(LotSize, symbol, 0.0, sl, tp, "Jarvis " + id);
   else
      Print("Unknown direction: ", direction);

   if(ok) Print("EXECUTED ", direction, " ", symbol, " | ", id);
   else   Print("Order failed. Retcode: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());

   lastSignalID = id;
  }

//+------------------------------------------------------------------+
string ReadSignalFile()
  {
   int flags = FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE;
   if(UseCommonFolder) flags |= FILE_COMMON;

   int h = FileOpen(SignalFile, flags);
   if(h == INVALID_HANDLE) return("");

   string line = "";
   while(!FileIsEnding(h))
     {
      string s = FileReadString(h);
      StringTrimLeft(s); StringTrimRight(s);
      if(s != "") line = s;                    // keep last non-empty line
     }
   FileClose(h);
   return(line);
  }
//+------------------------------------------------------------------+
