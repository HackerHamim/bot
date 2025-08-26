import asyncio
import datetime
import random   # just strategy shuffle
import time
import os
import sys
import requests
import pytz
import select
from colorama import Fore, Style, init
from termcolor import colored

# =============== Init ===============
init(autoreset=True)

# Timezone
TIMEZONE = pytz.timezone('Asia/Dhaka')

# OTC API URL
OTC_API_URL = "https://freegiveway.net/otcx.php"

# ===== Strategy Params (defaults) =====
RSI_PERIOD = 3
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MAX_PERIOD = 3           # last up-to 3 candles context allowed

ZIGZAG_DEPTH = 20            # lookback depth
ZIGZAG_DEVIATION = 20         # price tolerance
ZIGZAG_BACKSTEP = 20          # bars separation

EMA_PERIOD = 20              # legacy EMA
EMA_TREND_FILTER = True      # keep legacy trend alignment

# ===== Trade Result Storage =====
TRADE_HISTORY = []           # list of dicts: {"time","asset","dir","outcome"}

# =============== Pretty Console Helpers ===============
def rainbow(text: str) -> str:
    cols = [Fore.RED, Fore.YELLOW, Fore.GREEN, Fore.CYAN, Fore.BLUE, Fore.MAGENTA]
    return ''.join(cols[i % len(cols)] + ch for i, ch in enumerate(text)) + Style.RESET_ALL

def banner(text: str): print(rainbow(text))
def ok(text: str): print(colored(text, "cyan", attrs=["bold"]))
def info(text: str): print(colored(text, "magenta", attrs=["bold"]))
def warn(text: str): print(colored(text, "yellow", attrs=["bold"]))
def err(text: str): print(colored(text, "red", attrs=["bold"]))

# =============== User Inputs (Colorful) ===============
banner("⚙️  DARKHYDRA V3 — Real Candle Result Engine (OTC Version)")
TELEGRAM_BOT_TOKEN = input(rainbow("🤖 Enter your Telegram Bot Token: ")).strip()
TELEGRAM_CHAT_ID = input(rainbow("💬 Enter your Telegram Chat ID: ")).strip()
TAG_USER_ID = input(rainbow("🏷️ Enter Tag User ID (@username or numeric id): ")).strip()

# Pair input system
pairs_input = input(rainbow("📊 Enter your OTC Pairs (comma separated): ")).strip()
ASSETS = [p.strip() for p in pairs_input.split(",") if p.strip()]

# === New: strategy selection (comma or 'all') ===
strats_raw = input(
    rainbow("🧠 Strategies to use (comma or 'all') [RSI,ZIGZAG,COLOR,EMA]: ")
).strip().lower()

def _norm_name(s: str) -> str:
    s = s.strip().lower()
    if s in ("color","color_pattern","pattern","cp"): return "COLOR_PATTERN"
    if s in ("ema","e"): return "EMA"
    if s in ("zigzag","zz","z"): return "ZIGZAG"
    if s in ("rsi","r"): return "RSI"
    return ""

if strats_raw in ("", "all", "a"):
    ENABLED_STRATS = ["RSI", "ZIGZAG", "COLOR_PATTERN", "EMA"]
else:
    ENABLED_STRATS = [ _norm_name(x) for x in strats_raw.split(",") ]
    ENABLED_STRATS = [ x for x in ENABLED_STRATS if x ]  # drop unknowns
    if not ENABLED_STRATS:
        ENABLED_STRATS = ["RSI", "ZIGZAG", "COLOR_PATTERN", "EMA"]

# === New: ZigZag params from user (blank = default) ===
try:
    _depth = input(rainbow(f"🔧 ZigZag depth [{ZIGZAG_DEPTH}]: ")).strip()
    if _depth: ZIGZAG_DEPTH = int(_depth)
    _dev = input(rainbow(f"🔧 ZigZag deviation [{ZIGZAG_DEVIATION}]: ")).strip()
    if _dev: ZIGZAG_DEVIATION = float(_dev)
    _back = input(rainbow(f"🔧 ZigZag backstep [{ZIGZAG_BACKSTEP}]: ")).strip()
    if _back: ZIGZAG_BACKSTEP = int(_back)
except Exception as _:
    warn("Using default ZigZag params.")

# =============== OTC Data Fetching ===============
def fetch_otc_candles(instrument: str, count: int = 60):
    """
    Fetch OTC candles from the API. Returns list of dicts with keys:
    time (str 'YYYY-MM-DD HH:MM:SS'), mid{o,h,l,c}, complete=True
    """
    try:
        params = {"pair": instrument, "count": count}
        r = requests.get(OTC_API_URL, params=params, timeout=15)
        data = r.json()
        if "data" not in data:
            err(f"❌ No data for {instrument}")
            return []
        candles = []
        for item in data["data"]:
            dt_str = item["time"]
            if "T" in dt_str:
                dt_str = dt_str.replace("T", " ").split(".")[0]
            if len(dt_str) == 16:  # "YYYY-MM-DD HH:MM"
                dt_str += ":00"
            candles.append({
                "time": dt_str,
                "mid": {
                    "o": str(item["open"]),
                    "h": str(item["high"]),
                    "l": str(item["low"]),
                    "c": str(item["close"]),
                },
                "complete": True
            })
        return candles
    except Exception as e:
        err(f"❌ Fetch error for {instrument}: {e}")
        return []

def last_completed_candle(instrument: str):
    c = fetch_otc_candles(instrument, count=3)
    c = only_closed(c or [])
    return c[-1] if c else None

# =============== Candle Time Helpers (to avoid running candle) ===============
def _parse_ts(s: str) -> datetime.datetime:
    # normalize various formats
    s = s.replace("T", " ").split(".")[0]
    if len(s) == 16:  # "YYYY-MM-DD HH:MM"
        s += ":00"
    dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return TIMEZONE.localize(dt)

def is_candle_closed(candle) -> bool:
    """
    A candle is considered closed if now >= candle_time + 1 minute.
    Ensures we never analyze the running candle.
    """
    ct = _parse_ts(candle["time"])
    now = datetime.datetime.now(TIMEZONE)
    return now >= ct + datetime.timedelta(minutes=1)

def only_closed(candles):
    """Return only fully closed candles."""
    return [c for c in candles if is_candle_closed(c)]

# =============== Telegram Sender (Always Bold) ===============
async def send_telegram_message_bold(message: str):
    """
    Send Telegram message using official Web API (requests).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    text = f"<b>{message}</b>"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = await asyncio.to_thread(requests.post, url, data=payload, timeout=15)
        if resp.status_code != 200:
            print(f"❌ Telegram send error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"❌ Telegram send exception: {e}")

# =============== OTC Helpers ===============
def candle_direction(candle) -> str:
    """
    Return 'CALL' if close>open, 'PUT' if close<open, 'FLAT' if equal.
    """
    o = float(candle["mid"]["o"]); c = float(candle["mid"]["c"])
    if c > o: return "CALL"
    elif c < o: return "PUT"
    else: return "FLAT"

def get_candle_at_time(instrument: str, target_time: datetime.datetime, retries: int = 90, sleep_s: float = 1.0):
    """
    Poll until we find a candle for the specific target time.
    """
    target_str = target_time.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:00")
    for i in range(retries):
        info(f"Attempt {i+1}/{retries} to get candle for {target_str}")
        candles = fetch_otc_candles(instrument, count=12)
        if candles:
            for candle in candles:
                candle_time = candle.get("time", "")
                # Normalize if any 'T'
                if "T" in candle_time:
                    candle_time = candle_time.replace("T", " ").split(".")[0]
                if target_str in candle_time:
                    info(f"✅ Found candle for {target_str}: {candle_time}")
                    return candle
        time.sleep(sleep_s)
    err(f"❌ Could not find candle for {target_str} after {retries} attempts")
    return None

# =============== Indicators / Strategies ===============
def ema_value(values, period=EMA_PERIOD):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period  # start with SMA
    for price in values[period:]:
        ema = (price - ema) * k + ema
    return ema

def calculate_ema_trend(candles, period=EMA_PERIOD):
    """Return (ema_value, trend) where trend in {BULLISH, BEARISH, NEUTRAL, NO_DATA}"""
    if len(candles) < (period + 1):
        return None, "NO_DATA"
    closes = [float(c["mid"]["c"]) for c in candles]
    # compute EMA up to last 2 points for direction
    k = 2 / (period + 1)
    ema_list = []
    ema = sum(closes[:period]) / period
    ema_list.append(ema)
    for price in closes[period:]:
        ema = (price - ema) * k + ema
        ema_list.append(ema)
    if len(ema_list) < 2:
        return ema_list[-1], "NEUTRAL"
    if ema_list[-1] > ema_list[-2]:
        return ema_list[-1], "BULLISH"
    if ema_list[-1] < ema_list[-2]:
        return ema_list[-1], "BEARISH"
    return ema_list[-1], "NEUTRAL"

def rsi_series(closes, period=RSI_PERIOD):
    """Compute RSI only for the most recent point based on Wilder's method (simple last-step)."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def rsi_strategy(candles):
    """
    If RSI < 30 within last up-to RSI_MAX_PERIOD bars -> CALL
    If RSI > 70 within last up-to RSI_MAX_PERIOD bars -> PUT
    """
    closes = [float(c["mid"]["c"]) for c in candles]
    if len(closes) < RSI_PERIOD + RSI_MAX_PERIOD:
        return None
    # Evaluate RSI at last t offsets (0=last, 1=prev, ... up to MAX_PERIOD-1)
    for back in range(RSI_MAX_PERIOD):
        sub = closes[:len(closes) - back]
        rsi = rsi_series(sub, period=RSI_PERIOD)
        if rsi is None:
            continue
        if rsi < RSI_OVERSOLD:
            return "CALL"
        if rsi > RSI_OVERBOUGHT:
            return "PUT"
    return None

def zigzag_strategy(candles, depth=12, deviation=5, backstep=3):
    """
    ZigZag strategy with depth, deviation, backstep.
    Detect reversal at pivot candle[-2].
    - If pivot high is MAX high within depth & last close < pivot close -> PUT
    - If pivot low  is MIN low within depth & last close > pivot close -> CALL
    """
    if len(candles) < depth + backstep:
        return None

    window = candles[-depth:]
    pivot = candles[-2]
    last = candles[-1]

    pivot_high = float(pivot["mid"]["h"])
    pivot_low  = float(pivot["mid"]["l"])
    pivot_close = float(pivot["mid"]["c"])
    last_close  = float(last["mid"]["c"])

    highs = [float(c["mid"]["h"]) for c in window]
    lows  = [float(c["mid"]["l"]) for c in window]

    # --- Apply deviation filter (absolute price tolerance) ---
    max_high = max(highs)
    min_low  = min(lows)

    # top reversal
    if pivot_high >= (max_high - deviation) and last_close < pivot_close:
        return "PUT"

    # bottom reversal
    if pivot_low <= (min_low + deviation) and last_close > pivot_close:
        return "CALL"

    return None

def color_of(candle):
    d = candle_direction(candle)
    return "GREEN" if d == "CALL" else ("RED" if d == "PUT" else "DOJI")

def color_pattern_strategy(candles):
    """
    Predict 5th candle based on last 4:
    [RED, GREEN, RED, GREEN]  -> PUT
    [GREEN, RED, GREEN, RED]  -> CALL
    """
    if len(candles) < 4:
        return None
    last4 = candles[-4:]
    colors = [color_of(c) for c in last4]
    if colors == ["RED", "GREEN", "RED", "GREEN"]:
        return "PUT"
    if colors == ["GREEN", "RED", "GREEN", "RED"]:
        return "CALL"
    return None

def ema_strategy(candles):
    """
    Legacy EMA filter strategy:
    - Get last candle direction (CALL/PUT) from real body.
    - Only allow if aligns with EMA20 trend.
    """
    ema_val, trend = calculate_ema_trend(candles, period=EMA_PERIOD)
    if trend in ("NO_DATA", "NEUTRAL"):
        return None
    last_candle = candles[-1]
    dir_ = candle_direction(last_candle)
    if dir_ == "FLAT":
        return None
    if EMA_TREND_FILTER:
        if dir_ == "CALL" and trend == "BEARISH":
            return None
        if dir_ == "PUT" and trend == "BULLISH":
            return None
    return dir_

# =============== Strategy Priority Selector ===============
def choose_strategy(asset, candles, enabled, zz_depth, zz_dev, zz_back):
    """
    Shuffle enabled strategies once per analysis and pick first valid signal.
    """
    all_map = {
        "RSI":          (lambda cs: rsi_strategy(cs), "RSI"),
        "ZIGZAG":       (lambda cs: zigzag_strategy(cs, zz_depth, zz_dev, zz_back), "ZIGZAG"),
        "COLOR_PATTERN":(lambda cs: color_pattern_strategy(cs), "COLOR_PATTERN"),
        "EMA":          (lambda cs: ema_strategy(cs), "EMA"),
    }

    # Filter to enabled only
    pairs = [all_map[name] for name in all_map if name in enabled]

    # Shuffle order
    random.shuffle(pairs)

    for fn, name in pairs:
        try:
            direction = fn(candles)
        except Exception as e:
            warn(f"{asset}: {name} error: {e}")
            continue
        if direction in ("CALL", "PUT"):
            info(f"✅ {asset}: {name} strategy → {direction}")
            return name, direction

    warn(f"ℹ️ {asset}: No strategy matched.")
    return None, None

# =============== Formatting (Your Styles) ===============
def format_signal(asset: str, signal_time: datetime.datetime, direction: str, taguserid: str) -> str:
    signal_time = signal_time.astimezone(TIMEZONE)
    trade_place_time = (signal_time + datetime.timedelta(minutes=1)).strftime("%H:%M")
    expire_time = (signal_time + datetime.timedelta(minutes=2)).strftime("%H:%M")
    direction_icon = '🟢 CALL' if direction == 'CALL' else '🔴 PUT'

    return f"""
≡≡ □𝗗𝗔𝗥𝗞𝗛𝗬𝗗𝗥𝗔 𝗩𝟯□ ≡≡

🃏 𝙿𝙰𝙸𝚁 :> {asset}
⏰ 𝚃𝙸𝙼𝙴 :> {trade_place_time}
⌛ 𝙴𝚇𝙿𝙸𝚁𝚈 :> {expire_time}
📊 𝙳𝙸𝚁𝙴𝙲𝚃𝙸𝙾𝙽 :> {direction_icon}

𝙰𝙸 𝚂𝚈𝚂𝚃𝙴𝙼 𝙾𝙽:  𝗗𝗔𝗥𝗞𝗛𝗬𝗗𝗥𝗔 𝗩𝟯
1 ꜱᴛᴇᴘ ᴍᴛɢ _/- ᴜꜱᴇ ꜱᴇꜰᴛʏ

🌐𝙼𝙴𝚂𝚂𝙰𝙶𝙴:  {taguserid}
""".strip()

def format_result(asset: str, signal_time: datetime.datetime, direction: str, outcome: str, taguserid: str, mtg_step: int = 0) -> str:
    trade_place_time = (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M")
    direction_icon = '🟢 CALL' if direction == 'CALL' else '🔴 PUT'

    if outcome == "WIN" and mtg_step == 0:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃𝙰𝙳𝙾~✅! 𝙽𝙾𝙽 𝙼𝚃𝙶 𝚂𝚄𝚁𝙴𝚂𝙷𝙾𝚃 ! ✅"
    elif outcome == "WIN" and mtg_step == 1:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃𝙰𝙳𝙾~✅! 𝙼𝚃𝙶 𝟷 𝚂𝚄𝚁𝙴𝚂𝙷𝙾𝚃 ! ✅"
    else:
        result_line = "LOSS 🚫"

    return f"""
≡≡☲☆𝗔𝗜 𝗥𝗘𝗦𝗨𝗟𝗧𝗔𝗗𝗢☆☲ ≡≡

📊 𝙿𝙰𝙸𝚁: {asset}
🕔 𝚃𝙸𝙼𝙴:〔{trade_place_time}〕
🔰 𝙳𝙸𝚁𝙴𝙲𝚃𝙸𝙾𝙽: {direction_icon}
------------------------------------

{result_line}
🔰𝙵𝙴𝙴𝙳𝙱𝙰𝙲𝙺: {taguserid}
""".strip()

def format_summary():
    wins = sum(1 for r in TRADE_HISTORY if r["outcome"]=="WIN")
    losses = sum(1 for r in TRADE_HISTORY if r["outcome"]=="LOSS")
    total = len(TRADE_HISTORY)
    wr = round((wins/total)*100, 1) if total>0 else 0.0
    lines = [f"{r['time']} - {r['asset']} - {r['dir']}  {'☑' if r['outcome']=='WIN' else '✖'}" for r in TRADE_HISTORY]
    summary = "\n".join(lines) if lines else "(no trades)"
    return f"""
〄•━‼️𝗗𝗔𝗥𝗞𝗕𝗬𝗧𝗘 𝗥𝗘𝗦𝗨𝗟𝗧𝗦 ‼️━•〄
〆📆 - {datetime.datetime.now(TIMEZONE).strftime('%Y.%m.%d')}
┏━━━━━━━𐌀¹𐋄⁹━━━━━━━┓
{summary}
┗━━━━━━━𐌀¹𐋄⁹━━━━━━━┛
〄 WIN: {wins} | 〆 LOSS: {losses} | 〄 Winrate: {wr}%
""".strip()

# =============== Core Signal Generation ===============
def generate_signal_for_asset(asset: str):
    """
    Use multi-strategy priority to decide direction.
    Returns (direction, base_candle_used_for_time_ref, strategy_name)
    """
    raw = fetch_otc_candles(asset, count=60)
    candles = only_closed(raw)  # <<< NEW: only closed candles
    if not candles or len(candles) < 25:
        warn(f"⚠️ {asset}: Not enough closed data.")
        return None, None, None

    strat_name, direction = choose_strategy(
        asset,
        candles,
        ENABLED_STRATS,
        ZIGZAG_DEPTH,
        ZIGZAG_DEVIATION,
        ZIGZAG_BACKSTEP
    )
    if not direction:
        return None, candles[-1], None

    return direction, candles[-1], strat_name

# =============== Trade Process ===============
async def process_asset(asset: str):
    # 1) Generate signal using priority strategies (based on last CLOSED candle set)
    direction, base_candle, strat_name = generate_signal_for_asset(asset)
    if not direction:
        warn(f"⚠️ {asset}: No clear signal. Skipping.")
        return

    # Signal time = now (when we alert)
    signal_time = datetime.datetime.now(datetime.timezone.utc)

    # Announce Signal
    sig_msg = format_signal(asset, signal_time, direction, TAG_USER_ID)
    ok("\n" + sig_msg + "\n")
    await send_telegram_message_bold(sig_msg)

    # 2) Determine trade candle time (signal +1 minute) and wait till it's available
    trade_place_time = (signal_time + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
    info(f"⏳ Waiting trade candle at {trade_place_time.astimezone(TIMEZONE)} for {asset}...")
    trade_candle = get_candle_at_time(asset, trade_place_time, retries=120, sleep_s=2.0)

    if not trade_candle:
        err(f"❌ {asset}: Could not get trade candle in time.")
        res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=0)
        info("\n" + res_msg + "\n")
        await send_telegram_message_bold(res_msg)
        TRADE_HISTORY.append({
            "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
            "asset": asset, "dir": direction, "outcome": "LOSS"
        })
        return

    trade_dir = candle_direction(trade_candle)
    info(f"Trade candle direction: {trade_dir}, Signal: {direction}")

    if (direction == "CALL" and trade_dir == "CALL") or (direction == "PUT" and trade_dir == "PUT"):
        # WIN non-MTG
        res_msg = format_result(asset, signal_time, direction, "WIN", TAG_USER_ID, mtg_step=0)
        info("\n" + res_msg + "\n")
        await send_telegram_message_bold(res_msg)
        TRADE_HISTORY.append({
            "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
            "asset": asset, "dir": direction, "outcome": "WIN"
        })
        return
    else:
        # 3) MTG1: next candle
        mtg_time = trade_place_time + datetime.timedelta(minutes=1)
        info(f"🧪 MTG1: wait candle at {mtg_time.astimezone(TIMEZONE)} for {asset} ...")
        mtg1_candle = get_candle_at_time(asset, mtg_time, retries=120, sleep_s=2.0)
        if not mtg1_candle:
            err(f"❌ {asset}: MTG1 candle not found.")
            res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=1)
            info("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "LOSS"
            })
            return

        mtg1_dir = candle_direction(mtg1_candle)
        info(f"MTG1 candle direction: {mtg1_dir}, Signal: {direction}")
        if (direction == "CALL" and mtg1_dir == "CALL") or (direction == "PUT" and mtg1_dir == "PUT"):
            res_msg = format_result(asset, signal_time, direction, "WIN", TAG_USER_ID, mtg_step=1)
            info("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "WIN"
            })
        else:
            res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=1)
            info("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "LOSS"
            })

# =============== Scheduler + OFF Command ===============
async def main_loop():
    asset_index = 0
    while True:
        # OFF command (non-blocking)
        try:
            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.readline().strip().lower()
                if cmd == "off":
                    summary = format_summary()
                    ok("\n" + summary + "\n")
                    await send_telegram_message_bold(summary)
                    break
        except Exception:
            # Some environments may not support select on stdin; ignore
            pass

        now = datetime.datetime.now(TIMEZONE)
        banner(f"⏰ Current time: {now.strftime('%Y-%m-%d %H:%M:%S')} | Scanning...")

        # Process one asset per cycle (round-robin)
        asset = ASSETS[asset_index]
        info(f"📊 Processing {asset}...")
        await process_asset(asset)

        # Next asset
        asset_index = (asset_index + 1) % len(ASSETS)

        # Sleep to align roughly to minute boundary
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        sleep_secs = 60 - (now_utc.second)
        if sleep_secs < 5:
            sleep_secs += 60
        info(f"🕒 Sleeping ~{sleep_secs}s to align with next M1 window...")
        await asyncio.sleep(sleep_secs)

# =============== Entry ===============
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        warn("\n👋 Exiting gracefully...")
        # On Ctrl+C also send summary
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            summ = format_summary()
            ok("\n" + summ + "\n")
            loop.run_until_complete(send_telegram_message_bold(summ))
        except Exception:
            pass