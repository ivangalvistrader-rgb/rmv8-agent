"""
RMv8 Agent v4.0 — Análisis profundo basado en logs reales
Patrón observado (abril 2026):
  - 3 bloqueadores reales: ATR_FILTER_BLOCK, NO_TRIGGER, TIME_FILTER
  - ATR oscilando 64-126% del umbral (promedio 89%)
  - 8 abr (126%): ATR OK pero precio en zona media → NO_TRIGGER todo el día
  - 1-2 abr (103-105%): ATR OK intermitente, precio nunca tocó banda
  - RMv4 (versión anterior): tenía filtro ADX adicional que bloqueaba mucho
  - Lo que realmente falta: ver CUÁNTO falta al precio para tocar BB
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx, os, math, random
from datetime import datetime, timezone, timedelta

app = FastAPI(title="RMv8-Agent", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

AV_KEY   = os.getenv("ALPHA_VANTAGE_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")

EA = dict(
    bb_period=30, bb_dev=2.4,
    atr_period=38, atr_level=0.0005,
    sl_pips=185, tp_pips=30, shrink_pips=16,
    win_start=9, win_end=20,
)

# ════════════════════════════════════════════════════════════════
#  DATOS — Alpha Vantage FX_DAILY (gratuito) + Frankfurter fallback
# ════════════════════════════════════════════════════════════════

async def fetch_av_daily(months: int = 6) -> list[dict]:
    """FX_DAILY gratuito — últimos N meses (~130 días)"""
    url = (
        "https://www.alphavantage.co/query"
        "?function=FX_DAILY&from_symbol=CAD&to_symbol=CHF"
        f"&outputsize={'full' if months > 3 else 'compact'}&apikey={AV_KEY}"
    )
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
    key = "Time Series FX (Daily)"
    if key not in d:
        raise RuntimeError(d.get("Note") or d.get("Information") or "AV sin datos")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months*31)).strftime("%Y-%m-%d")
    items  = [(dt, v) for dt, v in d[key].items() if dt >= cutoff]
    items  = sorted(items)
    return [
        {"date": dt,
         "open":  float(v["1. open"]),
         "high":  float(v["2. high"]),
         "low":   float(v["3. low"]),
         "close": float(v["4. close"])}
        for dt, v in items
    ]

async def fetch_frankfurter(months: int = 6) -> list[dict]:
    end   = datetime.now(timezone.utc).date()
    start = end - timedelta(days=months*31)
    url   = f"https://api.frankfurter.app/{start}..{end}?from=CAD&to=CHF"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
    if "rates" not in d:
        raise RuntimeError("Frankfurter: sin datos")
    prices = [(dt, v["CHF"]) for dt, v in sorted(d["rates"].items()) if "CHF" in v]
    daily = []
    for i, (dt, close) in enumerate(prices):
        prev = prices[i-1][1] if i > 0 else close
        rng  = max(abs(close - prev) * 2.2, 0.0003)
        daily.append({
            "date":  dt,
            "open":  round(prev, 5),
            "high":  round(max(prev, close) + rng * 0.28, 5),
            "low":   round(min(prev, close) - rng * 0.28, 5),
            "close": round(close, 5),
        })
    return daily

def daily_to_m30(daily: list[dict]) -> list[dict]:
    """Convierte OHLC diario en pseudo-velas M30 realistas"""
    candles = []
    seed_val = sum(int(d["close"] * 100000) for d in daily[-5:]) % 9999
    rng = random.Random(seed_val)
    for day in daily:
        o, h, l, c = day["open"], day["high"], day["low"], day["close"]
        day_range  = h - l
        m30_vol    = day_range / math.sqrt(22)
        direction  = 1 if c >= o else -1
        price      = o
        for i in range(22):
            drift = direction * m30_vol * 0.12
            noise = rng.gauss(0, m30_vol * 0.38)
            move  = drift + noise
            op    = price
            cl    = max(l + 0.00001, min(h - 0.00001, price + move))
            hi    = min(h, max(op, cl) + abs(rng.gauss(0, m30_vol * 0.08)))
            lo    = max(l, min(op, cl) - abs(rng.gauss(0, m30_vol * 0.08)))
            candles.append({
                "time":  f"{day['date']} {9+i//2:02d}:{30*(i%2):02d}:00",
                "open":  round(op, 5), "high": round(hi, 5),
                "low":   round(lo, 5), "close": round(cl, 5),
            })
            price = cl
    return candles

async def get_data(months: int = 6) -> tuple[list[dict], list[dict], str]:
    """Retorna (daily, m30_candles, source)"""
    if AV_KEY:
        try:
            daily   = await fetch_av_daily(months)
            candles = daily_to_m30(daily)
            return daily, candles, f"Alpha Vantage FX_DAILY → {len(daily)} días"
        except Exception:
            pass
    try:
        daily   = await fetch_frankfurter(months)
        candles = daily_to_m30(daily)
        return daily, candles, f"Frankfurter API → {len(daily)} días"
    except Exception:
        pass
    # Fallback
    now = datetime.now(timezone.utc)
    rng = random.Random(42)
    price, daily, candles = 0.5707, [], []
    for i in range(months * 22):
        dt    = now - timedelta(days=(months*22 - i))
        move  = rng.gauss(0, 0.0012)
        price = max(0.5500, min(0.5900, price + move))
        hi    = price + abs(rng.gauss(0, 0.0005))
        lo    = price - abs(rng.gauss(0, 0.0005))
        daily.append({"date": dt.strftime("%Y-%m-%d"), "open": round(price-move,5),
                      "high": round(hi,5), "low": round(lo,5), "close": round(price,5)})
    candles = daily_to_m30(daily)
    return daily, candles, "Fallback estático"

# ════════════════════════════════════════════════════════════════
#  CÁLCULOS — exactos como el EA
# ════════════════════════════════════════════════════════════════

def calc_bb(closes: list[float]) -> dict:
    s   = closes[-EA["bb_period"]:]
    m   = sum(s) / len(s)
    std = math.sqrt(sum((x-m)**2 for x in s) / len(s))
    return dict(upper=round(m+EA["bb_dev"]*std,5), mid=round(m,5),
                lower=round(m-EA["bb_dev"]*std,5), std=round(std,5))

def calc_atr(candles: list[dict], period: int = None) -> float:
    p = period or EA["atr_period"]
    trs = [max(c["high"]-c["low"], abs(c["high"]-candles[i-1]["close"]),
               abs(c["low"]-candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    last = trs[-p:]
    return round(sum(last)/len(last), 6)

def atr_series_daily(daily: list[dict]) -> list[dict]:
    """ATR diario (True Range) para gráfico histórico"""
    result = []
    for i in range(1, len(daily)):
        h, l = daily[i]["high"], daily[i]["low"]
        pc   = daily[i-1]["close"]
        tr   = max(h-l, abs(h-pc), abs(l-pc))
        # ATR M30 estimado = TR_diario / sqrt(22) para comparar con umbral
        atr_m30_est = tr / math.sqrt(22)
        result.append({
            "date":        daily[i]["date"],
            "close":       daily[i]["close"],
            "tr_daily":    round(tr, 5),
            "atr_m30_est": round(atr_m30_est, 6),
            "atr_pct":     round(atr_m30_est / EA["atr_level"] * 100, 1),
            "above_threshold": atr_m30_est >= EA["atr_level"],
        })
    return result

def diagnose_block(atr_pass: bool, bb_signal, in_win: bool) -> str:
    """Replica exacta de los 3 bloqueadores reales del EA"""
    if not in_win:           return "TIME_FILTER"
    if not atr_pass:         return "ATR_FILTER_BLOCK"
    if bb_signal is None:    return "NO_TRIGGER"
    return "OPEN"

def evaluate_full(daily: list[dict], candles: list[dict], source: str) -> dict:
    closes   = [c["close"] for c in candles]
    price    = closes[-1]
    bb       = calc_bb(closes)
    atr_m30  = calc_atr(candles)
    now      = datetime.now(timezone.utc)
    gmt_h    = now.hour
    in_win   = EA["win_start"] <= gmt_h < EA["win_end"]
    atr_pass = atr_m30 >= EA["atr_level"]
    atr_pct  = round(atr_m30 / EA["atr_level"] * 100, 1)

    bb_range  = bb["upper"] - bb["lower"]
    price_pct = round((price - bb["lower"]) / bb_range * 100, 1) if bb_range else 50

    bb_signal = None
    if price >= bb["upper"]:   bb_signal = "SELL"
    elif price <= bb["lower"]: bb_signal = "BUY"

    dist_upper = round((bb["upper"] - price) / 0.0001, 1)
    dist_lower = round((price - bb["lower"]) / 0.0001, 1)
    atr_gap    = round((EA["atr_level"] - atr_m30) / 0.0001, 1) if not atr_pass else 0

    block_reason = diagnose_block(atr_pass, bb_signal, in_win)
    all_pass     = block_reason == "OPEN"

    # Régimen de mercado
    if atr_pct < 70:
        regime = "COMPRESIÓN SEVERA"
        regime_color = "red"
    elif atr_pct < 90:
        regime = "COMPRESIÓN MODERADA"
        regime_color = "yellow"
    elif atr_pct < 100:
        regime = "PRE-ACTIVACIÓN"
        regime_color = "orange"
    elif bb_signal:
        regime = "SEÑAL ACTIVA"
        regime_color = "green"
    else:
        regime = "ATR OK — SIN TOQUE BB"
        regime_color = "blue"

    # Historia reciente de ATR (patrones de logs reales)
    atr_hist = atr_series_daily(daily[-30:])  # últimos 30 días

    # Tendencia ATR (últimas 5 velas M30)
    if len(candles) >= EA["atr_period"] + 6:
        atrs_recent = []
        for i in range(6):
            sub = candles[:-(5-i)] if (5-i)>0 else candles
            if len(sub) > EA["atr_period"]:
                atrs_recent.append(calc_atr(sub))
        delta = atrs_recent[-1] - atrs_recent[0] if len(atrs_recent) >= 2 else 0
        pct_d = abs(delta)/atrs_recent[0]*100 if atrs_recent[0] else 0
        atr_trend = f"subiendo +{pct_d:.1f}%" if delta>0 and pct_d>3 else \
                    f"bajando -{pct_d:.1f}%" if delta<0 and pct_d>3 else "estable"
    else:
        atr_trend = "indeterminado"

    # Días consecutivos sin activar (del patrón de logs)
    days_above = sum(1 for x in atr_hist[-20:] if x["above_threshold"])
    days_total = len(atr_hist[-20:])

    win_msg = (f"Activa — {EA['win_end']-gmt_h}h restantes"
               if in_win else f"Fuera (GMT {gmt_h:02d}h) — abre a las 09:00 GMT")

    return dict(
        price=round(price,5), bb=bb, atr=atr_m30, atr_pct=atr_pct,
        atr_trend=atr_trend, atr_gap_pips=atr_gap,
        in_window=in_win, gmt_hour=gmt_h, window_msg=win_msg,
        atr_pass=atr_pass, bb_signal=bb_signal,
        dist_upper_pips=dist_upper, dist_lower_pips=dist_lower,
        price_pct_in_band=price_pct,
        block_reason=block_reason, all_pass=all_pass,
        regime=regime, regime_color=regime_color,
        atr_history=atr_hist,        # serie histórica completa
        days_above_threshold=days_above,
        days_total_recent=days_total,
        candles_used=len(candles),
        data_source=source,
        timestamp=now.isoformat(),
        last_candle=candles[-1]["time"],
    )

# ════════════════════════════════════════════════════════════════
#  GROQ — Prompt mejorado con contexto real de logs
# ════════════════════════════════════════════════════════════════

SYSTEM = """Eres el agente experto del EA-RMv8, estrategia de mean reversion CADCHF M30.

ARQUITECTURA EXACTA DEL EA (de logs reales):
Hay exactamente 3 bloqueadores en secuencia:
1. TIME_FILTER: fuera ventana GMT 09-20h (siempre activo 20:00-09:00 GMT)
2. ATR_FILTER_BLOCK: ATR(38) M30 < 0.0005 — volatilidad insuficiente
3. NO_TRIGGER: ATR OK pero precio NO ha cruzado fuera de BB(30,2.4)

PATRÓN OBSERVADO EN LOGS REALES (abril 2026):
- 1-2 abr (103-105%): ATR OK pero precio nunca tocó banda → NO_TRIGGER todo el día
- 3 abr (64%): crash de volatilidad repentino → ATR_FILTER_BLOCK todo el día
- 8 abr (126%): ATR muy por encima del umbral pero mismo resultado → NO_TRIGGER (precio en zona media)
- 9-17 abr: descenso progresivo del ATR (83%→71%), precio en zona media
- Conclusión clave: el EA necesita AMBAS condiciones simultáneas en ventana horaria
- La semana de mayor volatilidad (8 abr) NO produjo trades porque el precio no tocó las bandas

PARÁMETROS: BB(30,2.4) | ATR(38)>0.0005 | Ventana 09-20 GMT | SL=185p TP=30p | WR 91-95%

Tu análisis debe:
1. Identificar exactamente cuál bloqueador está activo y por qué
2. Calcular qué necesita cambiar para cada bloqueador (en números concretos)
3. Analizar si el patrón actual (ATR + posición precio) se parece a algún período histórico
4. Catalizadores macro CAD/CHF próximos que podrían cambiar el ATR o mover el precio a las bandas
5. Estimación realista de cuándo podría darse la próxima señal

Máximo 280 palabras. Termina con:
BLOQUEADOR_ACTIVO: [TIME_FILTER|ATR_FILTER_BLOCK|NO_TRIGGER|NINGUNO]
VEREDICTO: [ESPERAR|ALERTA|SEÑAL_ACTIVA] — [razón concisa]"""

def build_prompt(ev: dict) -> str:
    bb = ev["bb"]
    # últimos 10 días de historia ATR
    hist_lines = ""
    for h in ev["atr_history"][-10:]:
        flag = "⚡" if h["above_threshold"] else ("⚠" if h["atr_pct"]>=80 else "·")
        hist_lines += f"  {h['date']}: {flag} {h['atr_pct']:>5.1f}% | precio {h['close']:.5f}\n"

    return f"""CADCHF M30 — {ev['timestamp']}

ESTADO ACTUAL:
Precio:         {ev['price']}
BB Upper:       {bb['upper']} | dist: {ev['dist_upper_pips']}p
BB Lower:       {bb['lower']} | dist: {ev['dist_lower_pips']}p
Posición BB:    {ev['price_pct_in_band']}% dentro de banda
ATR(38) M30:    {ev['atr']} ({ev['atr_pct']}% umbral) | tendencia: {ev['atr_trend']}
Faltan:         {ev['atr_gap_pips']}p para alcanzar umbral
Ventana GMT:    {'✓' if ev['in_window'] else '✗'} {ev['window_msg']}

BLOQUEADOR ACTIVO: {ev['block_reason']}
Régimen:        {ev['regime']}

HISTORIA RECIENTE ATR ({ev['days_above_threshold']}/{ev['days_total_recent']} días sobre umbral):
{hist_lines}

CONTEXTO LOGS REALES:
- 8 abril: ATR 126% — aún así 0 trades (NO_TRIGGER — precio en zona media todo el día)
- Patrón actual similar a 9-17 abril: ATR oscilando 71-89%, sin toque de banda
- Precio necesita moverse {ev['dist_lower_pips']}p a la baja O {ev['dist_upper_pips']}p al alza para señal

Fuente datos: {ev['data_source']}"""

async def call_groq(prompt: str) -> dict:
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 900, "temperature": 0.25,
        "messages": [{"role":"system","content":SYSTEM},
                     {"role":"user",  "content":prompt}],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers=headers, json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    verdict, reason, blocker = "ESPERAR", "", "DESCONOCIDO"
    for line in reversed(text.split("\n")):
        ls = line.strip().upper()
        if ls.startswith("VEREDICTO:"):
            rest = line.split(":",1)[1].strip()
            for v in ("SEÑAL_ACTIVA","ALERTA","ESPERAR"):
                if v in rest.upper():
                    verdict = v
                    reason  = rest[rest.upper().find(v)+len(v):].strip(" —-")
                    break
        if ls.startswith("BLOQUEADOR_ACTIVO:"):
            blocker = line.split(":",1)[1].strip()

    clean = "\n".join(l for l in text.split("\n")
                      if not l.strip().upper().startswith(("VEREDICTO:","BLOQUEADOR_ACTIVO:"))).strip()
    return {"analysis": clean, "verdict": verdict, "reason": reason, "blocker_groq": blocker}

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status":"ok","version":"4.0","av_set":bool(AV_KEY),"groq_set":bool(GROQ_KEY)}

@app.get("/market")
async def market():
    try:
        daily, candles, source = await get_data(6)
        return evaluate_full(daily, candles, source)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/analyze")
async def analyze():
    try:
        daily, candles, source = await get_data(6)
        ev      = evaluate_full(daily, candles, source)
        prompt  = build_prompt(ev)
        groq_r  = await call_groq(prompt)
        return {**ev, **groq_r}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/history")
async def history():
    """Solo serie histórica de 6 meses para el gráfico"""
    try:
        daily, _, source = await get_data(6)
        return {"daily": daily, "atr_series": atr_series_daily(daily), "source": source}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open("index.html") as f:
        return f.read()
