"""
Portfolio Monitor v2.0
━━━━━━━━━━━━━━━━━━━━━
Arquitectura:
  - backtest_reference.json  → fijo en GitHub (IS/OOS real de los HTM)
  - data/history/{EA}/{date}.json → logs acumulados diarios en GitHub
  - Twelve Data API          → indicadores M30/H1/D1 para cada EA (GMT+3)
  - Groq LLaMA 3.3 70B       → análisis: estado actual vs backtest + predicción

Flujo diario:
  1. Subes CSV del logger → backend lo parsea
  2. Se guarda automáticamente en GitHub como {EA}/{YYYY-MM-DD}.json
  3. Groq lee: backtest_reference + historial acumulado + TD indicators
  4. Responde: ¿NORMAL/REVISAR/ACCIÓN? + predicción próximo trade
"""

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import httpx, os, csv, io, json, base64, math, re
from datetime import datetime, timezone, timedelta
from collections import defaultdict

app = FastAPI(title="Portfolio Monitor", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Environment ──────────────────────────────────────────────────
GROQ_KEY    = os.getenv("GROQ_KEY", "")
TD_KEY      = os.getenv("TWELVE_DATA_KEY", "")
GH_TOKEN    = os.getenv("GITHUB_TOKEN", "")   # Personal Access Token
GH_REPO     = os.getenv("GITHUB_REPO", "")    # format: "username/repo-name"
GH_BRANCH   = os.getenv("GITHUB_BRANCH", "main")

TD_BASE = "https://api.twelvedata.com"
GH_BASE = "https://api.github.com"

# ── Load static reference files ──────────────────────────────────
with open("backtest_reference.json") as f:
    BACKTEST = json.load(f)

# Load individual trade statistics (compact summary extracted from HTM files)
# Used by Groq to compare live patterns vs backtest trade characteristics
try:
    with open("backtest_summary.json") as f:
        BACKTEST_TRADES = json.load(f)
except FileNotFoundError:
    BACKTEST_TRADES = {}  # Optional — setup uploads it

# ── In-memory history cache (survives within session) ───────────
HISTORY_CACHE: dict[str, list] = {}   # ea_key → [day_data, ...]

# ════════════════════════════════════════════════════════════════
#  GITHUB — persistent storage
# ════════════════════════════════════════════════════════════════

def gh_headers():
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def gh_read(path: str) -> dict | None:
    """Read a file from GitHub repo. Returns parsed JSON or None."""
    if not GH_TOKEN or not GH_REPO:
        return None
    url = f"{GH_BASE}/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=gh_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        content = base64.b64decode(d["content"]).decode("utf-8")
        return {"data": json.loads(content), "sha": d["sha"]}

async def gh_write(path: str, data, message: str, sha: str | None = None):
    """Write/update a file in GitHub repo. data can be dict or str."""
    if not GH_TOKEN or not GH_REPO:
        return False
    if isinstance(data, dict):
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode()
    elif isinstance(data, str):
        raw = data.encode()
    else:
        raw = data
    content = base64.b64encode(raw).decode()
    payload = {"message": message, "content": content, "branch": GH_BRANCH}
    if sha:
        payload["sha"] = sha
    url = f"{GH_BASE}/repos/{GH_REPO}/contents/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.put(url, headers=gh_headers(), json=payload)
        return r.status_code in (200, 201)

async def save_log_to_github(ea_key: str, date_str: str, data: dict):
    """
    Save a parsed day log to GitHub: data/history/{ea_key}/{date}.json
    Strips large arrays before saving to keep files small.
    """
    # Clean up large fields before storing
    save_data = {k: v for k, v in data.items()
                 if k not in ("recent_trades", "recent_closes")}
    path = f"data/history/{ea_key}/{date_str}.json"
    existing = await gh_read(path)
    sha = existing["sha"] if existing else None
    ok = await gh_write(
        path, save_data,
        f"log: {ea_key} {date_str} — {data.get('trades_open',0)} trades | {data.get('dominant_block','?')}",
        sha
    )
    return ok

async def load_history_from_github(ea_key: str, days: int = 60) -> list:
    """Load last N days of logs for an EA from GitHub."""
    if not GH_TOKEN or not GH_REPO:
        return HISTORY_CACHE.get(ea_key, [])

    # List files in the EA's history folder
    url = f"{GH_BASE}/repos/{GH_REPO}/contents/data/history/{ea_key}?ref={GH_BRANCH}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=gh_headers())
        if r.status_code == 404:
            return []
        files = r.json()

    # Sort by name (date) descending, take last N
    files = sorted([f for f in files if f["name"].endswith(".json")],
                   key=lambda x: x["name"], reverse=True)[:days]

    history = []
    for f in files:
        result = await gh_read(f"data/history/{ea_key}/{f['name']}")
        if result:
            history.append(result["data"])

    history.reverse()  # chronological order
    HISTORY_CACHE[ea_key] = history
    return history

# ════════════════════════════════════════════════════════════════
#  TWELVE DATA — indicators per EA (GMT+3 aware)
# ════════════════════════════════════════════════════════════════

async def td_get(endpoint: str, params: dict) -> dict:
    params["apikey"] = TD_KEY
    # Timezone: GMT+3 for all requests (broker time)
    if "timezone" not in params:
        params["timezone"] = "Etc/GMT-3"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{TD_BASE}/{endpoint}", params=params)
        r.raise_for_status()
        d = r.json()
    if d.get("status") == "error":
        raise RuntimeError(f"TwelveData: {d.get('message', d)}")
    return d

def td_latest(values: list) -> dict:
    """Get latest non-null value from TD response."""
    for v in values:
        if v and all(val not in (None, "NaN", "") for val in v.values()):
            return v
    return values[0] if values else {}

async def fetch_ea_indicators(ea_key: str) -> dict:
    """
    Fetch exactly the indicators each EA uses.
    All in GMT+3 (broker time: FXPIG summer).
    Returns dict with all relevant values for Groq context.
    """
    bt = BACKTEST.get(ea_key, {})
    tdi = bt.get("td_indicators", {})
    if not tdi or not TD_KEY:
        return {"error": "No TD config or key"}

    symbol   = tdi["symbol"]
    interval = tdi["interval"]
    result   = {"symbol": symbol, "interval": interval, "timestamp": datetime.now(timezone.utc).isoformat()}
    errors   = []

    fetch = tdi.get("fetch", [])

    try:
        # ── OHLC current + recent candles ──────────────────────
        ts = await td_get("time_series", {
            "symbol": symbol, "interval": interval,
            "outputsize": 5, "order": "desc",
        })
        vals = ts.get("values", [])
        if vals:
            result["price"]       = float(vals[0]["close"])
            result["high_today"]  = float(vals[0]["high"])
            result["low_today"]   = float(vals[0]["low"])
            result["open_today"]  = float(vals[0]["open"])
            result["candle_time"] = vals[0]["datetime"]

        # ── RSI ──────────────────────────────────────────────────
        if "rsi" in fetch:
            d = await td_get("rsi", {"symbol": symbol, "interval": interval,
                                     "time_period": tdi.get("rsi_period", 14), "outputsize": 3})
            v = td_latest(d.get("values", []))
            result["rsi"] = float(v.get("rsi", 0)) if v else None

        # ── EMA ──────────────────────────────────────────────────
        if "ema" in fetch:
            # Main EMA
            d = await td_get("ema", {"symbol": symbol, "interval": interval,
                                     "time_period": tdi.get("ema_period", 20), "outputsize": 3})
            v = td_latest(d.get("values", []))
            result["ema"] = float(v.get("ema", 0)) if v else None
            # If EA uses EMA on different TF (e.g. LDBOX EMA H1, XAUMS EMA H4)
            ema_tf = tdi.get("ema_tf")
            if ema_tf and ema_tf != interval:
                d2 = await td_get("ema", {"symbol": symbol, "interval": ema_tf,
                                          "time_period": tdi.get("ema_period", 20), "outputsize": 3})
                v2 = td_latest(d2.get("values", []))
                result[f"ema_{ema_tf}"] = float(v2.get("ema", 0)) if v2 else None

        # ── SMA ──────────────────────────────────────────────────
        if "sma" in fetch:
            d = await td_get("sma", {"symbol": symbol, "interval": interval,
                                     "time_period": tdi.get("sma_period", 89), "outputsize": 3})
            v = td_latest(d.get("values", []))
            result["sma"] = float(v.get("sma", 0)) if v else None

        # ── BB (Bollinger Bands) ──────────────────────────────────
        if "bbands" in fetch:
            d = await td_get("bbands", {
                "symbol": symbol, "interval": interval,
                "time_period": tdi.get("bb_period", 20),
                "sd": tdi.get("bb_sd", 2.0),
                "outputsize": 3,
            })
            v = td_latest(d.get("values", []))
            if v:
                result["bb_upper"] = float(v.get("upper_band", 0))
                result["bb_mid"]   = float(v.get("middle_band", 0))
                result["bb_lower"] = float(v.get("lower_band", 0))
                if result.get("price") and result["bb_upper"] and result["bb_lower"]:
                    rng = result["bb_upper"] - result["bb_lower"]
                    p   = result["price"]
                    result["price_pct_in_bb"] = round((p - result["bb_lower"]) / rng * 100, 1) if rng else 50
                    result["dist_to_upper_pips"] = round((result["bb_upper"] - p) / 0.0001, 1)
                    result["dist_to_lower_pips"] = round((p - result["bb_lower"]) / 0.0001, 1)
                    result["bb_signal"] = "SELL" if p >= result["bb_upper"] else ("BUY" if p <= result["bb_lower"] else None)

        # ── ATR (main interval) ───────────────────────────────────
        if "atr" in fetch:
            d = await td_get("atr", {"symbol": symbol, "interval": interval,
                                     "time_period": tdi.get("atr_period", 14), "outputsize": 5})
            vals_atr = d.get("values", [])
            if vals_atr:
                result["atr"] = float(td_latest(vals_atr).get("atr", 0))
                result["atr_values"] = [float(v.get("atr", 0)) for v in vals_atr[:5]]
                # ATR threshold check (RMv8)
                thresh = tdi.get("atr_threshold")
                if thresh:
                    result["atr_pct_threshold"] = round(result["atr"] / thresh * 100, 1)
                    result["atr_above_threshold"] = result["atr"] >= thresh

        # ── ATR on H1 (XAUMS uses ATR H1 for SL sizing) ──────────
        if "atr_h1" in fetch:
            d = await td_get("atr", {"symbol": symbol, "interval": "1h",
                                     "time_period": tdi.get("atr_h1_period", 14), "outputsize": 3})
            v = td_latest(d.get("values", []))
            result["atr_h1"] = float(v.get("atr", 0)) if v else None

        # ── Keltner Channel ───────────────────────────────────────
        if "keltner" in fetch:
            d = await td_get("keltner", {"symbol": symbol, "interval": interval,
                                         "time_period": tdi.get("kc_period", 10),
                                         "multiplier": tdi.get("kc_mult", 2.7),
                                         "outputsize": 3})
            v = td_latest(d.get("values", []))
            if v:
                result["kc_upper"] = float(v.get("upper_band", 0))
                result["kc_mid"]   = float(v.get("middle_band", 0))
                result["kc_lower"] = float(v.get("lower_band", 0))

        # ── Williams %R ───────────────────────────────────────────
        if "willr" in fetch:
            d = await td_get("willr", {"symbol": symbol, "interval": interval,
                                       "time_period": tdi.get("willr_period", 38), "outputsize": 3})
            vals_w = d.get("values", [])
            if vals_w and len(vals_w) >= 2:
                result["wpr_1"] = float(td_latest(vals_w).get("willr", 0))
                result["wpr_2"] = float(vals_w[1].get("willr", 0))
                result["wpr_buy_signal"]  = result["wpr_2"] > -81 and result["wpr_1"] < -81
                result["wpr_sell_signal"] = result["wpr_2"] < -35 and result["wpr_1"] > -35

        # ── STOCH ──────────────────────────────────────────────────
        if "stoch" in fetch:
            d = await td_get("stoch", {"symbol": symbol, "interval": interval,
                                       "fast_k_period": tdi.get("stoch_k", 8),
                                       "slow_d_period": tdi.get("stoch_d", 7),
                                       "slow_k_period": tdi.get("stoch_s", 31),
                                       "outputsize": 3})
            v = td_latest(d.get("values", []))
            if v:
                result["stoch_k"] = float(v.get("slow_k", 0))
                result["stoch_d"] = float(v.get("slow_d", 0))

        # ── MACD ──────────────────────────────────────────────────
        if "macd" in fetch:
            d = await td_get("macd", {"symbol": symbol, "interval": interval,
                                      "fast_period": tdi.get("macd_fast", 12),
                                      "slow_period": tdi.get("macd_slow", 26),
                                      "signal_period": tdi.get("macd_signal", 9),
                                      "outputsize": 3})
            vals_m = d.get("values", [])
            if vals_m and len(vals_m) >= 2:
                v1 = vals_m[0]; v2 = vals_m[1]
                result["macd_hist_1"] = float(v1.get("macd_hist", 0))
                result["macd_hist_2"] = float(v2.get("macd_hist", 0))
                # MACD sign change = exit signal for MilkyWay
                h1, h2 = result["macd_hist_1"], result["macd_hist_2"]
                result["macd_bullish_cross"] = h2 < 0 and h1 >= 0
                result["macd_bearish_cross"] = h2 > 0 and h1 <= 0

        # ── D1 High/Low (XAUMS: breakout of 2 days ago H/L) ──────
        if "ema" in fetch and ea_key == "XAUMS":
            d_d1 = await td_get("time_series", {
                "symbol": symbol, "interval": "1day", "outputsize": 5, "order": "desc",
            })
            d1_vals = d_d1.get("values", [])
            if len(d1_vals) >= 3:
                day_ago2 = d1_vals[2]  # DayOffset=2 = anteayer
                result["d1_prev2_high"] = float(day_ago2["high"])
                result["d1_prev2_low"]  = float(day_ago2["low"])
                result["d1_prev2_date"] = day_ago2["datetime"]
                if result.get("price"):
                    p = result["price"]
                    result["above_d1_high"] = p > result["d1_prev2_high"]
                    result["below_d1_low"]  = p < result["d1_prev2_low"]

    except Exception as e:
        errors.append(str(e))

    if errors:
        result["td_errors"] = errors
    return result

# ════════════════════════════════════════════════════════════════
#  CSV PARSER — EACore_Logger universal
# ════════════════════════════════════════════════════════════════

# Symbol → EA mapping (handles broker suffixes like .r, .R, cash-1)
SYMBOL_TO_EA = {
    "GBPUSD": "CRv7",
    "CADCHF": "RMv8",
    "EURJPY": "ScalpAsia",
    "GDAXI": "LDBOX", "GER30": "LDBOX", "DE30": "LDBOX", "GER30CASH": "LDBOX",
    "EURUSD": "MilkyWay",
    "XAUUSD": "XAUMS", "GOLD": "XAUMS",
}

# EA operating windows in GMT hours.
# Broker logs are GMT+3 — we convert back to GMT to check window.
EA_WINDOWS_GMT = {
    "CRv7":      (0,  18),
    "RMv8":      (9,  20),
    "ScalpAsia": (22, 24),  # wraps midnight — handled below
    "LDBOX":     (9,  15),
    "MilkyWay":  (0,  24),  # no filter
    "XAUMS":     (12, 17),
}

def broker_dt_in_window(dt_str: str, ea_key: str) -> bool:
    """Return True if the broker timestamp (GMT+3) falls within the EA's GMT window."""
    try:
        dt = datetime.strptime(dt_str[:19], "%Y.%m.%d %H:%M:%S")
        gmt_h = (dt.hour - 3) % 24          # GMT+3 → GMT
        w_start, w_end = EA_WINDOWS_GMT.get(ea_key, (0, 24))
        if w_end == 24:
            return gmt_h >= w_start
        if w_start > w_end:                  # wraps midnight (ScalpAsia 22-24)
            return gmt_h >= w_start or gmt_h < w_end
        return w_start <= gmt_h < w_end
    except:
        return True                          # include if unparseable

def detect_ea_from_rows(rows):
    for row in rows[:30]:
        ea_col = row.get("EA", "").strip()
        # Clean symbol: remove suffix (.r .R), handle GER30cash-1 → GER30CASH
        raw_sym = row.get("Symbol", "").upper()
        sym = re.sub(r'[.\-]R$', '', raw_sym).replace("-1","").replace(".","").strip()
        det = row.get("Detalle", "")
        for key in BACKTEST:
            if key.lower() in ea_col.lower():
                return key
        if sym in SYMBOL_TO_EA:
            return SYMBOL_TO_EA[sym]
        # Partial match (e.g. GER30CASH → GER30)
        for s, k in SYMBOL_TO_EA.items():
            if s in sym:
                return k
        for magic, key in [(8524,"CRv7"),(8903,"RMv8"),(4450,"ScalpAsia"),
                           (123457,"LDBOX"),(310116,"MilkyWay"),(9500,"XAUMS")]:
            if str(magic) in det:
                return key
    return None

def parse_log_csv(content: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(content)))
    if not rows:
        return {"error": "CSV vacío"}

    ea_key = detect_ea_from_rows(rows)
    if not ea_key:
        return {"error": "EA no identificado. Verifica que sea del EACore_Logger."}

    bt = BACKTEST[ea_key]

    # ── Separate in-window vs out-of-window blocks ──────────────
    in_win_blocks  = defaultdict(int)
    out_win_blocks = defaultdict(int)

    atrs, bbls, bbus, prices = [], [], [], []
    spreads_in, spreads_all  = [], []
    trades_open, trades_close = [], []
    in_win_bars = total_bars = 0
    first_dt = last_dt = None

    for row in rows:
        ev  = row.get("Evento","").strip()
        det = row.get("Detalle","").strip()
        dt  = row.get("DateTime","").strip()
        spd = row.get("Spread_p","")
        pnl_raw = row.get("PnL_USD","")
        nota = row.get("Nota","")  # LDBOX puts DST info here

        if not dt or ev == "Evento": continue
        if not first_dt: first_dt = dt
        last_dt = dt

        in_win = broker_dt_in_window(dt, ea_key)

        # Bar counting
        if ev in ("BAR","INDICATORS","FILTER_PASS","FILTER_BLOCK","BLOCK"):
            total_bars += 1
            if in_win: in_win_bars += 1

        # ── Blocks — only count in-window ones for reporting ────
        if ev == "BLOCK":
            block_name = det.strip()
            # Skip OUTSIDE_WINDOW — it IS the time filter, not an anomaly
            if block_name == "OUTSIDE_WINDOW":
                out_win_blocks[block_name] += 1
            elif in_win:
                in_win_blocks[block_name] += 1
            else:
                out_win_blocks[block_name] += 1

        if ev == "FILTER_BLOCK":
            # TIME blocks → never count as actionable
            if "TIME" in det.upper() or "FUERA" in det.upper() or "OUTSIDE" in det.upper():
                out_win_blocks["TIME_FILTER"] += 1
                continue
            if not in_win:
                continue
            # In-window FILTER_BLOCK — categorize
            for kw in ["ATR","SPREAD","BB","RSI","CROSS","BOX","RANGE",
                       "REGIME","TRIGGER","PATTERN","SPIKE","DIST","WPR","KELTNER"]:
                if kw in det.upper():
                    in_win_blocks[f"{kw}_BLOCK"] += 1
                    break

        # ── Indicators — only in-window matter ─────────────────
        if ev == "INDICATORS" and in_win:
            for tag in ("ATR1=", "ATR="):
                if tag in det:
                    try:
                        v = float(det.split(tag)[1].split(" ")[0].split(",")[0])
                        if 0 < v < 100: atrs.append(round(v, 6))
                    except: pass
                    break
            if "BBLo1=" in det:
                try:
                    bbls.append(float(det.split("BBLo1=")[1].split(" ")[0]))
                    bbus.append(float(det.split("BBUp1=")[1].split(" ")[0]))
                except: pass
            for tag in ("C1=", "Close="):
                if tag in det:
                    try:
                        v = float(det.split(tag)[1].split(" ")[0].split(",")[0])
                        if 0.1 < v < 100000: prices.append(round(v, 5))
                    except: pass
                    break

        # ── Spread ──────────────────────────────────────────────
        if spd:
            try:
                sv = float(spd)
                spreads_all.append(sv)
                if in_win: spreads_in.append(sv)
            except: pass

        # ── Trades — always relevant ────────────────────────────
        if ev == "TRADE_OPEN" or ("OPEN" in ev and row.get("Price","")):
            try: price_v = float(row.get("Price",0) or 0)
            except: price_v = 0
            try: lots_v = float(row.get("Lots",0) or 0)
            except: lots_v = 0
            trades_open.append({
                "dt": dt, "dir": "BUY" if "BUY" in det.upper() else "SELL",
                "price": price_v, "lots": lots_v,
            })

        if ev in ("EXIT_TP","EXIT_SL","TRADE_CLOSE","WIN","LOSS","DEAL_OUT"):
            try: pnl = float(pnl_raw)
            except: pnl = 0.0
            trades_close.append({"dt": dt, "exit_type": ev, "pnl": round(pnl,2)})

    sl_count = sum(1 for t in trades_close if "SL" in t["exit_type"] or t["pnl"] < -1)
    tp_count = sum(1 for t in trades_close if "TP" in t["exit_type"] or t["pnl"] > 0)
    realized_pnl = round(sum(t["pnl"] for t in trades_close), 2)

    # Dominant block is ONLY from in-window events
    dominant_block = max(in_win_blocks, key=in_win_blocks.get) if in_win_blocks else "NONE"

    # ATR stats
    atr_avg = round(sum(atrs)/len(atrs), 6) if atrs else None
    atr_thresh = bt.get("atr_threshold")

    # ── Status ──────────────────────────────────────────────────
    expected_blocks = bt.get("main_blockers", [])
    block_is_expected = dominant_block == "NONE" or any(
        eb.replace("_","").upper() in dominant_block.replace("_","").upper()
        for eb in expected_blocks
    )
    if sl_count > bt.get("alert_sl_count", 99):
        status = "ACCIÓN"
        reason = f"{sl_count} SLs — supera umbral {bt['alert_sl_count']}"
    elif len(trades_open) == 0 and block_is_expected:
        status = "NORMAL"
        reason = f"0 trades — {dominant_block} esperado según backtest"
    elif len(trades_open) == 0 and not block_is_expected and in_win_bars > 3:
        status = "REVISAR"
        reason = f"0 trades — bloqueador inusual en ventana: {dominant_block}"
    elif len(trades_open) > 0:
        status = "NORMAL"
        reason = f"{len(trades_open)} trades detectados"
    else:
        status = "NORMAL"
        reason = "Sin actividad en ventana operativa"

    # Date extraction
    date_str = first_dt[:10].replace(".", "-") if first_dt else \
               datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "ea_key": ea_key,
        "ea_name": bt["name"],
        "symbol": bt["symbol"],
        "date": date_str,
        "period_start": first_dt,
        "period_end": last_dt,
        "in_window_bars": in_win_bars,
        "total_bars": total_bars,
        "trades_open": len(trades_open),
        "trades_close": len(trades_close),
        "sl_count": sl_count,
        "tp_count": tp_count,
        "realized_pnl": realized_pnl,
        # blocks: ONLY in-window — what actually matters operationally
        "blocks": dict(in_win_blocks),
        "out_window_events": sum(out_win_blocks.values()),
        "dominant_block": dominant_block,
        "block_is_expected": block_is_expected,
        "total_in_window_blocks": sum(in_win_blocks.values()),
        "atr_avg": atr_avg,
        "atr_min": round(min(atrs), 6) if atrs else None,
        "atr_max": round(max(atrs), 6) if atrs else None,
        "atr_pct_threshold": round(atr_avg/atr_thresh*100, 1) if atr_avg and atr_thresh else None,
        "price_last": prices[-1] if prices else None,
        "bbl_avg": round(sum(bbls)/len(bbls), 5) if bbls else None,
        "bbu_avg": round(sum(bbus)/len(bbus), 5) if bbus else None,
        "spread_avg": round(sum(spreads_in)/len(spreads_in), 2) if spreads_in else None,
        "spread_max": round(max(spreads_in), 2) if spreads_in else None,
        "status": status,
        "status_reason": reason,
        "recent_trades": trades_open[-3:],
        "recent_closes": trades_close[-3:],
    }

# ════════════════════════════════════════════════════════════════
#  GROQ — análisis con contexto completo
# ════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Eres el sistema experto de monitoreo de un portafolio algorítmico de 6 Expert Advisors operando en MetaTrader 5.

MANDATO: Responder UNA pregunta — ¿Está el comportamiento live dentro de lo que predice el backtest validado?

REGLA OPERATIVA: Período de observación de 3 meses. NO se toca ningún EA. Solo se monitorea y se aprende.

CONTEXTO DE ANÁLISIS:
Recibes 4 capas de información:
  1. BACKTEST_REFERENCE: métricas reales IS/OOS extraídas de los archivos HTM
  2. HISTÓRICO_LIVE: logs de días anteriores (acumulados desde el deploy)
  3. LOG_HOY: CSV del día parseado (bloqueadores, ATR, spreads, trades)
  4. TWELVE_DATA: indicadores reales del mercado ahora mismo (GMT+3)

TU ANÁLISIS debe cubrir:
A) DIAGNÓSTICO: ¿Qué está pasando hoy exactamente? ¿Qué bloqueador? ¿Es esperado?
B) PATRÓN HISTÓRICO: ¿Cuántos días seguidos con este patrón? ¿Se parece a algún período del OOS?
C) COMPARATIVA MARKET: Lo que ve Twelve Data (ATR, BB, precio) vs lo que el log reporta
D) PREDICCIÓN: Basado en el histórico de días y las condiciones actuales del mercado, 
   ¿cuándo es probable que se active el próximo trade? ¿Qué condición debe cambiar?

FORMATO: Directo, técnico, sin introducción. Máximo 320 palabras.
Termina SIEMPRE con exactamente estas dos líneas:
ESTADO: [NORMAL|REVISAR|ACCIÓN] — [razón concisa]
PREDICCIÓN: [estimación de próxima señal con condición específica]"""

def _build_td_history_section(ea_key: str, snaps: list) -> str:
    """
    Build a compact multi-day TD indicator trend for Groq context.
    Each EA gets the indicators most relevant to its trading logic.
    snaps: list of td_snapshot dicts, chronological order.
    """
    if not snaps:
        return ""

    recent = snaps[-7:]  # last 7 snapshots
    lines  = ["\n  Evolución indicadores TD (últimos días guardados):"]

    if ea_key == "CRv7":
        # RSI trend + EMA vs SMA cross distance
        rsis = [(s.get("ts","?")[:10], s.get("rsi")) for s in recent if s.get("rsi")]
        emas = [s.get("ema") for s in recent if s.get("ema")]
        smas = [s.get("sma") for s in recent if s.get("sma")]
        if rsis:
            rsi_vals = " → ".join(f"{v:.1f}" for _, v in rsis[-5:])
            above50  = sum(1 for _, v in rsis if v and v > 50)
            lines.append(f"  RSI(36): {rsi_vals} | {above50}/{len(rsis)} días > 50")
        if emas and smas and len(emas) == len(smas):
            diffs = [(e - s) / 0.0001 for e, s in zip(emas, smas)]
            diff_str = " → ".join(f"{d:+.1f}p" for d in diffs[-5:])
            cross_dir = "→ acercándose BULL" if diffs[-1] > diffs[0] and diffs[-1] < 0 else \
                        "→ acercándose BEAR" if diffs[-1] < diffs[0] and diffs[-1] > 0 else \
                        "→ cruzado BULL" if diffs[-1] > 0 else "→ cruzado BEAR"
            lines.append(f"  EMA-SMA gap: {diff_str} {cross_dir}")

    elif ea_key == "RMv8":
        # ATR TD trend + BB position
        atrs = [s.get("atr") for s in recent if s.get("atr")]
        pcts = [s.get("atr_pct_threshold") for s in recent if s.get("atr_pct_threshold")]
        bb_pos = [s.get("bb_signal") for s in recent]
        if atrs:
            atr_str = " → ".join(f"{a:.6f}" for a in atrs[-5:])
            trend = "↑" if atrs[-1] > atrs[0] else "↓"
            lines.append(f"  ATR TD diario: {atr_str} {trend}")
        if pcts:
            pct_str = " → ".join(f"{p:.0f}%" for p in pcts[-5:])
            lines.append(f"  % umbral TD: {pct_str}")
        sigs = [s for s in bb_pos if s]
        if sigs:
            lines.append(f"  Señales BB: {', '.join(sigs[-5:]) or 'ninguna'}")

    elif ea_key == "ScalpAsia":
        # Keltner + WPR — most relevant at session time
        wprs = [s.get("wpr_1") for s in recent if s.get("wpr_1") is not None]
        if wprs:
            wpr_str = " → ".join(f"{w:.1f}" for w in wprs[-5:])
            extremes = sum(1 for w in wprs if w < -81 or w > -35)
            lines.append(f"  WPR(38) cierre sesión: {wpr_str} | {extremes}/{len(wprs)} días en zona extrema")
        kcu = [s.get("kc_upper") for s in recent if s.get("kc_upper")]
        kcl = [s.get("kc_lower") for s in recent if s.get("kc_lower")]
        prices = [s.get("price") for s in recent if s.get("price")]
        if kcu and kcl and prices:
            widths = [(u - l) for u, l in zip(kcu[-3:], kcl[-3:])]
            w_str = " → ".join(f"{w:.3f}" for w in widths)
            lines.append(f"  Ancho Keltner últimos 3d: {w_str} (canal más {'ancho = más vol' if widths[-1] > widths[0] else 'estrecho = menos vol'})")

    elif ea_key == "LDBOX":
        # EMA H1 bias trend + ATR H1 (expected range size)
        atrs = [s.get("atr") for s in recent if s.get("atr")]
        prices = [s.get("price") for s in recent if s.get("price")]
        # Get H1 EMA (stored as ema_1h or ema)
        emas = [s.get("ema_1h") or s.get("ema") for s in recent]
        emas = [e for e in emas if e]
        if atrs:
            atr_units = [a * 10 for a in atrs]  # DAX: 1 unit = 0.1
            atr_str = " → ".join(f"{u:.1f}u" for u in atr_units[-5:])
            avg_range = sum(atr_units) / len(atr_units) * 2.5  # SLFc=2.5
            lines.append(f"  ATR H1 (rango caja esperado): {atr_str}")
            lines.append(f"  Rango SL esperado (2.5x ATR): ~{avg_range:.0f} unidades | Máx válido: 135u")
        if emas and prices:
            biases = ["ALCISTA" if p > e else "BAJISTA" for p, e in zip(prices, emas)]
            lines.append(f"  Bias H1 últimos días: {' → '.join(biases[-5:])}")

    elif ea_key == "MilkyWay":
        # BB position + MACD + Stoch — all 3 are exit signals
        bb_sigs = [s.get("bb_signal") for s in recent]
        macds   = [s.get("macd_hist_1") for s in recent if s.get("macd_hist_1") is not None]
        stochs  = [s.get("stoch_k") for s in recent if s.get("stoch_k") is not None]
        active_bb = sum(1 for s in bb_sigs if s)
        if bb_sigs:
            lines.append(f"  BB signal: {[s or '—' for s in bb_sigs[-5:]]} | {active_bb}/{len(bb_sigs)} días con señal")
        if macds:
            macd_str = " → ".join(f"{m:+.5f}" for m in macds[-5:])
            sign_changes = sum(1 for i in range(1, len(macds)) if (macds[i]>0) != (macds[i-1]>0))
            lines.append(f"  MACD hist: {macd_str} | {sign_changes} cambios de signo (= exits)")
        if stochs:
            stoch_str = " → ".join(f"{s:.1f}" for s in stochs[-5:])
            above71 = sum(1 for s in stochs if s > 71)
            below29 = sum(1 for s in stochs if s < 29)
            lines.append(f"  Stoch K: {stoch_str} | {above71}d >71 | {below29}d <29 (zonas de exit)")

    elif ea_key == "XAUMS":
        # Regime + D1 breakout status + ATR sizing
        prices = [s.get("price") for s in recent if s.get("price")]
        # H4 EMA stored as ema_4h
        emas_h4 = [s.get("ema_4h") or s.get("ema") for s in recent]
        emas_h4 = [e for e in emas_h4 if e]
        atrs_h1 = [s.get("atr_h1") for s in recent if s.get("atr_h1")]
        d1_breaks = [("SOBRE" if s.get("above_d1_high") else
                      "BAJO"  if s.get("below_d1_low")  else
                      "dentro") for s in recent if s.get("price")]

        if prices and emas_h4:
            regimes = []
            band = 0.014  # 1.4%
            for p, e in zip(prices, emas_h4):
                if p > e * (1 + band): regimes.append("ALCISTA")
                elif p < e * (1 - band): regimes.append("BAJISTA")
                else: regimes.append("NEUTRO")
            lines.append(f"  Régimen H4: {' → '.join(regimes[-5:])}")

        if d1_breaks:
            lines.append(f"  D1 breakout: {' → '.join(d1_breaks[-5:])}")

        if atrs_h1:
            sl_sizes = [a * 2.2 for a in atrs_h1]  # SL_ATRMult=2.2
            tp_sizes = [a * 2.2 * 1.5 for a in atrs_h1]  # TPRatio=1.5
            sl_str = " → ".join(f"${s*1000:.0f}" for s in sl_sizes[-3:])
            lines.append(f"  SL esperado (ATR H1 × 2.2): {sl_str} | TP=1.5×SL")

    return "\n".join(lines) if len(lines) > 1 else ""


def build_groq_prompt(ea_key: str, today_log: dict, history: list,
                      td_data: dict) -> str:
    bt  = BACKTEST[ea_key]
    bts = BACKTEST_TRADES.get(ea_key, {})  # compact summary stats

    # ── BACKTEST REFERENCE SECTION ───────────────────────────────
    is_s  = bt["IS"]
    oos_s = bt["OOS"]
    bts_is  = bts.get("IS",  {})
    bts_oos = bts.get("OOS", {})

    bt_section = f"""BACKTEST REFERENCIA (extraído de archivos HTM — datos reales):
  IS  ({is_s.get('years','?')}a, {is_s['trades']}t): WR={is_s['wr']}% PF={is_s['pf']} DD={is_s['dd_pct']}% Net=${is_s['net_profit']:,.0f}
  OOS ({oos_s.get('months','?')}m, {oos_s['trades']}t): WR={oos_s['wr']}% PF={oos_s['pf']} DD={oos_s['dd_pct']}% Net=${oos_s['net_profit']:,.0f}
  Estrategia: {bt['strategy']}
  Entrada: {bt['entry']}
  Salida: {bt['exit']}
  Esperado: {bt['expected_trades_month']} trades/mes | Ventana: {bt['window_gmt']}
  Normal sin trades hasta: {bt['normal_zero_trade_weeks']} semanas | Alerta SL: >{bt['alert_sl_count']} SLs"""

    if bts_oos:
        bt_section += f"""
  OOS exits: TP={bts_oos.get('exits',{}).get('t/p',0)} | SL={bts_oos.get('exits',{}).get('s/l',0)} | Close={bts_oos.get('exits',{}).get('close',0)}
  OOS avg win: ${bts_oos.get('avg_win','?')} | avg loss: ${bts_oos.get('avg_loss','?')}
  OOS best: ${bts_oos.get('best_trade','?')} | worst: ${bts_oos.get('worst_trade','?')}"""

    # ── HISTORICAL LIVE SECTION ──────────────────────────────────
    if history:
        total_days   = len(history)
        total_trades = sum(d.get("trades_open", 0) for d in history)
        total_sl     = sum(d.get("sl_count", 0)    for d in history)
        total_tp     = sum(d.get("tp_count", 0)    for d in history)
        total_pnl    = sum(d.get("realized_pnl",0) for d in history)
        zero_days    = sum(1 for d in history if d.get("trades_open", 0) == 0)
        last_trade   = next((d["date"] for d in reversed(history)
                             if d.get("trades_open", 0) > 0), "nunca")

        # Current zero-trade streak
        consec_zero = 0
        for d in reversed(history):
            if d.get("trades_open", 0) == 0: consec_zero += 1
            else: break

        # ATR trend (7 days) — only for EAs with ATR data
        atr_7d = [d.get("atr_avg") for d in history[-7:] if d.get("atr_avg")]
        atr_pct_7d = [d.get("atr_pct_threshold") for d in history[-7:] if d.get("atr_pct_threshold")]

        if len(atr_7d) >= 2:
            delta = atr_7d[-1] - atr_7d[0]
            trend_sym = "↑" if delta > 0.000010 else "↓" if delta < -0.000010 else "→"
            atr_trend_str = f"{trend_sym} {delta/atr_7d[0]*100:+.1f}% en 7 días"
        else:
            atr_trend_str = ""

        # Top blockers across all history
        all_blocks = defaultdict(int)
        for d in history:
            for k, v in d.get("blocks", {}).items():
                all_blocks[k] += v
        top3 = sorted(all_blocks.items(), key=lambda x: -x[1])[:3]

        # Trade pace vs OOS
        oos_daily = oos_s["trades"] / (oos_s.get("months", 12) * 22)
        live_daily = total_trades / total_days if total_days else 0
        pace_pct   = live_daily / oos_daily * 100 if oos_daily else 0

        # Days since start
        try:
            import datetime as dt_mod
            start = dt_mod.date.fromisoformat(history[0]["date"])
            days_since = (datetime.now(timezone.utc).date() - start).days
        except:
            days_since = total_days

        hist_section = f"""
HISTÓRICO LIVE ({total_days} días registrados, {days_since} días desde inicio):
  Trades: {total_trades} total | TP: {total_tp} | SL: {total_sl} | PnL: ${total_pnl:.2f}
  Sin trade: {zero_days}/{total_days} días ({zero_days/total_days*100:.0f}%) | Racha actual: {consec_zero} días consecutivos
  Último trade: {last_trade}
  Ritmo live: {live_daily:.3f}/día vs OOS {oos_daily:.3f}/día → {pace_pct:.0f}% del esperado
  Bloqueadores históricos en ventana: {', '.join(f'{k}:{v}' for k,v in top3) or 'sin datos'}"""

        if atr_7d:
            atr_str = " → ".join(f"{a:.6f}" for a in atr_7d[-5:])
            pct_str = " → ".join(f"{p:.0f}%" for p in atr_pct_7d[-5:]) if atr_pct_7d else ""
            hist_section += f"""
  ATR últimos 7d: {atr_str} {atr_trend_str}"""
            if pct_str:
                hist_section += f"""
  % umbral (0.0005): {pct_str}"""

        # ── TD HISTORICAL TREND (from saved snapshots) ────────────
        td_snaps = [d.get("td_snapshot", {}) for d in history if d.get("td_snapshot")]
        if td_snaps:
            hist_section += _build_td_history_section(ea_key, td_snaps)

    else:
        hist_section = "\nHISTÓRICO LIVE: Primer día registrado — sin histórico previo"

    # ── TWELVE DATA SECTION ──────────────────────────────────────
    tdi = bt.get("td_indicators", {})
    if td_data and not td_data.get("error"):
        td_lines = [
            f"\nTWELVE DATA — {td_data['symbol']} {td_data['interval']} "
            f"GMT+3 @ {td_data.get('candle_time','?')[:16]}:"
        ]
        p = td_data.get("price")
        if p: td_lines.append(f"  Precio: {p:.5f}")

        if td_data.get("rsi") is not None:
            rsi = td_data["rsi"]
            td_lines.append(f"  RSI({tdi.get('rsi_period',36)}): {rsi:.2f} → {'ALCISTA (>50)' if rsi>50 else 'BAJISTA (<50)'}")
        if td_data.get("ema") is not None:
            ema = td_data["ema"]
            sma = td_data.get("sma")
            cross = ""
            if sma:
                cross = f" | EMA {'>' if ema>sma else '<'} SMA → {'BULL' if ema>sma else 'BEAR'}"
            td_lines.append(f"  EMA({tdi.get('ema_period',69)}): {ema:.5f}{cross}")
        if td_data.get("bb_upper"):
            bb_u = td_data["bb_upper"]
            bb_l = td_data["bb_lower"]
            bb_m = td_data["bb_mid"]
            pct  = td_data.get("price_pct_in_bb", "?")
            sig  = td_data.get("bb_signal")
            du   = td_data.get("dist_to_upper_pips", "?")
            dl   = td_data.get("dist_to_lower_pips", "?")
            td_lines.append(f"  BB({tdi.get('bb_period',30)},{tdi.get('bb_sd',2.4)}σ): Up={bb_u:.5f} Mid={bb_m:.5f} Lo={bb_l:.5f}")
            td_lines.append(f"  Precio en BB: {pct}% | Dist upper: {du}p | Dist lower: {dl}p | Señal: {sig or 'ninguna'}")
        if td_data.get("atr") is not None:
            atr_td = td_data["atr"]
            pct_td = td_data.get("atr_pct_threshold")
            above  = td_data.get("atr_above_threshold")
            td_lines.append(
                f"  ATR({tdi.get('atr_period',38)}) M30: {atr_td:.6f}"
                + (f" → {pct_td:.1f}% umbral {'✓ ACTIVO' if above else '✗ BLOQUEADO'}" if pct_td else "")
            )
        if td_data.get("kc_upper"):
            td_lines.append(f"  Keltner({tdi.get('kc_period',10)},×{tdi.get('kc_mult',2.7)}): Up={td_data['kc_upper']:.3f} Lo={td_data['kc_lower']:.3f}")
        if td_data.get("wpr_1") is not None:
            wpr = td_data["wpr_1"]
            td_lines.append(
                f"  WPR({tdi.get('willr_period',38)}): {wpr:.1f} "
                f"| BUY signal: {td_data.get('wpr_buy_signal',False)} "
                f"| SELL signal: {td_data.get('wpr_sell_signal',False)}"
            )
        if td_data.get("d1_prev2_high"):
            h2 = td_data["d1_prev2_high"]
            l2 = td_data["d1_prev2_low"]
            pos = "SOBRE HIGH →breakout" if td_data.get("above_d1_high") else \
                  "BAJO LOW →breakout" if td_data.get("below_d1_low") else "dentro del rango"
            td_lines.append(f"  D1 anteayer H:{h2:.2f} L:{l2:.2f} | Precio {pos}")
        ema_tf = td_data.get(f"ema_{tdi.get('ema_tf','4h')}")
        if ema_tf and p:
            band = tdi.get("regime_band", 1.4) / 100
            dist_pct = (p - ema_tf) / ema_tf * 100
            regime = "ALCISTA" if p > ema_tf*(1+band) else "BAJISTA" if p < ema_tf*(1-band) else "NEUTRO"
            td_lines.append(f"  EMA H4({tdi.get('ema_period',31)}): {ema_tf:.2f} | Dist: {dist_pct:+.2f}% | Régimen: {regime}")
        if td_data.get("stoch_k") is not None:
            td_lines.append(f"  Stoch K: {td_data['stoch_k']:.1f} D: {td_data.get('stoch_d','?'):.1f}")
        if td_data.get("macd_hist_1") is not None:
            h1 = td_data["macd_hist_1"]
            h2v = td_data.get("macd_hist_2", 0)
            cross_m = "↑ BULL cross" if td_data.get("macd_bullish_cross") else \
                      "↓ BEAR cross" if td_data.get("macd_bearish_cross") else ""
            td_lines.append(f"  MACD hist: {h1:.6f} (prev:{h2v:.6f}) {cross_m}")
        td_section = "\n".join(td_lines)
    else:
        err = td_data.get("error", "sin clave API") if td_data else "sin clave API"
        td_section = f"\nTWELVE DATA: No disponible — {err}"

    # ── TODAY LOG SECTION ────────────────────────────────────────
    blocks_in = today_log.get("blocks", {})
    blocks_str = ", ".join(f"{k}:{v}" for k,v in
                            sorted(blocks_in.items(), key=lambda x: -x[1])) or "ninguno"
    out_win = today_log.get("out_window_events", 0)

    atr_log_str = ""
    if today_log.get("atr_avg"):
        thresh = bt.get("atr_threshold")
        a = today_log["atr_avg"]
        atr_log_str = (f"\n  ATR log broker: {a:.6f} ({a/thresh*100:.1f}% umbral)"
                       if thresh else f"\n  ATR log broker: {a:.6f}")

    log_section = f"""
LOG HOY — ventana operativa ({today_log.get('in_window_bars', today_log.get('bar_count',0))} barras en ventana / {today_log.get('total_bars',0)} total):
  Trades abiertos: {today_log['trades_open']} | Cerrados: {today_log['trades_close']} (TP:{today_log['tp_count']} SL:{today_log['sl_count']})
  PnL hoy: ${today_log['realized_pnl']}
  Spread avg/max (en ventana): {today_log.get('spread_avg','—')}p / {today_log.get('spread_max','—')}p{atr_log_str}
  Bloqueadores en ventana: {blocks_str}
  Eventos fuera de ventana ignorados: {out_win}
  Diagnóstico: {today_log['status']} — {today_log['status_reason']}"""

    return f"""{bt_section}
{hist_section}
{log_section}
{td_section}

Analiza las 4 capas. Sé específico con números reales. Máx 300 palabras."""

async def call_groq(prompt: str) -> dict:
    if not GROQ_KEY:
        return {"analysis": "GROQ_KEY no configurado.", "status": "REVISAR", "reason": "Sin clave", "prediction": "—"}
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 700, "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=35) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers=headers, json=body)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    status, reason, prediction = "NORMAL", "", "—"
    for line in reversed(text.split("\n")):
        ls = line.strip()
        if ls.upper().startswith("PREDICCIÓN:") or ls.upper().startswith("PREDICCION:"):
            prediction = ls.split(":", 1)[1].strip()
        if ls.upper().startswith("ESTADO:"):
            rest = ls.split(":", 1)[1].strip()
            for s in ("ACCIÓN", "ACCION", "REVISAR", "NORMAL"):
                if s in rest.upper():
                    status = "ACCIÓN" if "ACCI" in s else s
                    reason = rest[rest.upper().find(s)+len(s):].strip(" —-")
                    break
            break

    clean = "\n".join(l for l in text.split("\n")
                      if not l.strip().upper().startswith(("ESTADO:", "PREDICCIÓN:","PREDICCION:"))
                     ).strip()
    return {"analysis": clean, "status": status, "reason": reason, "prediction": prediction}

# ════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "2.0",
        "groq": bool(GROQ_KEY),
        "twelve_data": bool(TD_KEY),
        "github": bool(GH_TOKEN and GH_REPO),
        "github_repo": GH_REPO or "not configured",
    }

@app.get("/setup")
async def setup_repo():
    """
    One-tap GitHub setup — call this ONCE after deploy.
    Creates data/history/{EA}/ folders and uploads both JSON reference files.
    Safe to call multiple times (skips existing files).
    """
    if not GH_TOKEN or not GH_REPO:
        return JSONResponse({
            "error": "GITHUB_TOKEN y GITHUB_REPO deben estar configurados en Railway."
        }, status_code=503)

    EAS = ["CRv7", "RMv8", "ScalpAsia", "LDBOX", "MilkyWay", "XAUMS"]
    results = {}

    # 1. Create .gitkeep in each EA history folder
    for ea in EAS:
        path = f"data/history/{ea}/.gitkeep"
        existing = await gh_read(path)
        if existing:
            results[f"folder_{ea}"] = "already_exists"
            continue
        ok = await gh_write(path, "", f"init: history folder for {ea}")
        results[f"folder_{ea}"] = "created" if ok else "error"

    # 2. Upload reference JSON files (fixed, never change)
    for fname, data in [
        ("backtest_reference.json", BACKTEST),
        ("backtest_summary.json",   BACKTEST_TRADES),
    ]:
        existing = await gh_read(fname)
        sha = existing["sha"] if existing else None
        ok = await gh_write(fname, data, f"init: {fname} — extracted from HTM files", sha)
        results[fname] = "uploaded" if ok else "error"

    errors = [k for k, v in results.items() if v == "error"]
    success = len(results) - len(errors)

    return {
        "status": "ok" if not errors else "partial",
        "message": f"{success}/{len(results)} operaciones exitosas.",
        "repo": GH_REPO,
        "branch": GH_BRANCH,
        "details": results,
        "next_step": "El repo está listo. Ahora sube el primer CSV desde la app." if not errors
                     else f"Revisa las variables de Railway. Errores: {errors}",
    }

@app.get("/backtest")
async def backtest():
    """Return the full backtest reference (read-only)."""
    return BACKTEST

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Upload a CSV log file → auto-detect EA → parse → save to GitHub → return analysis.
    """
    content = (await file.read()).decode("utf-8", errors="replace")
    today   = parse_log_csv(content)
    if "error" in today:
        return JSONResponse({"error": today["error"]}, status_code=422)

    ea_key   = today["ea_key"]
    date_str = today["date"]

    # 1. Save to GitHub
    saved = await save_log_to_github(ea_key, date_str, today)

    # 2. Load history
    history = await load_history_from_github(ea_key)

    # 3. Fetch TD indicators
    td_data = {}
    if TD_KEY:
        try:
            td_data = await fetch_ea_indicators(ea_key)
        except Exception as e:
            td_data = {"error": str(e)}

    return {
        "ea_key":    ea_key,
        "date":      date_str,
        "saved_gh":  saved,
        "log":       today,
        "history_days": len(history),
        "td":        td_data,
    }

@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    """
    Full pipeline: Upload CSV → parse → GitHub → history → TD → Groq analysis.
    TD snapshot is saved alongside the log for historical trend tracking.
    """
    content = (await file.read()).decode("utf-8", errors="replace")
    today   = parse_log_csv(content)
    if "error" in today:
        return JSONResponse({"error": today["error"]}, status_code=422)

    ea_key   = today["ea_key"]
    date_str = today["date"]

    # Fetch TD indicators FIRST (before saving) so we can store them with the log
    td_data = {}
    if TD_KEY:
        try:
            td_data = await fetch_ea_indicators(ea_key)
        except Exception as e:
            td_data = {"error": str(e)}

    # Save log + compact TD snapshot to GitHub together
    # This lets Groq see the evolution of market indicators over time
    td_snapshot = _compact_td_snapshot(ea_key, td_data)
    today_with_td = {**today, "td_snapshot": td_snapshot}
    await save_log_to_github(ea_key, date_str, today_with_td)

    # Load full history (now includes TD snapshots from previous days)
    history = await load_history_from_github(ea_key)

    # Groq
    prompt = build_groq_prompt(ea_key, today, history, td_data)
    groq_r = await call_groq(prompt)

    return {
        "ea_key":       ea_key,
        "ea_name":      today["ea_name"],
        "date":         date_str,
        "log":          today,
        "td":           td_data,
        "history_days": len(history),
        **groq_r,
    }


def _compact_td_snapshot(ea_key: str, td: dict) -> dict:
    """
    Extract only the key TD values for historical storage.
    Keeps the stored JSON small while preserving what Groq needs for trend analysis.
    """
    if not td or td.get("error"):
        return {}

    snap = {"ts": td.get("candle_time", "")[:16]}

    # Fields useful for trend analysis across days
    for f in ["price", "rsi", "ema", "sma", "atr",
              "bb_upper", "bb_lower", "bb_mid", "bb_signal",
              "atr_pct_threshold", "atr_above_threshold",
              "kc_upper", "kc_lower", "wpr_1",
              "stoch_k", "macd_hist_1",
              "above_d1_high", "below_d1_low",
              "d1_prev2_high", "d1_prev2_low",
              "atr_h1"]:
        if td.get(f) is not None:
            snap[f] = td[f]

    # EMA on higher timeframe (LDBOX H1, XAUMS H4)
    for k in td:
        if k.startswith("ema_") and td[k] is not None:
            snap[k] = td[k]

    return snap

@app.get("/history/{ea_key}")
async def get_history(ea_key: str, days: int = 30):
    """Get accumulated log history for an EA."""
    if ea_key not in BACKTEST:
        return JSONResponse({"error": "EA not found"}, status_code=404)
    history = await load_history_from_github(ea_key, days)
    return {"ea_key": ea_key, "days": len(history), "data": history}

@app.post("/upload_batch")
async def upload_batch(files: list[UploadFile] = File(default=...)):
    """
    Upload multiple CSV files at once — for loading historical logs.
    Each file is auto-detected, parsed (window-filtered), and saved to GitHub.
    Returns a summary of what was processed.
    """
    results = []
    errors  = []
    saved   = 0

    for file in files:
        try:
            content = (await file.read()).decode("utf-8", errors="replace")
            data = parse_log_csv(content)
            if "error" in data:
                errors.append({"file": file.filename, "error": data["error"]})
                continue

            ea_key   = data["ea_key"]
            date_str = data["date"]

            # Check if already exists in GitHub — skip if so
            existing = await gh_read(f"data/history/{ea_key}/{date_str}.json")
            if existing:
                results.append({
                    "file": file.filename,
                    "ea": ea_key,
                    "date": date_str,
                    "status": "skipped_exists",
                })
                continue

            ok = await save_log_to_github(ea_key, date_str, data)
            if ok:
                saved += 1
                results.append({
                    "file": file.filename,
                    "ea": ea_key,
                    "date": date_str,
                    "status": "saved",
                    "blocks": data.get("blocks", {}),
                    "trades": data.get("trades_open", 0),
                })
            else:
                errors.append({"file": file.filename, "error": "GitHub write failed"})

        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)})

    return {
        "processed": len(files),
        "saved": saved,
        "skipped": len([r for r in results if r["status"] == "skipped_exists"]),
        "errors": len(errors),
        "results": results,
        "error_details": errors,
    }

@app.get("/indicators/{ea_key}")
async def get_indicators(ea_key: str):
    """Get current TD indicators for an EA."""
    if ea_key not in BACKTEST:
        return JSONResponse({"error": "EA not found"}, status_code=404)
    if not TD_KEY:
        return JSONResponse({"error": "TWELVE_DATA_KEY not configured"}, status_code=503)
    try:
        td = await fetch_ea_indicators(ea_key)
        return td
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/portfolio_status")
async def portfolio_status():
    """Quick status for all EAs (no Groq, uses cached history)."""
    result = {}
    for ea_key, bt in BACKTEST.items():
        if ea_key == "_meta": continue
        history = HISTORY_CACHE.get(ea_key, [])
        if history:
            last = history[-1]
            total_trades = sum(d.get("trades_open",0) for d in history)
            total_sl = sum(d.get("sl_count",0) for d in history)
            result[ea_key] = {
                "loaded": True,
                "days": len(history),
                "last_date": last.get("date"),
                "status": last.get("status","NORMAL"),
                "reason": last.get("status_reason",""),
                "total_trades_live": total_trades,
                "total_sl_live": total_sl,
                "total_pnl_live": round(sum(d.get("realized_pnl",0) for d in history),2),
                "last_trade": next((d["date"] for d in reversed(history)
                                   if d.get("trades_open",0)>0), "nunca"),
            }
        else:
            result[ea_key] = {"loaded": False, "status": "SIN_DATOS"}
    return result

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open("index.html") as f:
        return f.read()
