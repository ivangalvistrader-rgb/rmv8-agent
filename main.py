"""
Portfolio Monitor v1.0 — 6 EAs FXIFY $15K + GetLeveraged $100K
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lee CSV del EACore_Logger y responde UNA pregunta:
¿El comportamiento live está dentro de lo esperado por el backtest?

EAs:
  CRv7     | GBPUSD H1  | RSI + EMA/SMA cruce           | Magic 8524
  RMv8     | CADCHF M30 | BB(30,2.4) + ATR(38)           | Magic 8903
  ScalpAsia| EURJPY M5  | Keltner + Williams %R          | Magic 4450
  LDBOX    | GDAXI M5   | Frankfurt/London box breakout  | Magic 123457
  MilkyWay | EURUSD H1  | BB + DeMarker + Stoch + MACD  | Magic 310116
  XAUMS    | XAUUSD M15 | EMA régimen + D1 H/L breakout  | Magic 9500
"""

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx, os, csv, io, json, re
from datetime import datetime, timezone
from collections import defaultdict

app = FastAPI(title="Portfolio Monitor", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GROQ_KEY = os.getenv("GROQ_KEY", "")

# ── Conocimiento completo del portafolio ─────────────────────────
PORTFOLIO = {
    "CRv7": {
        "name": "EA-CRv7", "symbol": "GBPUSD", "tf": "H1",
        "magic": 8524, "color": "#3b82f6",
        "strategy": "Trend: RSI(36)>50 setup + cruce EMA(69)/SMA(89) trigger",
        "entry_logic": "RSI barra1 cruza 50, EMA cruza sobre/bajo SMA en barra 2→1",
        "exit_logic": "TP fijo 186p + 1 cierre parcial 33% a +14p (PMSw=2)",
        "sl": 132, "tp": 186,
        "window_gmt": "00:00-18:00", "win_start": 0, "win_end": 18,
        "lots_fxpig": 0.20, "lots_getlev": 0.80,
        "IS": {"trades": 313, "wr": 78.0, "pf": 1.86, "dd": 4.41},
        "OOS": {"trades": 45, "wr": 80.0, "pf": 2.13, "dd": 3.33},
        "avg_duration_h": 113.8,
        "expected_trades_month": 4.4,
        "main_blocks": ["TIME_FILTER", "NO_RSI_SETUP", "NO_CROSS"],
        "alert_threshold_sl": 3,   # >3 SL en período = revisar
        "normal_zero_trade_weeks": 3,
    },
    "RMv8": {
        "name": "EA-RMv8", "symbol": "CADCHF", "tf": "M30",
        "magic": 8903, "color": "#a855f7",
        "strategy": "Mean Reversion: BB(30,2.4) + ATR(38)>0.0005",
        "entry_logic": "Precio CRUZA FUERA de BB + ATR(38) M30 > 0.0005",
        "exit_logic": "TP 30p fijo + ShrinkSL a +16p (reduce pérdida potencial)",
        "sl": 185, "tp": 30,
        "window_gmt": "09:00-20:00", "win_start": 9, "win_end": 20,
        "lots_fxpig": 0.10, "lots_getlev": 0.40,
        "IS": {"trades": 307, "wr": 92.18, "pf": 2.10, "dd": 3.01},
        "OOS": {"trades": 40, "wr": 95.0, "pf": 6.04, "dd": 1.72},
        "avg_duration_h": 116,
        "expected_trades_month": 3.3,
        "main_blocks": ["TIME_FILTER", "ATR_FILTER_BLOCK", "NO_TRIGGER"],
        "alert_threshold_sl": 2,
        "normal_zero_trade_weeks": 4,
    },
    "ScalpAsia": {
        "name": "EA-ScalpAsia", "symbol": "EURJPY", "tf": "M5",
        "magic": 4450, "color": "#14b8a6",
        "strategy": "Scalp Asian session: Keltner Channel + Williams %R",
        "entry_logic": "Close fuera de Keltner + WPR cruza nivel -81 (buy) o -35 (sell)",
        "exit_logic": "Trailing SL sobre Keltner (PMSw=5) — salida principalmente por SL dinámico",
        "sl_dynamic": True, "tp_dynamic": True,
        "window_gmt": "22:00-00:00 (23-07 backtest)", "win_start": 22, "win_end": 24,
        "lots_fxpig": 0.23, "lots_getlev": 0.90,
        "IS": {"trades": 294, "wr": 74.5, "pf": 2.58, "dd": 4.41},
        "OOS": {"trades": 60, "wr": 76.7, "pf": 2.78, "dd": 2.49},
        "avg_duration_h": 8.9,
        "expected_trades_month": 12,
        "main_blocks": ["TIME_FILTER", "NO_KELTNER_BREAK", "NO_WPR_CROSS"],
        "alert_threshold_sl": 8,   # alta frecuencia — más SLs son normales
        "normal_zero_trade_weeks": 1,
        "note": "StrHr live=22 GMT (backtest StrHr=0 por offset UTC+2 de Darwinex)",
    },
    "LDBOX": {
        "name": "EA-LDBOX", "symbol": "GDAXI", "tf": "M5",
        "magic": 123457, "color": "#f97316",
        "strategy": "Breakout: Caja Frankfurt/London + EMA bias H1 + 1 trade/día",
        "entry_logic": "Breakout de la caja 09-15h GMT + vela body válida + EMA H1 bias + distancia máx 22u",
        "exit_logic": "SL dinámico (2.5x rango) + TP dinámico (4x rango) + ShrinkSL",
        "sl_dynamic": True, "tp_dynamic": True,
        "window_gmt": "09:00-15:00", "win_start": 9, "win_end": 15,
        "lots_fxpig": "RiskPct=0.72%", "lots_getlev": "RiskPct=0.75%",
        "IS": {"trades": 498, "wr": 46.0, "pf": 1.48, "dd": 5.71},
        "OOS": {"trades": 75, "wr": 50.7, "pf": 2.03, "dd": 2.52},
        "avg_duration_h": 36,
        "expected_trades_month": 10.7,
        "main_blocks": ["TIME_FILTER", "BOX_INVALID", "SPREAD_HIGH", "NO_BREAKOUT"],
        "alert_threshold_sl": 12,
        "normal_zero_trade_weeks": 1,
        "note": "1 trade por día máximo. Box range válido: 11-135 unidades",
    },
    "MilkyWay": {
        "name": "EA-MilkyWay", "symbol": "EURUSD", "tf": "H1",
        "magic": 310116, "color": "#ec4899",
        "strategy": "Mean Reversion: BB(32) + DeMarker(9) + Stoch(8,7,31) + MACD(44,38,31)",
        "entry_logic": "Precio fuera de BB(32) + MaxCandle<140pts + DeM + Amplitude(18) confirman agotamiento",
        "exit_logic": "Stoch cruza StochB(71) O MACD cambia signo — sin TP fijo",
        "sl": "Dinámico: ATR histórico + 40 offset [8-135p]", "tp": "Sin TP fijo",
        "window_gmt": "Sin filtro horario activo", "win_start": 0, "win_end": 24,
        "lots_fxpig": 0.25, "lots_getlev": "retirado",
        "IS": {"trades": 260, "wr": 61.2, "pf": 1.46, "dd": 4.70},
        "OOS": {"trades": 36, "wr": 66.7, "pf": 4.72, "dd": 2.21},
        "avg_duration_h": 72,
        "expected_trades_month": 5,
        "main_blocks": ["SPIKE_FILTER", "PATTERN_BLOCKER", "NO_BB_BREAK", "DEM_FILTER"],
        "alert_threshold_sl": 4,
        "normal_zero_trade_weeks": 2,
        "note": "OOS PF=4.72 inflado (36 trades). Referencia real es IS PF=1.46. Retirado de GetLeveraged.",
    },
    "XAUMS": {
        "name": "EA-XAUMS", "symbol": "XAUUSD", "tf": "M15",
        "magic": 9500, "color": "#eab308",
        "strategy": "Breakout: EMA régimen H4 + ruptura High/Low D1 + confirmación ATR M15",
        "entry_logic": "Régimen H4 EMA(31) ±1.4% → ruptura del H/L del anteayer + cuerpo vela ≥ ATR(34)",
        "exit_logic": "SL = 2.2x ATR_H1(32), TP = 1.5x SL (ratio 1:1.5). BUY/SELL asimétrico (SellATRMult=2.1x)",
        "sl_dynamic": True, "tp_dynamic": True,
        "window_gmt": "12:00-17:00", "win_start": 12, "win_end": 17,
        "lots_fxpig": "RiskPct=0.70%", "lots_getlev": "RiskPct=0.75%",
        "IS": {"trades": 170, "wr": 52.4, "pf": 1.61, "dd": 5.38},
        "OOS": {"trades": 35, "wr": 60.0, "pf": 2.24, "dd": 1.81},
        "avg_duration_h": 24,
        "expected_trades_month": 4.9,
        "main_blocks": ["TIME_FILTER", "REGIME_NEUTRAL", "NO_TRIGGER_BREAK", "NO_ATR_CONFIRM"],
        "alert_threshold_sl": 5,
        "normal_zero_trade_weeks": 2,
    },
}

# Magic number → EA key
MAGIC_MAP = {
    8524: "CRv7", 8903: "RMv8", 4450: "ScalpAsia",
    123457: "LDBOX", 310116: "MilkyWay", 9500: "XAUMS",
}

# ── Estado en memoria ────────────────────────────────────────────
LOG_STORE: dict[str, dict] = {}   # ea_key → parsed data

# ════════════════════════════════════════════════════════════════
#  PARSER CSV — EACore_Logger universal
# ════════════════════════════════════════════════════════════════

def detect_ea(rows: list[dict]) -> str | None:
    """Detecta qué EA generó el log por Magic, EA name, o símbolo."""
    for row in rows[:20]:
        ea_col = row.get("EA", "").strip()
        sym    = row.get("Symbol", "").replace(".r", "").strip()
        det    = row.get("Detalle", "")

        # Por nombre directo
        for key in PORTFOLIO:
            if key.lower() in ea_col.lower():
                return key

        # Por símbolo
        sym_map = {
            "GBPUSD": "CRv7", "CADCHF": "RMv8", "EURJPY": "ScalpAsia",
            "GDAXI": "LDBOX", "GER30": "LDBOX",
            "EURUSD": "MilkyWay", "XAUUSD": "XAUMS",
        }
        if sym in sym_map:
            return sym_map[sym]

        # Por Magic en INIT
        if "Magic" in det or "MagicStart" in det or "magic" in det.lower():
            for magic, key in MAGIC_MAP.items():
                if str(magic) in det:
                    return key

    return None

def parse_log(content: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(content)))
    if not rows:
        return {"error": "CSV vacío o sin cabecera válida"}

    ea_key = detect_ea(rows)
    if not ea_key:
        return {"error": "No se pudo identificar el EA. Verifica que el CSV sea del EACore_Logger"}

    ea_info = PORTFOLIO[ea_key]

    # Acumuladores
    atrs, bbls, bbus, prices, spreads = [], [], [], [], []
    blocks = defaultdict(int)
    filter_passes = defaultdict(int)
    trades_open = []
    trades_close = []
    inits = []
    indicators_rows = []
    bar_count = 0

    for row in rows:
        ev  = row.get("Evento", "").strip()
        det = row.get("Detalle", "").strip()
        dt  = row.get("DateTime", "").strip()
        spd = row.get("Spread_p", "")
        pnl_raw = row.get("PnL_USD", "")

        # INIT
        if ev == "INIT":
            inits.append({"dt": dt, "detail": det})

        # Barras evaluadas
        if ev in ("BAR", "INDICATORS", "FILTER_PASS", "FILTER_BLOCK", "BLOCK"):
            bar_count += 1

        # ATR (RMv8 y otros)
        if "ATR1=" in det:
            try:
                v = float(det.split("ATR1=")[1].split(" ")[0].rstrip(","))
                if v > 0: atrs.append(v)
            except: pass
        if "ATR=" in det and "ATR1=" not in det:
            try:
                v = float(det.split("ATR=")[1].split(" ")[0].rstrip(","))
                if v > 0: atrs.append(v)
            except: pass

        # BB
        if "BBLo1=" in det:
            try:
                bbls.append(float(det.split("BBLo1=")[1].split(" ")[0]))
                bbus.append(float(det.split("BBUp1=")[1].split(" ")[0]))
            except: pass

        # Precio C1
        if "C1=" in det:
            try:
                v = float(det.split("C1=")[1].split(" ")[0].split(",")[0])
                if v > 0.1: prices.append(v)
            except: pass
        if "Close=" in det:
            try:
                v = float(det.split("Close=")[1].split(" ")[0].split(",")[0])
                if v > 0.1: prices.append(v)
            except: pass

        # Spread
        if spd:
            try: spreads.append(float(spd))
            except: pass

        # Bloqueadores
        if ev == "BLOCK":
            blocks[det] += 1
        if ev == "FILTER_BLOCK":
            for kw in ["TIME", "ATR", "SPREAD", "BB", "RSI", "CROSS", "BOX",
                       "REGIME", "TRIGGER", "PATTERN", "SPIKE", "DIST"]:
                if kw in det.upper():
                    blocks[kw + "_BLOCK"] += 1
                    break

        # Trades
        if ev == "TRADE_OPEN" or "OPEN_BUY" in ev or "OPEN_SELL" in ev:
            try:
                price_v = float(row.get("Price", 0) or 0)
                lots_v  = float(row.get("Lots", 0) or 0)
            except: price_v = lots_v = 0
            trades_open.append({
                "dt": dt, "dir": "BUY" if "BUY" in det.upper() else "SELL",
                "price": price_v, "lots": lots_v, "detail": det,
            })

        if ev in ("EXIT_TP", "EXIT_SL", "TRADE_CLOSE", "EXIT_TIME", "WIN", "LOSS"):
            try: pnl = float(pnl_raw)
            except: pnl = 0.0
            trades_close.append({
                "dt": dt, "exit_type": ev, "pnl": pnl, "detail": det,
            })

    # Diagnóstico
    total_blocks = sum(blocks.values())
    total_trades = len(trades_open)
    realized_pnl = sum(t["pnl"] for t in trades_close)
    sl_trades    = [t for t in trades_close if "SL" in t["exit_type"] or t["pnl"] < 0]
    tp_trades    = [t for t in trades_close if "TP" in t["exit_type"] or t["pnl"] > 0]

    oos = ea_info["OOS"]
    expected_per_session = oos["expected_trades_month"] if "expected_trades_month" in oos else oos["trades"] / 12

    # Estado principal
    dominant_block = max(blocks, key=blocks.get) if blocks else "ninguno"

    # ¿Es normal?
    def assess_status():
        if total_trades == 0 and total_blocks > 0:
            if dominant_block in ea_info["main_blocks"] or any(
                b in dominant_block for b in ["TIME", "ATR", "SPREAD"]
            ):
                return "NORMAL", f"0 trades — bloqueado por {dominant_block} (esperado según backtest)"
            return "REVISAR", f"0 trades — bloqueador inusual: {dominant_block}"
        if len(sl_trades) > ea_info["alert_threshold_sl"]:
            return "ACCIÓN", f"{len(sl_trades)} SLs — supera umbral de {ea_info['alert_threshold_sl']} para este EA"
        if total_trades > 0:
            return "NORMAL", f"{total_trades} trades detectados — actividad dentro de lo esperado"
        return "NORMAL", "Sin actividad — período sin condiciones"

    status, status_reason = assess_status()

    # Filas de inicio
    first_dt = inits[0]["dt"] if inits else (rows[0].get("DateTime","") if rows else "")
    last_dt  = rows[-1].get("DateTime","") if rows else ""

    return {
        "ea_key": ea_key,
        "ea_name": ea_info["name"],
        "symbol": ea_info["symbol"],
        "tf": ea_info["tf"],
        "strategy": ea_info["strategy"],
        "period_start": first_dt,
        "period_end":   last_dt,
        "bar_count": bar_count,
        "trades_open": len(trades_open),
        "trades_close": len(trades_close),
        "sl_count": len(sl_trades),
        "tp_count": len(tp_trades),
        "realized_pnl": round(realized_pnl, 2),
        "blocks": dict(blocks),
        "dominant_block": dominant_block,
        "total_blocks": total_blocks,
        "atr_avg": round(sum(atrs)/len(atrs), 6) if atrs else None,
        "atr_min": round(min(atrs), 6) if atrs else None,
        "atr_max": round(max(atrs), 6) if atrs else None,
        "price_last": round(prices[-1], 5) if prices else None,
        "bbl_avg": round(sum(bbls)/len(bbls), 5) if bbls else None,
        "bbu_avg": round(sum(bbus)/len(bbus), 5) if bbus else None,
        "spread_avg": round(sum(spreads)/len(spreads), 2) if spreads else None,
        "spread_max": round(max(spreads), 2) if spreads else None,
        "status": status,
        "status_reason": status_reason,
        "recent_trades": trades_open[-5:],
        "recent_closes": trades_close[-5:],
        "IS": ea_info["IS"],
        "OOS": ea_info["OOS"],
        "expected_trades_month": ea_info.get("expected_trades_month"),
        "alert_threshold_sl": ea_info["alert_threshold_sl"],
        "normal_zero_trade_weeks": ea_info["normal_zero_trade_weeks"],
        "inits": inits,
    }

# ════════════════════════════════════════════════════════════════
#  GROQ — análisis experto por EA
# ════════════════════════════════════════════════════════════════

SYSTEM_PORTFOLIO = """Eres el sistema de monitoreo experto de un portafolio de 6 Expert Advisors en MetaTrader 5, operando una cuenta FXIFY $15K (DD diario máx 5%) y GetLeveraged $100K (DD diario máx 3%).

REGLA OPERATIVA FUNDAMENTAL:
Los EAs están en período de observación de 3 meses. NO se toca ningún EA. Solo se monitorea.
La pregunta que respondes es ÚNICA: ¿Está el comportamiento live dentro de lo que predice el backtest?

LOS 6 EAs DEL PORTAFOLIO:
1. EA-CRv7  | GBPUSD H1  | RSI(36)+EMA(69)/SMA(89) | 4.4 trades/mes | WR OOS 80%
2. EA-RMv8  | CADCHF M30 | BB(30,2.4)+ATR(38)>0.0005 | 3.3 trades/mes | WR OOS 95%
3. EA-ScalpAsia | EURJPY M5 | Keltner+WPR | 12 trades/mes | WR OOS 77%
4. EA-LDBOX | GDAXI M5   | Box Frankfurt/London | 10.7 trades/mes | WR OOS 51%
5. EA-MilkyWay | EURUSD H1 | BB+DeM+Stoch+MACD | 5 trades/mes | WR OOS 67%
6. EA-XAUMS | XAUUSD M15 | EMA régimen H4 + D1 breakout | 4.9 trades/mes | WR OOS 60%

PortfolioRM: máximo 4 trades simultáneos. CRv7 y RMv8 solapan 91.9% del tiempo (núcleo permanente).

Tus respuestas deben ser:
1. Directas: ¿Normal, Revisar o Acción?
2. Específicas: menciona el bloqueador exacto y por qué es o no es esperado
3. Comparativas: contrasta con el backtest OOS real
4. Concisas: máximo 200 palabras por EA

Termina SIEMPRE con:
ESTADO: [NORMAL|REVISAR|ACCIÓN] — [razón en una frase]"""

def build_ea_prompt(data: dict) -> str:
    ea = PORTFOLIO.get(data["ea_key"], {})
    blocks_str = ", ".join(f"{k}:{v}" for k,v in data["blocks"].items()) or "ninguno"
    recent_opens = [f"{t['dt']} {t['dir']} @{t['price']}" for t in data.get("recent_trades", [])]
    recent_closes = [f"{t['dt']} {t['exit_type']} PnL={t['pnl']}" for t in data.get("recent_closes", [])]

    atr_info = ""
    if data.get("atr_avg"):
        atr_level = 0.0005 if data["ea_key"] == "RMv8" else None
        if atr_level:
            pct = data["atr_avg"]/atr_level*100
            atr_info = f"\nATR(38) M30: {data['atr_avg']:.6f} ({pct:.0f}% del umbral 0.0005)"
        else:
            atr_info = f"\nATR observado: {data['atr_avg']:.6f} (min:{data['atr_min']:.6f} max:{data['atr_max']:.6f})"

    return f"""ANÁLISIS {data['ea_name']} — {data['symbol']} {data['tf']}
Período: {data['period_start']} → {data['period_end']}
Barras evaluadas: {data['bar_count']}

ACTIVIDAD:
Trades abiertos: {data['trades_open']}
Trades cerrados: {data['trades_close']} (TP:{data['tp_count']} SL:{data['sl_count']})
PnL realizado: ${data['realized_pnl']}
{atr_info}
Spread avg/max: {data.get('spread_avg','—')}p / {data.get('spread_max','—')}p

BLOQUEADORES:
{blocks_str}
Dominante: {data['dominant_block']} ({data['total_blocks']} eventos totales)

BACKTEST REFERENCIA OOS:
WR: {data['OOS']['wr']}% | PF: {data['OOS']['pf']} | DD máx: {data['OOS']['dd']}%
Trades esperados/mes: {data.get('expected_trades_month','?')}
Umbral SL alerta: {data['alert_threshold_sl']} SLs en período
Normal con 0 trades hasta: {data.get('normal_zero_trade_weeks','?')} semanas

ESTRATEGIA: {ea.get('strategy','')}
ENTRADA: {ea.get('entry_logic','')}
SALIDA: {ea.get('exit_logic','')}

Trades recientes: {'; '.join(recent_opens) or 'ninguno'}
Cierres recientes: {'; '.join(recent_closes) or 'ninguno'}

ESTADO DETERMINISTA: {data['status']} — {data['status_reason']}

Analiza si el comportamiento es consistente con el backtest OOS."""

async def call_groq(prompt: str) -> dict:
    if not GROQ_KEY:
        return {"analysis": "GROQ_KEY no configurado", "status": "REVISAR", "reason": "Sin API key"}
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 600, "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PORTFOLIO},
            {"role": "user", "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers=headers, json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    status, reason = "NORMAL", ""
    for line in reversed(text.split("\n")):
        if line.strip().upper().startswith("ESTADO:"):
            rest = line.split(":", 1)[1].strip()
            for s in ("ACCIÓN", "ACCION", "REVISAR", "NORMAL"):
                if s in rest.upper():
                    status = "ACCIÓN" if "ACCI" in s else s
                    reason = rest[rest.upper().find(s)+len(s):].strip(" —-")
                    break
            break

    clean = "\n".join(l for l in text.split("\n")
                      if not l.strip().upper().startswith("ESTADO:")).strip()
    return {"analysis": clean, "status": status, "reason": reason}

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "1.0",
        "groq_set": bool(GROQ_KEY),
        "logs_loaded": list(LOG_STORE.keys()),
        "portfolio": list(PORTFOLIO.keys()),
    }

@app.get("/portfolio_info")
async def portfolio_info():
    """Toda la info estática del portafolio para el frontend."""
    return {ea: {
        "name": info["name"], "symbol": info["symbol"], "tf": info["tf"],
        "color": info["color"], "strategy": info["strategy"],
        "IS": info["IS"], "OOS": info["OOS"],
        "expected_trades_month": info.get("expected_trades_month"),
        "window_gmt": info.get("window_gmt"),
        "lots_fxpig": str(info.get("lots_fxpig", "—")),
        "sl": str(info.get("sl", "Dinámico")),
        "tp": str(info.get("tp", "Dinámico")),
    } for ea, info in PORTFOLIO.items()}

@app.post("/upload/{ea_key}")
async def upload(ea_key: str, file: UploadFile = File(...)):
    """Sube CSV de un EA específico."""
    if ea_key not in PORTFOLIO and ea_key != "auto":
        return JSONResponse({"error": f"EA '{ea_key}' no reconocido"}, status_code=400)
    content = (await file.read()).decode("utf-8", errors="replace")
    data = parse_log(content)
    if "error" in data:
        return JSONResponse({"error": data["error"]}, status_code=422)
    # Si auto-detectó, guardar con la key detectada
    key = data.get("ea_key", ea_key)
    LOG_STORE[key] = data
    return {"status": "ok", "ea_key": key, "ea_name": data["ea_name"],
            "trades": data["trades_open"], "status_det": data["status"]}

@app.get("/status/{ea_key}")
async def status(ea_key: str):
    """Datos parseados de un EA."""
    if ea_key not in LOG_STORE:
        return JSONResponse({"error": "Sin log cargado para este EA"}, status_code=404)
    d = LOG_STORE[ea_key]
    return {k: v for k, v in d.items() if k not in ("recent_trades", "recent_closes")}

@app.get("/analyze/{ea_key}")
async def analyze(ea_key: str):
    """Datos + análisis Groq de un EA."""
    if ea_key not in LOG_STORE:
        return JSONResponse({"error": "Sin log cargado"}, status_code=404)
    data   = LOG_STORE[ea_key]
    prompt = build_ea_prompt(data)
    groq_r = await call_groq(prompt)
    return {**data, **groq_r}

@app.get("/analyze_all")
async def analyze_all():
    """Analiza todos los EAs con log cargado."""
    if not LOG_STORE:
        return JSONResponse({"error": "No hay logs cargados"}, status_code=404)
    results = {}
    import asyncio
    tasks = {k: call_groq(build_ea_prompt(v)) for k, v in LOG_STORE.items()}
    for key, task in tasks.items():
        groq_r = await task
        results[key] = {**LOG_STORE[key], **groq_r}
    return results

@app.get("/portfolio_status")
async def portfolio_status():
    """Resumen rápido de todos los EAs (sin Groq)."""
    result = {}
    for key, info in PORTFOLIO.items():
        if key in LOG_STORE:
            d = LOG_STORE[key]
            result[key] = {
                "status": d["status"], "reason": d["status_reason"],
                "trades": d["trades_open"], "sl": d["sl_count"],
                "blocks": d["dominant_block"], "loaded": True,
            }
        else:
            result[key] = {"status": "SIN_DATOS", "loaded": False}
    return result

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open("index.html") as f:
        return f.read()
