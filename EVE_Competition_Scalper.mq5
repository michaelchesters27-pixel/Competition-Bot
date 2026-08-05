#property copyright "EVE Competition Scalper"
#property version   "2.00"
#property strict
#property description "XAUUSD M5 autonomous competition EA: 60 minutes research, then 60 minutes trading."

#include <Trade/Trade.mqh>

input string RailwayBaseUrl = "https://competition-bot-production-9b15.up.railway.app";
input string ApiKey         = "replace-this-key";
input double FixedLots      = 0.01;
input ulong  MagicNumber    = 26080401;
input int    PulseSeconds   = 2;
input int    HistoryBars    = 420;
input int    SlippagePoints = 30;

CTrade trade;
string g_session_id      = "";
string g_launch_id       = "";
string g_phase           = "DISCONNECTED";
string g_strategy        = "";
string g_last_signal_id  = "";
datetime g_last_closed_bar_sent = 0;
int g_remaining_seconds  = 3600;

// -----------------------------------------------------------------------------
// Utility helpers
// -----------------------------------------------------------------------------
string TrimTrailingSlash(string value)
{
   while(StringLen(value)>0 && StringSubstr(value,StringLen(value)-1,1)=="/")
      value=StringSubstr(value,0,StringLen(value)-1);
   return value;
}

string JsonEscape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r","\\r");
   StringReplace(value,"\n","\\n");
   StringReplace(value,"\t","\\t");
   return value;
}

string BoolJson(bool value)
{
   return value ? "true" : "false";
}

string PriceString(double value)
{
   return DoubleToString(NormalizeDouble(value,_Digits),_Digits);
}

string FormatCountdown(int total_seconds)
{
   if(total_seconds<0)
      total_seconds=0;
   int minutes=total_seconds/60;
   int seconds=total_seconds%60;
   return StringFormat("%02d:%02d",minutes,seconds);
}

bool FixedLotIsSupported()
{
   double minimum=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   double maximum=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   double step=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   if(FixedLots<minimum-1e-9 || FixedLots>maximum+1e-9 || step<=0.0)
      return false;
   double steps=FixedLots/step;
   return MathAbs(steps-MathRound(steps))<1e-7;
}

string LaunchVariableName()
{
   return "EVE_COMP_V2_"+IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN))+"_"+IntegerToString((long)ChartID());
}

void LoadOrCreateLaunchId()
{
   string variable_name=LaunchVariableName();
   long launch_time=0;
   if(GlobalVariableCheck(variable_name))
      launch_time=(long)GlobalVariableGet(variable_name);
   if(launch_time<=0)
   {
      launch_time=(long)TimeGMT();
      GlobalVariableSet(variable_name,(double)launch_time);
   }
   g_launch_id=IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN))+"-"+
               IntegerToString((long)ChartID())+"-"+IntegerToString(launch_time);
}

void ClearLaunchIdForFreshAttachment(const int reason)
{
   if(reason==REASON_REMOVE || reason==REASON_RECOMPILE || reason==REASON_PARAMETERS ||
      reason==REASON_CHARTCHANGE || reason==REASON_CHARTCLOSE || reason==REASON_TEMPLATE ||
      reason==REASON_INITFAILED)
      GlobalVariableDel(LaunchVariableName());
}

int PostJson(string endpoint,string body,string &response)
{
   string base=TrimTrailingSlash(RailwayBaseUrl);
   string url=base+endpoint;
   string headers="Content-Type: application/json\r\nX-API-Key: "+ApiKey+"\r\n";
   char payload[];
   char result[];
   string response_headers="";

   StringToCharArray(body,payload,0,WHOLE_ARRAY,CP_UTF8);
   if(ArraySize(payload)>0)
      ArrayResize(payload,ArraySize(payload)-1);

   ResetLastError();
   int status=WebRequest("POST",url,headers,15000,payload,result,response_headers);
   response=CharArrayToString(result,0,-1,CP_UTF8);

   if(status==-1)
   {
      int error=GetLastError();
      Print("EVE WebRequest failed. Error ",error,
            ". Add this URL in MT5: Tools > Options > Expert Advisors > Allow WebRequest: ",base);
   }
   else if(status<200 || status>=300)
   {
      Print("EVE server returned HTTP ",status,": ",response);
   }
   return status;
}

bool ParseWire(string response,string &parts[])
{
   ArrayResize(parts,0);
   ushort separator=StringGetCharacter("|",0);
   int count=StringSplit(response,separator,parts);
   return count>0 && parts[0]=="OK";
}

// -----------------------------------------------------------------------------
// Session and market-data transport
// -----------------------------------------------------------------------------
bool StartOrResumeSession()
{
   string body="{";
   body += "\"account_login\":\""+IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN))+"\",";
   body += "\"symbol\":\""+JsonEscape(_Symbol)+"\",";
   body += "\"timeframe\":\"M5\",";
   body += "\"launch_id\":\""+JsonEscape(g_launch_id)+"\"";
   body += "}";

   string response="";
   int status=PostJson("/api/session/start",body,response);
   if(status<200 || status>=300)
      return false;

   string parts[];
   if(!ParseWire(response,parts) || ArraySize(parts)<6)
   {
      Print("EVE could not parse session response: ",response);
      return false;
   }

   g_session_id=parts[1];
   g_phase=parts[2];
   g_remaining_seconds=(int)StringToInteger(parts[4]);
   g_strategy=(parts[5]=="-" ? "" : parts[5]);
   Print("EVE session connected: ",g_session_id," phase=",g_phase);
   return true;
}

string BuildBarsJson()
{
   int requested=MathMax(100,HistoryBars);
   MqlRates rates[];
   ArraySetAsSeries(rates,true);
   int copied=CopyRates(_Symbol,PERIOD_M5,0,requested,rates);
   if(copied<=0)
      return "[]";

   string json="[";
   bool first=true;
   for(int i=copied-1;i>=0;i--)
   {
      if(!first)
         json += ",";
      first=false;
      json += "{";
      json += "\"time\":"+IntegerToString((long)rates[i].time)+",";
      json += "\"open\":"+DoubleToString(rates[i].open,_Digits)+",";
      json += "\"high\":"+DoubleToString(rates[i].high,_Digits)+",";
      json += "\"low\":"+DoubleToString(rates[i].low,_Digits)+",";
      json += "\"close\":"+DoubleToString(rates[i].close,_Digits)+",";
      json += "\"tick_volume\":"+IntegerToString((long)rates[i].tick_volume)+",";
      json += "\"spread\":"+IntegerToString((long)rates[i].spread);
      json += "}";
   }
   json += "]";
   return json;
}

datetime LastClosedBarTime()
{
   MqlRates rates[];
   ArraySetAsSeries(rates,true);
   if(CopyRates(_Symbol,PERIOD_M5,0,2,rates)<2)
      return 0;
   return rates[1].time;
}

void SendSignalAcknowledgement(string signal_id,bool accepted,string message)
{
   string body="{";
   body += "\"session_id\":\""+JsonEscape(g_session_id)+"\",";
   body += "\"signal_id\":\""+JsonEscape(signal_id)+"\",";
   body += "\"accepted\":"+BoolJson(accepted)+",";
   body += "\"message\":\""+JsonEscape(message)+"\"";
   body += "}";
   string response="";
   PostJson("/api/signal/ack",body,response);
}

void AdjustStopsForBroker(string action,double &sl,double &tp)
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick))
      return;
   long stops_level=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
   long freeze_level=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_FREEZE_LEVEL);
   double minimum=MathMax((double)stops_level,(double)freeze_level)*_Point;
   minimum=MathMax(minimum,2.0*_Point);

   if(action=="BUY")
   {
      if(sl>=tick.ask-minimum) sl=tick.ask-minimum;
      if(tp<=tick.ask+minimum) tp=tick.ask+minimum;
   }
   else if(action=="SELL")
   {
      if(sl<=tick.bid+minimum) sl=tick.bid+minimum;
      if(tp>=tick.bid-minimum) tp=tick.bid-minimum;
   }
   sl=NormalizeDouble(sl,_Digits);
   tp=NormalizeDouble(tp,_Digits);
}

bool ExecuteSignal(string action,string signal_id,double sl,double tp)
{
   if(g_phase!="TRADING")
   {
      SendSignalAcknowledgement(signal_id,false,"EA blocked the signal because the session is not in TRADING phase");
      return false;
   }

   AdjustStopsForBroker(action,sl,tp);
   string comment="EVE|"+signal_id;
   bool submitted=false;

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);

   if(action=="BUY")
      submitted=trade.Buy(FixedLots,_Symbol,0.0,sl,tp,comment);
   else if(action=="SELL")
      submitted=trade.Sell(FixedLots,_Symbol,0.0,sl,tp,comment);
   else
      return false;

   uint retcode=trade.ResultRetcode();
   bool accepted=submitted && (retcode==TRADE_RETCODE_DONE || retcode==TRADE_RETCODE_PLACED || retcode==TRADE_RETCODE_DONE_PARTIAL);
   string message=trade.ResultRetcodeDescription();
   SendSignalAcknowledgement(signal_id,accepted,message);

   if(accepted)
      Print("EVE ",action," accepted. Signal ",signal_id," SL=",PriceString(sl)," TP=",PriceString(tp));
   else
      Print("EVE ",action," rejected. Signal ",signal_id," retcode=",retcode," ",message);
   return accepted;
}

void SendPulse(bool force_bars=false)
{
   if(g_session_id=="")
   {
      if(!StartOrResumeSession())
         return;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick))
      return;

   datetime closed_bar=LastClosedBarTime();
   bool include_bars=force_bars || (closed_bar>0 && closed_bar!=g_last_closed_bar_sent);

   string body="{";
   body += "\"session_id\":\""+JsonEscape(g_session_id)+"\",";
   body += "\"bid\":"+DoubleToString(tick.bid,_Digits)+",";
   body += "\"ask\":"+DoubleToString(tick.ask,_Digits)+",";
   body += "\"terminal_time\":"+IntegerToString((long)TimeGMT())+",";
   body += "\"bars\":"+(include_bars ? BuildBarsJson() : "[]");
   body += "}";

   string response="";
   int status=PostJson("/api/pulse",body,response);
   if(status<200 || status>=300)
      return;

   string parts[];
   if(!ParseWire(response,parts) || ArraySize(parts)<12)
   {
      Print("EVE could not parse pulse response: ",response);
      return;
   }

   g_session_id=parts[1];
   g_phase=parts[2];
   g_remaining_seconds=(int)StringToInteger(parts[4]);
   g_strategy=(parts[5]=="-" ? "" : parts[5]);
   string action=parts[6];
   string signal_id=(parts[7]=="-" ? "" : parts[7]);
   double sl=StringToDouble(parts[8]);
   double tp=StringToDouble(parts[9]);

   if(include_bars && closed_bar>0)
      g_last_closed_bar_sent=closed_bar;

   if((action=="BUY" || action=="SELL") && signal_id!="" && signal_id!=g_last_signal_id)
   {
      g_last_signal_id=signal_id;
      ExecuteSignal(action,signal_id,sl,tp);
   }

   string strategy_text=(g_strategy=="" ? "Research in progress" : g_strategy);
   Comment(
      "EVE COMPETITION SCALPER\n",
      "Session: ",g_session_id,"\n",
      "Launch: ",g_launch_id,"\n",
      "Phase: ",g_phase,"\n",
      "Phase time remaining: ",FormatCountdown(g_remaining_seconds),"\n",
      "Strategy: ",strategy_text,"\n",
      "Symbol: ",_Symbol,"  Timeframe: M5  Lots: ",DoubleToString(FixedLots,2)
   );
}

// -----------------------------------------------------------------------------
// Deal reporting
// -----------------------------------------------------------------------------
string DealEntryName(ENUM_DEAL_ENTRY entry)
{
   if(entry==DEAL_ENTRY_IN) return "IN";
   if(entry==DEAL_ENTRY_OUT) return "OUT";
   if(entry==DEAL_ENTRY_INOUT) return "INOUT";
   if(entry==DEAL_ENTRY_OUT_BY) return "OUT_BY";
   return "UNKNOWN";
}

string DealTypeName(ENUM_DEAL_TYPE type)
{
   if(type==DEAL_TYPE_BUY) return "BUY";
   if(type==DEAL_TYPE_SELL) return "SELL";
   return "OTHER";
}

string SignalIdFromComment(string comment)
{
   if(StringFind(comment,"EVE|")==0)
      return StringSubstr(comment,4);
   return "";
}

void ReportDeal(ulong deal_ticket)
{
   if(g_session_id=="" || deal_ticket==0)
      return;
   if(!HistoryDealSelect(deal_ticket))
      return;

   ulong magic=(ulong)HistoryDealGetInteger(deal_ticket,DEAL_MAGIC);
   if(magic!=MagicNumber)
      return;

   ENUM_DEAL_TYPE deal_type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(deal_ticket,DEAL_TYPE);
   if(deal_type!=DEAL_TYPE_BUY && deal_type!=DEAL_TYPE_SELL)
      return;

   ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal_ticket,DEAL_ENTRY);
   string comment=HistoryDealGetString(deal_ticket,DEAL_COMMENT);
   string signal_id=SignalIdFromComment(comment);

   string body="{";
   body += "\"session_id\":\""+JsonEscape(g_session_id)+"\",";
   body += "\"signal_id\":\""+JsonEscape(signal_id)+"\",";
   body += "\"deal_ticket\":\""+IntegerToString((long)deal_ticket)+"\",";
   body += "\"order_ticket\":\""+IntegerToString((long)HistoryDealGetInteger(deal_ticket,DEAL_ORDER))+"\",";
   body += "\"position_id\":\""+IntegerToString((long)HistoryDealGetInteger(deal_ticket,DEAL_POSITION_ID))+"\",";
   body += "\"deal_type\":\""+DealTypeName(deal_type)+"\",";
   body += "\"entry_type\":\""+DealEntryName(entry)+"\",";
   body += "\"symbol\":\""+JsonEscape(HistoryDealGetString(deal_ticket,DEAL_SYMBOL))+"\",";
   body += "\"volume\":"+DoubleToString(HistoryDealGetDouble(deal_ticket,DEAL_VOLUME),2)+",";
   body += "\"price\":"+DoubleToString(HistoryDealGetDouble(deal_ticket,DEAL_PRICE),_Digits)+",";
   body += "\"profit\":"+DoubleToString(HistoryDealGetDouble(deal_ticket,DEAL_PROFIT),2)+",";
   body += "\"commission\":"+DoubleToString(HistoryDealGetDouble(deal_ticket,DEAL_COMMISSION),2)+",";
   body += "\"swap\":"+DoubleToString(HistoryDealGetDouble(deal_ticket,DEAL_SWAP),2)+",";
   body += "\"comment\":\""+JsonEscape(comment)+"\",";
   body += "\"deal_time\":"+IntegerToString((long)HistoryDealGetInteger(deal_ticket,DEAL_TIME));
   body += "}";

   string response="";
   PostJson("/api/deal",body,response);
}

// -----------------------------------------------------------------------------
// MT5 event handlers
// -----------------------------------------------------------------------------
int OnInit()
{
   if(_Period!=PERIOD_M5)
   {
      Alert("Attach EVE Competition Scalper to an M5 chart.");
      return INIT_PARAMETERS_INCORRECT;
   }
   string symbol_upper=_Symbol;
   StringToUpper(symbol_upper);
   if(StringFind(symbol_upper,"XAUUSD")!=0)
   {
      Alert("Attach EVE Competition Scalper to XAUUSD.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(!FixedLotIsSupported())
   {
      Alert("This broker does not support the exact fixed lot size ",DoubleToString(FixedLots,2)," on ",_Symbol,".");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(StringFind(RailwayBaseUrl,"YOUR-RAILWAY-DOMAIN")>=0)
   {
      Alert("Enter your real Railway domain in the EA inputs first.");
      return INIT_PARAMETERS_INCORRECT;
   }

   LoadOrCreateLaunchId();

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   int timer=MathMax(1,PulseSeconds);
   EventSetTimer(timer);
   StartOrResumeSession();
   SendPulse(true);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ClearLaunchIdForFreshAttachment(reason);
   Comment("");
}

void OnTimer()
{
   SendPulse(false);
}

void OnTradeTransaction(
   const MqlTradeTransaction &trans,
   const MqlTradeRequest &request,
   const MqlTradeResult &result
)
{
   if(trans.type==TRADE_TRANSACTION_DEAL_ADD && trans.deal>0)
      ReportDeal(trans.deal);
}
