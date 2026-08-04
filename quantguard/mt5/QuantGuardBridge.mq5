//+------------------------------------------------------------------+
//|                                         QuantGuardBridge.mq5      |
//|                                                                    |
//| Bridges QuantGuard to MT5/Exness (or any MT5 broker).             |
//|                                                                    |
//| WHAT THIS DOES:                                                    |
//| MT5/Exness has no simple REST API for placing trades from outside  |
//| the terminal, so this Expert Advisor runs INSIDE MT5 and does the  |
//| bridging itself:                                                    |
//|   1. Every PollIntervalSeconds, it asks QuantGuard's server:        |
//|      "any pending orders for me?" (GET /mt5/poll/{ApiKey})          |
//|   2. For each pending order, it places the trade using MT5's own    |
//|      native trading functions (CTrade) - the same way any manual    |
//|      or other EA trade would be placed on your real account.        |
//|   3. It reports the result back to QuantGuard                       |
//|      (POST /mt5/report/{ApiKey}/{signal_id}) - fill price, ticket   |
//|      number, or an error if the trade failed.                       |
//|                                                                      |
//| SETUP REQUIRED BEFORE THIS WILL WORK:                                |
//| 1. In MT5: Tools > Options > Expert Advisors > check "Allow          |
//|    WebRequest for listed URL" and add your QuantGuard server's       |
//|    URL (e.g. http://your-server:8000) to the list. WebRequest is     |
//|    blocked by default for security - MT5 will silently refuse to     |
//|    connect until this is done.                                      |
//| 2. Attach this EA to any chart (symbol doesn't matter - it trades    |
//|    whatever symbol each signal specifies, not just the chart's own). |
//| 3. Set the inputs below: your server URL and your QuantGuard API key.|
//| 4. Enable AutoTrading (the button in the MT5 toolbar) - without      |
//|    this, MT5 blocks all EA trading regardless of this script.        |
//|                                                                      |
//| IMPORTANT - NOT TESTED IN A REAL MT5 TERMINAL:                       |
//| This was written correctly to the best of available knowledge of     |
//| MQL5, but could not be compiled or run against a real MT5 terminal   |
//| while building it (no MetaTrader available in that environment).     |
//| Test carefully on a demo/testnet account before ever pointing this   |
//| at a live funded account, the same way the Binance connector was     |
//| proven out on testnet first before real money was ever involved.     |
//+------------------------------------------------------------------+
#property copyright "QuantGuard"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- Inputs (configure these in the EA's settings when you attach it)
input string ServerURL          = "http://your-server:8000";  // QuantGuard server address (no trailing slash)
input string ApiKey             = "qg_yourkeyhere";              // Your QuantGuard API key
input int    PollIntervalSeconds = 5;                              // How often to check for new orders
input string SymbolSuffix        = "";                              // Some brokers append a suffix to symbols
                                                                       // (e.g. "EURUSD.a" or "EURUSDm") - set that
                                                                       // suffix here if yours does; leave blank if not.

CTrade trade;

//+------------------------------------------------------------------+
//| Expert initialization                                              |
//+------------------------------------------------------------------+
int OnInit()
{
   if(StringLen(ApiKey) == 0 || ApiKey == "qg_yourkeyhere")
   {
      Print("QuantGuardBridge: Set your real ApiKey in the EA inputs before running.");
      return(INIT_PARAMETERS_INCORRECT);
   }
   EventSetTimer(PollIntervalSeconds);
   Print("QuantGuardBridge: started, polling ", ServerURL, " every ", PollIntervalSeconds, "s");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                            |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Timer - the main poll loop                                         |
//+------------------------------------------------------------------+
void OnTimer()
{
   PollForSignals();
}

//+------------------------------------------------------------------+
//| Poll QuantGuard for pending signals and execute each one           |
//+------------------------------------------------------------------+
void PollForSignals()
{
   string url = ServerURL + "/mt5/poll/" + ApiKey;
   string headers = "";
   char   post_data[];
   char   result[];
   string result_headers;
   int    timeout = 5000;

   ResetLastError();
   int res = WebRequest("GET", url, headers, timeout, post_data, result, result_headers);

   if(res == -1)
   {
      int err = GetLastError();
      if(err == 4060)
         Print("QuantGuardBridge ERROR: WebRequest blocked. Add '", ServerURL,
               "' to Tools > Options > Expert Advisors > Allow WebRequest for listed URL.");
      else
         Print("QuantGuardBridge ERROR: poll failed, error code ", err);
      return;
   }

   string response = CharArrayToString(result, 0, (int)ArraySize(result));
   ProcessSignalsResponse(response);
}

//+------------------------------------------------------------------+
//| Parse the {"signals": [...]} response and execute each signal      |
//| MQL5 has no built-in JSON library, so this is a small, targeted     |
//| parser for QuantGuard's specific, fixed response shape - not a      |
//| general-purpose JSON parser.                                        |
//+------------------------------------------------------------------+
void ProcessSignalsResponse(string response)
{
   int arrayStart = StringFind(response, "[");
   int arrayEnd   = StringFind(response, "]");
   if(arrayStart == -1 || arrayEnd == -1 || arrayEnd <= arrayStart)
      return; // no signals array found, or it's empty - nothing to do

   string arrayContent = StringSubstr(response, arrayStart + 1, arrayEnd - arrayStart - 1);
   if(StringLen(arrayContent) == 0)
      return; // empty array - no pending signals right now

   int pos = 0;
   while(pos < StringLen(arrayContent))
   {
      int objStart = StringFind(arrayContent, "{", pos);
      if(objStart == -1) break;
      int objEnd = StringFind(arrayContent, "}", objStart);
      if(objEnd == -1) break;

      string obj = StringSubstr(arrayContent, objStart, objEnd - objStart + 1);
      ExecuteSignalFromJson(obj);

      pos = objEnd + 1;
   }
}

//+------------------------------------------------------------------+
//| Extract fields from one signal's JSON object and place the trade   |
//+------------------------------------------------------------------+
void ExecuteSignalFromJson(string obj)
{
   int    signalId = (int)GetJsonNumber(obj, "id");
   string symbol   = GetJsonString(obj, "symbol") + SymbolSuffix;
   string side     = GetJsonString(obj, "side");
   double quantity = GetJsonNumber(obj, "quantity");

   Print("QuantGuardBridge: executing signal #", signalId, " ", side, " ", quantity, " ", symbol);

   bool ok;
   if(side == "BUY")
      ok = trade.Buy(quantity, symbol);
   else
      ok = trade.Sell(quantity, symbol);

   if(ok)
   {
      ulong  ticket    = trade.ResultOrder();
      double fillPrice = trade.ResultPrice();
      ReportResult(signalId, "FILLED", (string)ticket, fillPrice, "");
   }
   else
   {
      uint    errCode = trade.ResultRetcode();
      string errDesc = trade.ResultRetcodeDescription();
      Print("QuantGuardBridge: order failed for signal #", signalId, " - ", errDesc);
      ReportResult(signalId, "ERROR", "", 0.0, errDesc);
   }
}

//+------------------------------------------------------------------+
//| Report a signal's execution result back to QuantGuard              |
//+------------------------------------------------------------------+
void ReportResult(int signalId, string status, string ticket, double fillPrice, string errorMessage)
{
   string url = ServerURL + "/mt5/report/" + ApiKey + "/" + IntegerToString(signalId);
   string body = "{\"status\": \"" + status + "\"" +
                 ", \"mt5_ticket\": " + (ticket == "" ? "null" : ("\"" + ticket + "\"")) +
                 ", \"fill_price\": " + (fillPrice == 0.0 ? "null" : DoubleToString(fillPrice, 5)) +
                 ", \"error_message\": " + (errorMessage == "" ? "null" : ("\"" + errorMessage + "\"")) +
                 "}";

   char post_data[];
   StringToCharArray(body, post_data, 0, StringLen(body));
   char result[];
   string result_headers;
   string headers = "Content-Type: application/json\r\n";

   ResetLastError();
   int res = WebRequest("POST", url, headers, 5000, post_data, result, result_headers);
   if(res == -1)
      Print("QuantGuardBridge ERROR: failed to report result for signal #", signalId, ", error ", GetLastError());
}

//+------------------------------------------------------------------+
//| Minimal JSON field extraction helpers (fixed-shape parser)         |
//+------------------------------------------------------------------+
string GetJsonString(string json, string key)
{
   string search = "\"" + key + "\":\"";
   int start = StringFind(json, search);
   if(start == -1) return "";
   start += StringLen(search);
   int end = StringFind(json, "\"", start);
   if(end == -1) return "";
   return StringSubstr(json, start, end - start);
}

double GetJsonNumber(string json, string key)
{
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start == -1) return 0.0;
   start += StringLen(search);
   int end = start;
   while(end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if(c == ',' || c == '}') break;
      end++;
   }
   string numStr = StringSubstr(json, start, end - start);
   StringTrimLeft(numStr);
   StringTrimRight(numStr);
   return StringToDouble(numStr);
}
//+------------------------------------------------------------------+