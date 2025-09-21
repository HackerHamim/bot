import asyncio
import datetime
import random
import time
import os
import sys
import requests
import pytz
import select
from colorama import Fore, Style, init
from termcolor import colored
import html

expiry_date = datetime.datetime(2025, 10, 30)  # Set your expiry date here

if datetime.datetime.now() > expiry_date:
    print("Sorry, your plan has expired. Buy plan DM @monsters_king_1st ")
    exit()

# =============== Init ===============
init(autoreset=True)

# Timezone
TIMEZONE = pytz.timezone('Asia/Dhaka')

# OTC API URL
OTC_API_URL = "https://freegiveway.net/otcx.php"

# ===== Trade Result Storage =====
TRADE_HISTORY = []           # list of dicts: {"time","asset","dir","outcome"}

# =============== Pretty Console Helpers ===============
def rainbow(text: str) -> str:
    cols = [Fore.RED, Fore.YELLOW, Fore.GREEN, Fore.CYAN, Fore.BLUE, Fore.MAGENTA]
    return ''.join(cols[i % len(cols)] + ch for i, ch in enumerate(text)) + Style.RESET_ALL

def banner(text: str): print(rainbow(text))
def ok(text: str): print(colored(text, "cyan", attrs=["bold"]))
def info(text: str): print(colored(text, "white", attrs=["bold"]))
def warn(text: str): print(colored(text, "yellow", attrs=["bold"]))
def err(text: str): print(colored(text, "red", attrs=["bold"]))


# =============== User Inputs (Colorful) ===============
banner("⚙️  XHUNTER  V3 — Real Candle Result Engine (OTC Version)")
name = input(rainbow("BOT NAME :")).strip()
TELEGRAM_BOT_TOKEN = input(rainbow("🤖 Enter your Telegram Bot Token: ")).strip()
TELEGRAM_CHAT_ID = input(rainbow("💬 Enter your Telegram Chat ID: ")).strip()
TAG_USER_ID = input(rainbow("🏷️ Enter Tag User ID (@username or numeric id): ")).strip()

# Pair input system
pairs_input = input(rainbow("📊 Enter your OTC Pairs (comma separated): ")).strip()
ASSETS = [p.strip() for p in pairs_input.split(",") if p.strip()]

# MTG Step input
MTG_STEP = int(input(rainbow("🔢 Enter MTG Step (0, 1, or 2): ")).strip())
if MTG_STEP not in [0, 1, 2]:
    warn("⚠️ Invalid MTG Step. Using default: 0")
    MTG_STEP = 0

info(f"✅ Using MTG Step: {MTG_STEP}")

# =============== OTC Data Fetching ===============
def fetch_otc_candles(instrument: str, count: int = 60):
    """
    Fetch OTC candles from the API. Returns list of dicts with keys:
    time (str 'YYYY-MM-DD HH:MM:SS'), mid{o,h,l,c}, complete=True
    """
    try:
        params = {"pair": instrument, "count": count}
        r = requests.get(OTC_API_URL, params=params, timeout=15)
        
        # Check if response is empty or not JSON
        if not r.content:
            err(f"❌ Empty response for {instrument}")
            return []
            
        # Try to parse JSON
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            err(f"❌ Invalid JSON response for {instrument}: {r.text[:100]}")
            return []
            
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
    Send Telegram message to Telegram chat in bold text.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Escape message safely for HTML, then wrap in bold
    safe_message = f"<b>{html.escape(message)}</b>"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": safe_message,
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
        info(f"CANDEL MOVE ...{i+1}/{retries}")
        candles = fetch_otc_candles(instrument, count=12)
        if candles:
            for candle in candles:
                candle_time = candle.get("time", "")
                # Normalize if any 'T'
                if "T" in candle_time:
                    candle_time = candle_time.replace("T", " ").split(".")[0]
                if target_str in candle_time:
                    ok(f"✅ Found candle for {target_str}: {candle_time}")
                    return candle
        time.sleep(sleep_s)
    err(f"❌ Could not find candle for {target_str} after {retries} attempts")
    return None

# =============== 3-Candle Trend Strategy ===============
def get_trend(open_price, close_price):
    if close_price > open_price:
        return "UP"
    elif close_price < open_price:
        return "DOWN"
    else:
        return "DOJI"

def generate_3candle_signal(asset: str):
    """
    Generate signal based on last 3 candle trends.
    Returns (direction, base_candle)
    """
    try:
        # Fetch last 3 candles
        candles = fetch_otc_candles(asset, count=3)
        if not candles or len(candles) < 3:
            warn(f"⚠️ {asset}: Not enough data for 3-candle analysis.")
            return None, None
            
        # Take last 3 candles
        last_3 = candles[-3:]
        
        # Detect trends
        trends = []
        for candle in last_3:
            open_price = float(candle["mid"]["o"])
            close_price = float(candle["mid"]["c"])
            trend = get_trend(open_price, close_price)
            trends.append(trend)
        
        # Count UP and DOWN
        up_count = trends.count("UP")
        down_count = trends.count("DOWN")
        
        # Final Signal
        if up_count > down_count:
            direction = "CALL"
            info(f"✅ {asset}: 3-Candle UPTREND - Trends: {trends} (UP: {up_count}, DOWN: {down_count})")
        elif down_count > up_count:
            direction = "PUT"
            info(f"✅ {asset}: 3-Candle DOWNTREND - Trends: {trends} (UP: {up_count}, DOWN: {down_count})")
        else:
            info(f"⏸️ {asset}: NO CLEAR TREND - Trends: {trends} (UP: {up_count}, DOWN: {down_count})")
            return None, None
            
        return direction, candles[-1]
        
    except Exception as e:
        err(f"❌ Error generating 3-candle signal for {asset}: {e}")
        return None, None

# =============== Formatting (Your Styles) ===============

def format_signal(asset: str, signal_time: datetime.datetime, direction: str, taguserid: str) -> str:
    signal_time = signal_time.astimezone(TIMEZONE)
    trade_place_time = (signal_time + datetime.timedelta(minutes=1)).strftime("%H:%M")
    direction_icon = 'CALL' if direction == 'CALL' else 'PUT'

    return f"""
≡≡≡【⊰ {name} 𝗣𝗥𝗢 ⊱】≡≡≡

♾ ASSET<⊱ {asset}
⌛ ENTRY<⊱ {trade_place_time}
📊 DIRECTION<⊱ {direction_icon}

🤖 ᴘᴏᴡᴇʀᴇᴅ ʙʏ {name}
📌 MTG STEP: {MTG_STEP}

💬𝐒𝐎𝐅𝐓𝐖𝐀𝐑𝐄 𝐎𝐖𝐍𝐄𝐑:- {taguserid}
""".strip()


import datetime
import pytz

# Timezone
TIMEZONE = pytz.timezone("Asia/Dhaka")

# Global counters
TOTAL_WINS = 0
TOTAL_LOSSES = 0

def format_result(asset: str, signal_time: datetime.datetime, direction: str, outcome: str, taguserid: str, mtg_step: int = 0) -> str:
    global TOTAL_WINS, TOTAL_LOSSES

    trade_place_time = (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M")
    direction_icon = '🟢 CALL' if direction == 'CALL' else '🔴 PUT'

    # === Result Line (mtg step diye) ===
    if outcome == "WIN" and mtg_step == 0:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃>✅⊰ 𝙽𝙾𝙽 𝙼𝚃𝙶 𝚂𝚄𝚁𝙴𝚂𝙷𝙾𝚃 ⊱✅"
    elif outcome == "WIN" and mtg_step == 1:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃>✅⊰ 𝙼𝚃𝙶 𝚂𝚄𝚁𝙴𝚂𝙷𝙾𝚃 ⊱✅"
    elif outcome == "WIN" and mtg_step == 2:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃>✅⊰ 𝙼𝚃𝙶 𝚂𝚄𝚁𝙴𝚂𝙷𝙾𝚃 ⊱✅"
    else:
        result_line = "📌𝚁𝙴𝚂𝚄𝙻𝚃>🚫 LOSS 🚫"

    # === Count update ===
    if outcome == "WIN":
        TOTAL_WINS += 1
    elif outcome == "LOSS":
        TOTAL_LOSSES += 1

    total_trades = TOTAL_WINS + TOTAL_LOSSES
    win_rate = round((TOTAL_WINS / total_trades) * 100, 1) if total_trades > 0 else 0.0

    return f"""
≡≡ ¤𝗥𝗘𝗦𝗨𝗟𝗧𝗔𝗗𝗢¤ ≡≡

♾ ASSET<⊱ {asset}
⌛ ENTRY<⊱ {trade_place_time}
📊 DIRECTION<⊱ {direction_icon}
<><><><><><><><><><><><><><>

🏆 WIN> {TOTAL_WINS} _/- LOSS> {TOTAL_LOSSES}

{result_line}

💬𝙵𝙴𝙴𝙳𝙱𝙰𝙲𝙺: {taguserid}
""".strip()


# Monospace font এ convert করার জন্য map
MONO_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-:. ",
    "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉"
    "𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣"
    "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿﹘∶． "
)

def to_mono(text: str) -> str:
    return text.translate(MONO_MAP)

def format_summary():
    wins = sum(1 for r in TRADE_HISTORY if r["outcome"] == "WIN")
    losses = sum(1 for r in TRADE_HISTORY if r["outcome"] == "LOSS")
    total = len(TRADE_HISTORY)
    wr = round((wins / total) * 100, 1) if total > 0 else 0.0

    # ✅ সব WIN signals detail সহ (monospace এ)
    win_lines = [
        to_mono(f"{r['time']} ╏ {r['asset']} ╏ {r['dir']}  ✅")
        for r in TRADE_HISTORY if r["outcome"] == "WIN"
    ]

    # ❌ শুধু loss times list (monospace এ)
    loss_times = [to_mono(r["time"]) for r in TRADE_HISTORY if r["outcome"] == "LOSS"]

    summary = f"""
{to_mono("==========  PARTIAL  ==========")}

━━━━━━━━ • ━━━━━━━━
                   {to_mono("📆 - " + datetime.datetime.now(TIMEZONE).strftime('%Y.%m.%d'))}
━━━━━━━━ • ━━━━━━━━
                       {to_mono("OTC MARKET")}
━━━━━━━━ • ━━━━━━━━
{chr(10).join(win_lines) if win_lines else to_mono("No WIN signals")}
"""

    if loss_times:
        summary += f"""

━━━━━━━━ • ━━━━━━━━
❌ {to_mono("LOSS TIMES " + ", ".join(loss_times))}
"""
    summary += f"""
━━━━━━━━ • ━━━━━━━━
🧿  {to_mono(f"Total Signal : {total}  ✠  Ratio: ({wr}%)")}
━━━━━━━━ • ━━━━━━━━
{to_mono(f"TOTAL WIN : {wins}  TOTAL LOSS: {losses}")}

{to_mono("======== ⊰  HUNTER PRO ⊱ ========")}
"""
    return summary

def generate_signal_for_asset(asset: str):
    """
    Generate signal using 3-candle trend counting strategy.
    Returns (direction, base_candle_used_for_time_ref)
    """
    direction, base_candle = generate_3candle_signal(asset)
    
    if not direction:
        return None, None

    return direction, base_candle

# =============== Trade Process ===============
async def process_asset(asset: str):
    # 1) Generate signal using 3-candle trend strategy
    direction, base_candle = generate_signal_for_asset(asset)
    if not direction:
        warn(f"⚠️ {asset}: No clear signal. Skipping.")
        return "NO_SIGNAL"

    # Signal time = now (when we alert)
    signal_time = datetime.datetime.now(datetime.timezone.utc)

    # Announce Signal
    sig_msg = format_signal(asset, signal_time, direction, TAG_USER_ID)
    ok("\n" + sig_msg + "\n")
    await send_telegram_message_bold(sig_msg)

    # 2) Determine trade candle time (signal +1 minute) and wait till its available
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
        return "LOSS"

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
        return "WIN"
    elif MTG_STEP == 0:
        # LOSS at MTG Step 0
        res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=0)
        info("\n" + res_msg + "\n")
        await send_telegram_message_bold(res_msg)
        TRADE_HISTORY.append({
            "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
            "asset": asset, "dir": direction, "outcome": "LOSS"
        })
        return "LOSS"
    else:
        # 3) MTG1: next candle
        mtg_time = trade_place_time + datetime.timedelta(minutes=1)
        info(f"🧪 MTG1: wait candle at {mtg_time.astimezone(TIMEZONE)} for {asset} ...")
        mtg1_candle = get_candle_at_time(asset, mtg_time, retries=120, sleep_s=2.0)
        if not mtg1_candle:
            err(f"❌ {asset}: MTG1 candle not found.")
            res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=1)
            warn("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "LOSS"
            })
            return "LOSS"

        mtg1_dir = candle_direction(mtg1_candle)
        info(f"MTG1 candle direction: {mtg1_dir}, Signal: {direction}")
        if (direction == "CALL" and mtg1_dir == "CALL") or (direction == "PUT" and mtg1_dir == "PUT"):
            res_msg = format_result(asset, signal_time, direction, "WIN", TAG_USER_ID, mtg_step=1)
            warn("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "WIN"
            })
            return "WIN"
        elif MTG_STEP == 1:
            # LOSS at MTG Step 1
            res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=1)
            info("\n" + res_msg + "\n")
            await send_telegram_message_bold(res_msg)
            TRADE_HISTORY.append({
                "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                "asset": asset, "dir": direction, "outcome": "LOSS"
            })
            return "LOSS"
        else:
            # 4) MTG2: next candle
            mtg2_time = mtg_time + datetime.timedelta(minutes=1)
            info(f"🧪 MTG2: wait candle at {mtg2_time.astimezone(TIMEZONE)} for {asset} ...")
            mtg2_candle = get_candle_at_time(asset, mtg2_time, retries=120, sleep_s=2.0)
            if not mtg2_candle:
                err(f"❌ {asset}: MTG2 candle not found.")
                res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=2)
                warn("\n" + res_msg + "\n")
                await send_telegram_message_bold(res_msg)
                TRADE_HISTORY.append({
                    "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                    "asset": asset, "dir": direction, "outcome": "LOSS"
                })
                return "LOSS"

            mtg2_dir = candle_direction(mtg2_candle)
            info(f"MTG2 candle direction: {mtg2_dir}, Signal: {direction}")
            if (direction == "CALL" and mtg2_dir == "CALL") or (direction == "PUT" and mtg2_dir == "PUT"):
                res_msg = format_result(asset, signal_time, direction, "WIN", TAG_USER_ID, mtg_step=2)
                warn("\n" + res_msg + "\n")
                await send_telegram_message_bold(res_msg)
                TRADE_HISTORY.append({
                    "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                    "asset": asset, "dir": direction, "outcome": "WIN"
                })
                return "WIN"
            else:
                # LOSS at MTG Step 2
                res_msg = format_result(asset, signal_time, direction, "LOSS", TAG_USER_ID, mtg_step=2)
                info("\n" + res_msg + "\n")
                await send_telegram_message_bold(res_msg)
                TRADE_HISTORY.append({
                    "time": (signal_time.astimezone(TIMEZONE) + datetime.timedelta(minutes=1)).strftime("%H:%M"),
                    "asset": asset, "dir": direction, "outcome": "LOSS"
                })
                return "LOSS"

# =============== Scheduler + OFF Command ===============
async def main_loop():
    current_asset_index = 0
    
    while True:
        # OFF command (non-blocking)
        try:
            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.readline().strip().lower()
                if cmd == "off":
                    summary = format_summary()
                    ok("\n" + summary + "\n")
                    await send_telegram_message_bold(summary)
        except Exception:
            # কিছু environment এ select কাজ নাও করতে পারে
            pass

        now = datetime.datetime.now(TIMEZONE)
        banner(f"⏰ Current time: {now.strftime('%Y-%m-%d %H:%M:%S')} | Scanning...")

        
        # 🔀 Shuffle assets each round
        shuffled_assets = ASSETS[:]
        random.shuffle(shuffled_assets)

        # Process each asset in shuffled order
        for asset in shuffled_assets:
            info(f"📊 Processing {asset}...")
            await process_asset(asset)

        # Sleep before next scan
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        sleep_secs = 60 - now_utc.second
        if sleep_secs < 5:
            sleep_secs += 60
        info(f"🕒 Sleeping ~{sleep_secs}s before next scan...")
        await asyncio.sleep(sleep_secs)


# =============== Entry ===============
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        warn("\n👋 Exiting gracefully...")

        # Ctrl+C দিলে summary send + exit
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            summ = format_summary()
            ok("\n" + summ + "\n")
            loop.run_until_complete(send_telegram_message_bold(summ))
        except Exception:
            pass
