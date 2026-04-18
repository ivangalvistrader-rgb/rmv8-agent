"""
RMv8 Agent v3.0 — Groq (LLaMA 3.3 70B) + Determinista
Fuentes de datos 100% gratuitas:
  - Alpha Vantage FX_DAILY (gratuito, diferente a FX_INTRADAY que es premium)
  - Frankfurter API (sin key, sin límite)
  - Fallback estático si todo falla
Groq LLaMA 3.3 70B para razonamiento autónomo (gratis)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx, os, math, random
from datetime import datetime, timezone, timedelta

app = FastAPI(title="RMv8-Agent", version="3.0")
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
#  FUENTE 1: Alpha Vantage FX_DAILY (GRATUITO con key)
# ════════════════════════════════════════════════════════════════

async def fetch_av_daily() -> list[dict]:
    url = (
        "https://www.alphavantage.co/query"
        "?function=FX_DAILY&from_symbol=CAD&to_symbol=CHF"
        f"&outputsize=compact&apikey={AV_KEY}"
    )
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
    key = "Time Series FX (Daily)"
    if key not in d:
        raise RuntimeError(d.get("Note") or d.get("Information") or "AV: sin datos")
    items = sorted(d[key].items())[-60:]
    return [
        {"date": dt,
         "open":  float(v["1. open"]),
         "high":  float(v["2. high"]),
         "low":   float(v["3. low"]),
         "close": float(v["4. close"])}
        for dt, v in items
    ]

# ════════════════════════════════════════════════════════════════
#  FUENTE 2: Frankfurter (sin key, siempre disponible)
# ════════════════════════════════════════════════════════════════

async def fetch_frankfurter() -> list[dict]:
    end   = datetime.now(timezone.utc).date()
    start = end - timedelta(days=90)
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
        rng  = max(abs(close - prev) * 2, 0.0004)
        daily.append({
            "date":  dt,
            "open":  round(prev, 5),
            "high":  round(max(prev, close) + rng * 0.3, 5),
            "low":   round(min(prev, close) - rng * 0.3, 5),
            "close": round(close, 5),
        })
    return daily[-50:]

# ════════════════════════════════════════════════════════════════
#  CONVERTIR DIARIO → PSEUDO-VELAS M30
# ════════════════════════════════════════════════════════════════

def daily_to_m30(daily: list[dict]) -> list[dict]:
    """
    Distribuye el rango OHLC diario en 22 velas M30 (sesión ~11h).
    Produce ATR M30 estadísticamente consistente con ATR_diario / sqrt(22).
    """
    candles = []
    rng_seed = sum(int(d["close"] * 100000) for d in daily[-5:]) % 9999
    rng = random.Random(rng_seed)   # seed determinista por datos → reproducible

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
                "time":  f"{day['date']} {9 + i//2:02d}:{30*(i%2):02d}:00",
                "open":  round(op, 5), "high": round(hi, 5),
                "low":   round(lo, 5), "close": round(cl, 5),
            })
            price = cl
    return candles

# ════════════════════════════════════════════════════════════════
#  ORQUESTADOR DE DATOS
# ════════════════════════════════════════════════════════════════

async def get_candles() -> tuple[list[dict], str]:
    # Intento 1: AV FX_DAILY (gratuito)
    if AV_KEY:
        try:
            daily   = await fetch_av_daily()
            candles = daily_to_m30(daily)
            return candles, f"Alpha Vantage FX_DAILY → {len(daily)} días → {len(candles)} velas M30"
        except Exception:
            pass

    # Intento 2: Frankfurter (siempre gratuito)
    try:
        daily   = await fetch_frankfurter()
        candles = daily_to_m30(daily)
        return candles, f"Frankfurter API → {len(daily)} días → {len(candles)} velas M30"
    except Exception:
        pass

    # Fallback: datos sintéticos basados en comportamiento reciente
    now = datetime.now(timezone.utc)
    rng = random.Random(42)
    price, candles = 0.5707, []
    for i in range(660):
        dt    = now - timedelta(minutes=30 * (660 - i))
        move  = rng.gauss(0, 0.00014)
        price = max(0.5650, min(0.5770, price + move))
        hi    = price + abs(rng.gauss(0, 0.00007))
        lo    = price - abs(rng.gauss(0, 0.00007))
        candles.append({
            "time":  dt.strftime("%Y-%m-%d %H:%M:00"),
            "open":  round(price - move, 5), "high": round(hi, 5),
            "low":   round(lo, 5),           "close": round(price, 5),
        })
    return candles, "Fallback estático (compresión CADCHF abril 2026)"

# ════════════════════════════════════════════════════════════════
#  CÁLCULOS DETERMINISTAS (exactos como el EA)
# ════════════════════════════════════════════════════════════════

def calc_bb(closes: list[float]) -> dict:
    s   = closes[-EA["bb_period"]:]
    m   = sum(s) / len(s)
    std = math.sqrt(sum((x - m) ** 2 for x in s) / len(s))
    return dict(upper=round(m + EA["bb_dev"]*std, 5),
                mid=round(m, 5),
                lower=round(m - EA["bb_dev"]*std, 5))

def calc_atr(candles: list[dict]) -> float:
    trs = [max(c["high"] - c["low"],
               abs(c["high"] - candles[i-1]["close"]),
               abs(c["low"]  - candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    last = trs[-EA["atr_period"]:]
    return round(sum(last) / len(last), 6)

def atr_trend(candles: list[dict]) -> str:
    if len(candles) < EA["atr_period"] + 6:
        return "indeterminado"
    atrs = [calc_atr(candles[:-(5-i)] if (5-i) > 0 else candles) for i in range(6)]
    delta = atrs[-1] - atrs[0]
    pct   = abs(delta) / atrs[0] * 100 if atrs[0] else 0
    if delta > 0 and pct > 4:  return f"subiendo +{pct:.1f}%"
    if delta < 0 and pct > 4:  return f"bajando -{pct:.1f}%"
    return "estable"

def evaluate(candles: list[dict], source: str) -> dict:
    closes   = [c["close"] for c in candles]
    price    = closes[-1]
    bb       = calc_bb(closes)
    atr      = calc_atr(candles)
    trend    = atr_trend(candles)
    now      = datetime.now(timezone.utc)
    gmt_h    = now.hour
    in_win   = EA["win_start"] <= gmt_h < EA["win_end"]
    atr_pass = atr >= EA["atr_level"]
    atr_pct  = round(atr / EA["atr_level"] * 100, 1)
    bb_range = bb["upper"] - bb["lower"]
    price_pct= round((price - bb["lower"]) / bb_range * 100, 1) if bb_range else 50

    bb_signal = None
    if price >= bb["upper"]:   bb_signal = "SELL"
    elif price <= bb["lower"]: bb_signal = "BUY"

    dist_upper = round((bb["upper"] - price) / 0.0001, 1)
    dist_lower = round((price - bb["lower"]) / 0.0001, 1)
    atr_gap    = round((EA["atr_level"] - atr) / 0.0001, 1) if not atr_pass else 0
    all_pass   = in_win and atr_pass and bb_signal is not None

    if atr_pct < 60:
        regime = "COMPRESIÓN SEVERA"
        regime_detail = (f"ATR al {atr_pct}% del umbral. Rango muy estrecho sostenido. "
                         f"Tendencia ATR: {trend}. Históricamente precede ruptura, pero timing impredecible.")
    elif atr_pct < 80:
        regime = "COMPRESIÓN MODERADA"
        regime_detail = (f"ATR al {atr_pct}% — en recuperación ({trend}). "
                         f"Posible activación en 1-3 días si el momentum continúa.")
    elif atr_pct < 100:
        regime = "PRE-ACTIVACIÓN"
        regime_detail = (f"ATR al {atr_pct}% — faltan solo {atr_gap:.0f}p de volatilidad. "
                         f"Tendencia {trend}. Alta probabilidad de activación próxima.")
    elif bb_signal:
        regime = "CONDICIONES COMPLETAS"
        regime_detail = f"ATR ✓ | BB {bb_signal} ✓ | Ventana ✓. Todos los filtros activos."
    else:
        regime = "VOLATILIDAD OK — SIN SEÑAL BB"
        regime_detail = (f"ATR suficiente ({atr_pct}%). Precio a {dist_upper:.0f}p de upper "
                         f"y {dist_lower:.0f}p de lower. Esperando toque de banda.")

    win_msg = (f"Activa — {EA['win_end'] - gmt_h}h restantes."
               if in_win else f"Fuera de ventana (GMT {gmt_h:02d}:00). Ventana: 09-20h GMT.")

    verdict = "SEÑAL_ACTIVA" if all_pass else ("ALERTA" if atr_pct >= 80 and in_win else "ESPERAR")

    return dict(
        price=round(price,5), bb=bb, atr=atr, atr_pct=atr_pct,
        atr_trend=trend, atr_gap_pips=atr_gap, in_window=in_win,
        gmt_hour=gmt_h, window_msg=win_msg, atr_pass=atr_pass,
        bb_signal=bb_signal, dist_upper_pips=dist_upper, dist_lower_pips=dist_lower,
        price_pct_in_band=price_pct, all_pass=all_pass, regime=regime,
        regime_detail=regime_detail, verdict_deterministic=verdict,
        timestamp=now.isoformat(), last_candle=candles[-1]["time"],
        candles_used=len(candles), data_source=source,
    )

# ════════════════════════════════════════════════════════════════
#  GROQ — LLaMA 3.3 70B
# ════════════════════════════════════════════════════════════════

SYSTEM = """Eres el agente autónomo del EA-RMv8, estrategia de mean reversion CADCHF M30.

PARÁMETROS DEL EA:
- BB(30, 2.4): entra cuando precio CRUZA FUERA de la banda
- ATR(38) M30 > 0.0005: volatilidad mínima obligatoria  
- Ventana 09:00-20:00 GMT | SL=185p TP=30p | WR histórico 91-95%
- IS (290t, 6 años): PF=2.00, WR=91.7% | OOS (42t, 11M): PF=6.19, WR=95.2%
- El EA lleva todo abril 2026 sin un trade: ATR sostenido bajo umbral

TU ANÁLISIS debe cubrir:
1. Por qué el mercado está así y qué lo causa fundamentalmente
2. Catalizadores macro concretos de CAD (BoC, empleo, PIB, petróleo) y CHF (SNB, refugio, inflación)
3. Cuándo y qué condiciones exactas necesitan cambiar
4. Nivel de confianza si hubiera señal ahora

Directo, técnico, sin repetir datos del prompt. Máximo 260 palabras.
Termina SIEMPRE con:
VEREDICTO: [ESPERAR|ALERTA|SEÑAL_ACTIVA] — [razón concisa]"""

def build_prompt(ev: dict) -> str:
    bb = ev["bb"]
    return f"""CADCHF M30 — {ev['timestamp']}
Fuente: {ev['data_source']}

INDICADORES:
Precio:        {ev['price']} ({ev['price_pct_in_band']}% en banda)
BB Upper:      {bb['upper']} | Dist: {ev['dist_upper_pips']}p
BB Lower:      {bb['lower']} | Dist: {ev['dist_lower_pips']}p
ATR(38) M30:   {ev['atr']} ({ev['atr_pct']}% umbral) | Tendencia: {ev['atr_trend']}
Faltan:        {ev['atr_gap_pips']}p de volatilidad para umbral

FILTROS:
Ventana GMT:   {'✓' if ev['in_window'] else '✗'} {ev['window_msg']}
ATR:           {'✓ PASA' if ev['atr_pass'] else '✗ FALLA'}
Señal BB:      {ev['bb_signal'] or 'Sin señal (precio en zona media)'}
Régimen:       {ev['regime']}

DIAGNÓSTICO:
{ev['regime_detail']}

Analiza con profundidad y da tu veredicto."""

async def call_groq(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 800, "temperature": 0.3,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user",   "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers=headers, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

def parse_verdict(text: str) -> tuple[str, str]:
    for line in reversed(text.split("\n")):
        if line.strip().upper().startswith("VEREDICTO:"):
            rest = line.split(":", 1)[1].strip()
            for v in ("SEÑAL_ACTIVA", "ALERTA", "ESPERAR"):
                if v in rest.upper():
                    idx    = rest.upper().find(v)
                    reason = rest[idx + len(v):].strip(" —-")
                    return v, reason
    return "ESPERAR", ""

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0",
            "av_set": bool(AV_KEY), "groq_set": bool(GROQ_KEY)}

@app.get("/market")
async def market():
    try:
        candles, source = await get_candles()
        return evaluate(candles, source)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/analyze")
async def analyze():
    try:
        candles, source = await get_candles()
        ev              = evaluate(candles, source)
        prompt          = build_prompt(ev)
        analysis        = await call_groq(prompt)
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
