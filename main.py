"""
RMv8 Agent — Groq (LLaMA 3.3 70B) + Determinista
Alpha Vantage para datos M30 reales de CADCHF
Groq para razonamiento autónomo (gratis)
Deploy: Railway / Render
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx, os, math, json
from datetime import datetime, timezone

app = FastAPI(title="RMv8-Agent", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Keys desde variables de entorno ──────────────────────────────
AV_KEY   = os.getenv("ALPHA_VANTAGE_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")

# ── Parámetros fijos EA-RMv8 ─────────────────────────────────────
EA = dict(
    bb_period=30, bb_dev=2.4,
    atr_period=38, atr_level=0.0005,
    sl_pips=185,   tp_pips=30,
    shrink_pips=16,
    win_start=9,   win_end=20,   # GMT
)

# ════════════════════════════════════════════════════════════════
#  DATOS — Alpha Vantage M30
# ════════════════════════════════════════════════════════════════

async def get_candles(n: int = 100) -> list[dict]:
    url = (
        "https://www.alphavantage.co/query"
        "?function=FX_INTRADAY&from_symbol=CAD&to_symbol=CHF"
        f"&interval=30min&outputsize=compact&apikey={AV_KEY}"
    )
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()

    key = "Time Series FX (30min)"
    if key not in d:
        raise RuntimeError(d.get("Note") or d.get("Information") or str(d))

    candles = [
        {"time": k,
         "open":  float(v["1. open"]),
         "high":  float(v["2. high"]),
         "low":   float(v["3. low"]),
         "close": float(v["4. close"])}
        for k, v in list(d[key].items())[:n]
    ]
    candles.reverse()   # más antiguo primero
    return candles

# ════════════════════════════════════════════════════════════════
#  CÁLCULOS DETERMINISTAS — exactos como el EA
# ════════════════════════════════════════════════════════════════

def calc_bb(closes: list[float]) -> dict:
    s = closes[-EA["bb_period"]:]
    m = sum(s) / len(s)
    std = math.sqrt(sum((x-m)**2 for x in s) / len(s))
    return dict(upper=round(m+EA["bb_dev"]*std,5),
                mid=round(m,5),
                lower=round(m-EA["bb_dev"]*std,5),
                std=round(std,5))

def calc_atr(candles: list[dict]) -> float:
    trs = [max(c["high"]-c["low"],
               abs(c["high"]-candles[i-1]["close"]),
               abs(c["low"] -candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    last = trs[-EA["atr_period"]:]
    return round(sum(last)/len(last), 6)

def atr_trend(candles: list[dict], window: int = 5) -> str:
    """Tendencia del ATR en las últimas 'window' velas."""
    if len(candles) < EA["atr_period"] + window + 1:
        return "indeterminado"
    atrs = []
    for i in range(window):
        subset = candles[:-(window-i)] if (window-i) > 0 else candles
        if len(subset) > EA["atr_period"]:
            atrs.append(calc_atr(subset))
    if len(atrs) < 2:
        return "indeterminado"
    delta = atrs[-1] - atrs[0]
    pct   = abs(delta) / atrs[0] * 100
    if delta > 0 and pct > 5:   return f"subiendo +{pct:.1f}%"
    if delta < 0 and pct > 5:   return f"bajando -{pct:.1f}%"
    return "estable"

def evaluate(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    bb     = calc_bb(closes)
    atr    = calc_atr(candles)
    trend  = atr_trend(candles)

    now      = datetime.now(timezone.utc)
    gmt_h    = now.hour
    in_win   = EA["win_start"] <= gmt_h < EA["win_end"]
    atr_pass = atr >= EA["atr_level"]
    atr_pct  = round(atr / EA["atr_level"] * 100, 1)
    bb_range = bb["upper"] - bb["lower"]
    price_pct= round((price - bb["lower"]) / bb_range * 100, 1) if bb_range else 50

    # Señal BB: precio CRUZA fuera de la banda (lógica exacta del EA)
    bb_signal = None
    if price >= bb["upper"]:   bb_signal = "SELL"
    elif price <= bb["lower"]: bb_signal = "BUY"

    # Distancia a bandas en pips (CADCHF: 1 pip = 0.0001)
    dist_upper = round((bb["upper"] - price) / 0.0001, 1)
    dist_lower = round((price - bb["lower"]) / 0.0001, 1)
    atr_gap    = round((EA["atr_level"] - atr) / 0.0001, 1) if not atr_pass else 0

    all_pass = in_win and atr_pass and bb_signal is not None

    # ── Diagnóstico determinista ──────────────────────────────────
    if atr_pct < 60:
        regime = "COMPRESIÓN SEVERA"
        regime_detail = (
            f"ATR al {atr_pct}% del umbral. El par lleva varios días "
            f"en rango muy estrecho. Históricamente esto precede a una "
            f"ruptura, pero el timing es impredecible."
        )
    elif atr_pct < 80:
        regime = "COMPRESIÓN MODERADA"
        regime_detail = (
            f"ATR al {atr_pct}% del umbral. Volatilidad en recuperación "
            f"({trend}). Monitorear de cerca — puede activar en 1-3 días "
            f"si el movimiento continúa."
        )
    elif atr_pct < 100:
        regime = "PRE-ACTIVACIÓN"
        regime_detail = (
            f"ATR al {atr_pct}% del umbral — faltan solo {atr_gap:.0f} pips "
            f"de volatilidad. Tendencia ATR: {trend}. Alta probabilidad de "
            f"activación en las próximas horas si el momentum persiste."
        )
    else:
        if bb_signal:
            regime = "CONDICIONES COMPLETAS"
            regime_detail = (
                f"ATR supera umbral ({atr_pct}%) Y precio fuera de BB. "
                f"Señal {bb_signal} activa. Todos los filtros pasados."
            )
        else:
            regime = "VOLATILIDAD OK — SIN SEÑAL BB"
            regime_detail = (
                f"ATR suficiente ({atr_pct}%). Precio a {dist_upper:.0f}p "
                f"de upper y {dist_lower:.0f}p de lower. Esperando que el "
                f"precio alcance una banda para señal."
            )

    if not in_win:
        window_msg = f"Fuera de ventana GMT (hora actual: {gmt_h:02d}:00). Ventana: 09-20h GMT."
    else:
        remaining = EA["win_end"] - gmt_h
        window_msg = f"Ventana activa — {remaining}h restantes hoy."

    # Veredicto determinista
    if all_pass:
        verdict_det = "SEÑAL_ACTIVA"
    elif atr_pct >= 80 and in_win:
        verdict_det = "ALERTA"
    else:
        verdict_det = "ESPERAR"

    return dict(
        price=round(price,5), bb=bb, atr=atr,
        atr_pct=atr_pct, atr_trend=trend, atr_gap_pips=atr_gap,
        in_window=in_win, gmt_hour=gmt_h, window_msg=window_msg,
        atr_pass=atr_pass, bb_signal=bb_signal,
        dist_upper_pips=dist_upper, dist_lower_pips=dist_lower,
        price_pct_in_band=price_pct,
        all_pass=all_pass, regime=regime, regime_detail=regime_detail,
        verdict_deterministic=verdict_det,
        timestamp=now.isoformat(),
        last_candle=candles[-1]["time"],
        candles_used=len(candles),
    )

# ════════════════════════════════════════════════════════════════
#  GROQ — LLaMA 3.3 70B (razonamiento)
# ════════════════════════════════════════════════════════════════

SYSTEM = """Eres el agente autónomo del EA-RMv8, estrategia de mean reversion sobre CADCHF M30.

PARÁMETROS DEL EA:
- Bollinger Bands(30, 2.4) — entra cuando precio CRUZA FUERA de la banda
- ATR(38) M30 > 0.0005 — volatilidad mínima obligatoria
- Ventana horaria: 09:00-20:00 GMT
- SL=185 pips, TP=30 pips (ratio invertido, requiere WR>86% para ser rentable)
- ShrinkSL a +16 pips a favor (reduce pérdida potencial)
- Win Rate histórico IS: 91.7% (290 trades, 6 años)
- Win Rate histórico OOS: 95.2% (42 trades, 11 meses)

CONTEXTO OPERATIVO REAL:
- El EA lleva todo abril 2026 sin un solo trade
- ATR M30 sostenido entre 0.00032-0.00047 durante semanas
- Cuentas activas: FXIFY $15K (0.10 lots) y GetLeveraged $100K (0.40 lots)
- Es normal tener meses sin trades — el IS muestra meses con 0-1 trades en compresión

TU MISIÓN:
Con los datos exactos que recibes (calculados matemáticamente), razona sobre:
1. Por qué el mercado está así y qué lo está causando
2. Qué catalizadores macro de CAD o CHF pueden cambiar la volatilidad
3. Cuándo y bajo qué condiciones esperarías activación
4. Qué nivel de confianza tendrías si hubiera señal ahora mismo

Sé directo, técnico, sin repetir datos ya dados. Máximo 250 palabras.
TERMINA SIEMPRE con una línea:
VEREDICTO: [ESPERAR|ALERTA|SEÑAL_ACTIVA] — [razón concisa]"""

def build_prompt(ev: dict) -> str:
    bb = ev["bb"]
    return f"""DATOS CALCULADOS — {ev['timestamp']}
Última vela M30: {ev['last_candle']}

INDICADORES EXACTOS (calculados desde {ev['candles_used']} velas M30 reales):
Precio:         {ev['price']}
BB Upper(30,2.4): {bb['upper']}
BB Mid:          {bb['mid']}
BB Lower(30,2.4): {bb['lower']}
Posición precio: {ev['price_pct_in_band']}% dentro de bandas
Dist. a upper:   {ev['dist_upper_pips']} pips
Dist. a lower:   {ev['dist_lower_pips']} pips

ATR(38) M30:    {ev['atr']} ({ev['atr_pct']}% del umbral 0.0005)
Tendencia ATR:  {ev['atr_trend']}
Faltan para umbral: {ev['atr_gap_pips']} pips de volatilidad

FILTROS:
Ventana GMT:    {'✓' if ev['in_window'] else '✗'} {ev['window_msg']}
Filtro ATR:     {'✓ PASA' if ev['atr_pass'] else '✗ FALLA'}
Señal BB:       {ev['bb_signal'] or 'Sin señal (precio en zona media)'}
Condición:      {'⚡ SEÑAL ACTIVA' if ev['all_pass'] else ev['regime']}

DIAGNÓSTICO CALCULADO:
{ev['regime_detail']}

Analiza esta situación con razonamiento profundo y da tu veredicto."""

async def call_groq(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type":  "application/json",
    }
    body = {
        "model":       "llama-3.3-70b-versatile",
        "max_tokens":  800,
        "temperature": 0.3,   # más bajo = más consistente para análisis técnico
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user",   "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers=headers, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

def parse_verdict(text: str) -> tuple[str, str]:
    for line in reversed(text.split("\n")):
        line = line.strip()
        if line.upper().startswith("VEREDICTO:"):
            rest = line.split(":", 1)[1].strip()
            for v in ("SEÑAL_ACTIVA", "ALERTA", "ESPERAR"):
                if v in rest.upper():
                    reason = rest.upper().replace(v, "").replace("—", "").replace("-", "").strip()
                    # volver al case original
                    reason_orig = rest[rest.upper().find(v)+len(v):].strip(" —-")
                    return v, reason_orig
    return "ESPERAR", ""

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status":"ok","av_set":bool(AV_KEY),"groq_set":bool(GROQ_KEY)}

@app.get("/market")
async def market():
    try:
        candles = await get_candles()
        return evaluate(candles)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/analyze")
async def analyze():
    try:
        candles  = await get_candles()
        ev       = evaluate(candles)
        prompt   = build_prompt(ev)
        analysis = await call_groq(prompt)
        verdict, reason = parse_verdict(analysis)
        clean = "\n".join(
            l for l in analysis.split("\n")
            if not l.strip().upper().startswith("VEREDICTO:")
        ).strip()
        return {**ev, "analysis": clean, "verdict": verdict, "reason": reason}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open("index.html") as f:
        return f.read()
