import os
import sys
import time
import json
import hmac
import hashlib
import requests
import traceback
import math
from datetime import datetime

# ============================================================
# DELTA XAUTUSD GRID BOT (FINAL ULTIMATE)
# SERIES BASED LOT ADDER + MULTIPLIER CYCLE + PARTIAL FILL SAFE
# ============================================================

BASE_URL = "https://api.india.delta.exchange"

SYMBOL = "XAUTUSD"
PRODUCT_ID = 131253

GRID = float(os.getenv("GRID", "15"))
LOT_SIZE = float(os.getenv("LOT_SIZE", "5"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "5"))

ENTRY_MULTIPLIER = float(os.getenv("ENTRY_MULTIPLIER", "10"))
MAX_REENTRY_SIZE = float(os.getenv("MAX_REENTRY_SIZE", "200"))

# NEW SERIES CONFIG
SERIES_STEP = float(os.getenv("SERIES_STEP", "100"))
SERIES_ADD_LOT = float(os.getenv("SERIES_ADD_LOT", "0"))

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"

USER_AGENT = "xautusd-grid-bot-FINAL-ULTIMATE-SERIES"

print("BOT FILE RUNNING...")
sys.stdout.flush()

print("BOT STARTED...")
print("SYMBOL:", SYMBOL)
print("PRODUCT_ID:", PRODUCT_ID)
print("GRID:", GRID)
print("LOT_SIZE:", LOT_SIZE)
print("SLEEP_SECONDS:", SLEEP_SECONDS)
print("ENTRY_MULTIPLIER:", ENTRY_MULTIPLIER)
print("MAX_REENTRY_SIZE:", MAX_REENTRY_SIZE)
print("SERIES_STEP:", SERIES_STEP)
print("SERIES_ADD_LOT:", SERIES_ADD_LOT)
sys.stdout.flush()

if not API_KEY or not API_SECRET:
    raise Exception("DELTA_API_KEY / DELTA_API_SECRET missing!")

# ============================================================
# LOCK SYSTEM
# ============================================================

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        print("LOCK FILE EXISTS -> BOT ALREADY RUNNING. EXITING.")
        sys.stdout.flush()
        sys.exit(0)

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    print("LOCK ACQUIRED.")
    sys.stdout.flush()


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except:
        pass


acquire_lock()

# ============================================================
# STATE SYSTEM
# ============================================================

def default_state():
    return {
        "levels": [],

        # cycle multiplier entry tracking
        "cycle_entry_size": None,
        "cycle_entry_price": None,
        "cycle_entry_sell_price": None,

        # cycle base series tracking (dynamic)
        "cycle_base_series": None,

        "last_action": None,
        "last_action_price": None,
        "last_order_id": None,

        "last_fill_id": None,
        "last_fill_price": None,
        "last_fill_side": None,

        "processed_fill_ids": []
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        d = default_state()
        d.update(data)

        if d.get("levels") is None:
            d["levels"] = []

        if d.get("processed_fill_ids") is None:
            d["processed_fill_ids"] = []

        if len(d["processed_fill_ids"]) > 5000:
            d["processed_fill_ids"] = d["processed_fill_ids"][-2000:]

        return d
    except:
        return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


state = load_state()

# ============================================================
# SIGNATURE HELPERS
# ============================================================

def generate_signature(message: str) -> str:
    return hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()


def safe_json_response(r):
    try:
        return r.json()
    except:
        return None


def private_get(endpoint: str, params=None):
    if params is None:
        params = {}

    query_string = ""
    if params:
        query_string = "?" + "&".join([f"{k}={v}" for k, v in params.items()])

    full_endpoint = endpoint + query_string
    url = BASE_URL + full_endpoint

    timestamp = str(int(time.time()))
    signature_data = "GET" + timestamp + full_endpoint
    signature = generate_signature(signature_data)

    headers = {
        "Accept": "application/json",
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.get(url, headers=headers, timeout=20)
    data = safe_json_response(r)

    if data is None:
        raise Exception("PRIVATE GET JSON ERROR: " + r.text)

    return data


def private_post(endpoint: str, payload: dict):
    url = BASE_URL + endpoint
    timestamp = str(int(time.time()))
    body = json.dumps(payload)

    signature_data = "POST" + timestamp + endpoint + body
    signature = generate_signature(signature_data)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.post(url, headers=headers, data=body, timeout=20)
    data = safe_json_response(r)

    if data is None:
        raise Exception("PRIVATE POST JSON ERROR: " + r.text)

    return data


# ============================================================
# EXCHANGE HELPERS
# ============================================================

def get_live_price():
    url = f"{BASE_URL}/v2/tickers/{SYMBOL}"
    r = requests.get(url, timeout=10)
    data = safe_json_response(r)

    if data is None:
        raise Exception("Ticker JSON error: " + r.text)

    if data.get("success") is not True:
        raise Exception("Ticker API failed: " + str(data))

    return float(data["result"]["close"])


def get_open_position_size():
    data = private_get("/v2/positions/margined")

    if data.get("success") is not True:
        raise Exception("Positions API failed: " + str(data))

    for p in data.get("result", []):
        if p.get("product_symbol") == SYMBOL:
            return float(p.get("size", 0))

    return 0.0


def get_fills(page_size=200):
    data = private_get("/v2/fills", params={"page_size": page_size})

    if data.get("success") is not True:
        raise Exception("Fills API failed: " + str(data))

    return data.get("result", [])


def get_last_buy_fill():
    fills = get_fills(page_size=200)
    for f in fills:
        if f.get("product_symbol") == SYMBOL and f.get("side") == "buy":
            return f
    return None


def place_market_order(side: str, size: float):
    payload = {
        "product_id": PRODUCT_ID,
        "size": float(size),
        "side": side,
        "order_type": "market_order"
    }

    res = private_post("/v2/orders", payload)
    print("ORDER RESPONSE:", res)
    sys.stdout.flush()
    return res


# ============================================================
# DUPLICATE ACTION GUARD
# ============================================================

def already_executed_same_price(action, price):
    if state.get("last_action") == action and state.get("last_action_price") is not None:
        if abs(float(state["last_action_price"]) - float(price)) < 0.01:
            return True
    return False


def mark_action(action, price, order_id=None):
    state["last_action"] = action
    state["last_action_price"] = float(price)
    state["last_order_id"] = order_id
    save_state(state)


# ============================================================
# SERIES LOT SIZE CALCULATION (DYNAMIC, NOT FIXED)
# ============================================================

def get_series_base(price):
    """
    SERIES_STEP=100
    5200 -> 5200
    5185 -> 5100
    5005 -> 5000
    4999 -> 4900
    """
    if SERIES_STEP <= 0:
        return None
    return math.floor(float(price) / SERIES_STEP) * SERIES_STEP


def calculate_dynamic_lot(price):
    """
    LOT_SIZE changes only when price goes BELOW cycle_base_series.
    If price goes ABOVE cycle series, it stays base LOT_SIZE.
    """
    base_lot = float(LOT_SIZE)

    if SERIES_ADD_LOT <= 0:
        return base_lot

    if SERIES_STEP <= 0:
        return base_lot

    cycle_series = state.get("cycle_base_series")
    if cycle_series is None:
        return base_lot

    current_series = get_series_base(price)
    if current_series is None:
        return base_lot

    # steps down from cycle series
    steps_down = int((cycle_series - current_series) / SERIES_STEP)

    # if price above cycle series => no add lot
    if steps_down < 0:
        steps_down = 0

    dynamic_lot = base_lot + (steps_down * float(SERIES_ADD_LOT))

    if dynamic_lot < base_lot:
        dynamic_lot = base_lot

    return float(dynamic_lot)


# ============================================================
# LEVEL MANAGEMENT
# ============================================================

def sort_levels():
    state["levels"] = sorted(state["levels"], key=lambda x: x["buy_price"])
    save_state(state)


def add_level(buy_price, size, is_cycle=False):
    buy_price = float(buy_price)
    size = float(size)

    if size <= 0:
        return

    level = {
        "buy_price": buy_price,
        "sell_price": buy_price + GRID,
        "size": size,
        "is_cycle": bool(is_cycle)
    }

    state["levels"].append(level)
    sort_levels()


def reduce_exact_sell_level(sell_price, sold_size):
    sell_price = float(sell_price)
    sold_size = float(sold_size)

    for i, lv in enumerate(state["levels"]):
        if abs(float(lv["sell_price"]) - sell_price) < 0.05:
            current_size = float(lv["size"])

            if sold_size >= current_size:
                removed = state["levels"].pop(i)
                save_state(state)
                return removed
            else:
                state["levels"][i]["size"] = current_size - sold_size
                save_state(state)
                return state["levels"][i]

    return None


def reduce_closest_sell_level(fill_price, sold_size):
    if not state["levels"]:
        return None

    fill_price = float(fill_price)
    sold_size = float(sold_size)

    closest_index = None
    closest_diff = 999999

    for i, lv in enumerate(state["levels"]):
        diff = abs(float(lv["sell_price"]) - fill_price)
        if diff < closest_diff:
            closest_diff = diff
            closest_index = i

    if closest_index is None:
        return None

    # tolerance to handle slippage / manual sell fill mismatch
    if closest_diff > 3.0:
        return None

    current_size = float(state["levels"][closest_index]["size"])

    if sold_size >= current_size:
        removed = state["levels"].pop(closest_index)
        save_state(state)
        return removed
    else:
        state["levels"][closest_index]["size"] = current_size - sold_size
        save_state(state)
        return state["levels"][closest_index]


def get_next_buy_price():
    if not state["levels"]:
        return None

    lowest_buy = min([lv["buy_price"] for lv in state["levels"]])
    return float(lowest_buy - GRID)


def get_next_sell_target(price):
    sell_candidates = []
    for lv in state["levels"]:
        if float(price) >= float(lv["sell_price"]):
            sell_candidates.append(lv)

    if not sell_candidates:
        return None

    sell_candidates = sorted(sell_candidates, key=lambda x: x["sell_price"])
    return sell_candidates[0]


def reset_all_levels():
    state["levels"] = []

    state["cycle_entry_size"] = None
    state["cycle_entry_price"] = None
    state["cycle_entry_sell_price"] = None
    state["cycle_base_series"] = None

    save_state(state)


def get_total_level_size():
    total = 0.0
    for lv in state["levels"]:
        total += float(lv.get("size", 0))
    return float(total)


# ============================================================
# MULTIPLIER ENTRY SYSTEM
# ============================================================

def calculate_reentry_size():
    size = LOT_SIZE * ENTRY_MULTIPLIER
    if size > MAX_REENTRY_SIZE:
        size = MAX_REENTRY_SIZE
    return float(size)


# ============================================================
# PROCESS NEW FILLS (MANUAL + PARTIAL + MULTI-FILL)
# ============================================================

def process_new_fills():
    fills = get_fills(page_size=200)

    new_fills = []
    for f in fills:
        if f.get("product_symbol") != SYMBOL:
            continue

        fid = f.get("id")
        if fid is None:
            continue

        if fid in state["processed_fill_ids"]:
            continue

        new_fills.append(f)

    if not new_fills:
        return

    new_fills = sorted(new_fills, key=lambda x: x.get("created_at", ""))

    for f in new_fills:
        fid = f.get("id")
        fill_price = float(f.get("price"))
        fill_side = f.get("side")
        fill_size = float(f.get("size", 0))

        if fill_size <= 0:
            state["processed_fill_ids"].append(fid)
            continue

        print("PROCESSING NEW FILL:", fill_side, fill_price, fill_size, "ID:", fid)
        sys.stdout.flush()

        if fill_side == "buy":
            if state.get("cycle_entry_size") is not None and abs(fill_size - state["cycle_entry_size"]) < 0.01:
                add_level(fill_price, fill_size, is_cycle=True)
            else:
                add_level(fill_price, fill_size, is_cycle=False)

        elif fill_side == "sell":
            removed = reduce_exact_sell_level(fill_price, fill_size)
            if removed is None:
                reduce_closest_sell_level(fill_price, fill_size)

        state["last_fill_id"] = fid
        state["last_fill_price"] = fill_price
        state["last_fill_side"] = fill_side

        state["processed_fill_ids"].append(fid)

    if len(state["processed_fill_ids"]) > 5000:
        state["processed_fill_ids"] = state["processed_fill_ids"][-2000:]

    save_state(state)

    pos_size = get_open_position_size()
    if pos_size <= 0:
        reset_all_levels()


# ============================================================
# SAFE RECOVERY / MISMATCH AUTO FIX
# ============================================================

def safe_recover_from_last_buy(pos_size):
    last_buy = get_last_buy_fill()

    if last_buy is None:
        print("SAFE RECOVERY FAILED: NO LAST BUY FILL FOUND -> RESETTING")
        reset_all_levels()
        return

    buy_price = float(last_buy["price"])

    print("SAFE RECOVERY -> LAST BUY PRICE:", buy_price, "POS SIZE:", pos_size)
    sys.stdout.flush()

    reset_all_levels()

    state["cycle_entry_size"] = pos_size
    state["cycle_entry_price"] = buy_price
    state["cycle_entry_sell_price"] = buy_price + GRID
    state["cycle_base_series"] = get_series_base(buy_price)
    save_state(state)

    add_level(buy_price, pos_size, is_cycle=True)


def mismatch_protection_check():
    pos_size = get_open_position_size()

    if pos_size <= 0:
        reset_all_levels()
        return

    total_levels = get_total_level_size()

    if not state["levels"]:
        print("LEVELS EMPTY BUT POSITION EXISTS -> RECOVERING")
        safe_recover_from_last_buy(pos_size)
        return

    if abs(total_levels - pos_size) > 0.01:
        print("POSITION MISMATCH DETECTED !!!")
        print("EXCHANGE POS:", pos_size, "STATE LEVEL SUM:", total_levels)
        print("AUTO RECOVERING NOW...")
        sys.stdout.flush()

        safe_recover_from_last_buy(pos_size)

    # if cycle_base_series missing but position exists -> recover it
    if state.get("cycle_base_series") is None:
        print("CYCLE BASE SERIES MISSING -> FIXING FROM LAST BUY")
        sys.stdout.flush()
        last_buy = get_last_buy_fill()
        if last_buy:
            state["cycle_base_series"] = get_series_base(float(last_buy["price"]))
            save_state(state)


# ============================================================
# STARTUP
# ============================================================

print("STATE LOADED:", state)
sys.stdout.flush()

try:
    price = get_live_price()
    pos_size = get_open_position_size()

    print("STARTUP LIVE PRICE:", price)
    print("STARTUP POS SIZE:", pos_size)
    sys.stdout.flush()

    mismatch_protection_check()

except Exception as e:
    print("STARTUP ERROR:", str(e))
    traceback.print_exc()
    sys.stdout.flush()


# ============================================================
# MAIN LOOP
# ============================================================

try:
    while True:
        try:
            LOT_SIZE = float(os.getenv("LOT_SIZE", "5"))
            GRID = float(os.getenv("GRID", "15"))
            ENTRY_MULTIPLIER = float(os.getenv("ENTRY_MULTIPLIER", "10"))
            MAX_REENTRY_SIZE = float(os.getenv("MAX_REENTRY_SIZE", "200"))
            SERIES_STEP = float(os.getenv("SERIES_STEP", "100"))
            SERIES_ADD_LOT = float(os.getenv("SERIES_ADD_LOT", "0"))

            process_new_fills()
            mismatch_protection_check()

            price = get_live_price()
            pos_size = get_open_position_size()

            dynamic_lot = calculate_dynamic_lot(price)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{now} PRICE:{price} POS:{pos_size} CYCLE_SERIES:{state.get('cycle_base_series')} DYNAMIC_LOT:{dynamic_lot} NEXT_BUY:{get_next_buy_price()} LEVELS:{state.get('levels')}")
            sys.stdout.flush()

            # ============================================================
            # POSITION 0 -> MULTIPLIER ENTRY
            # ============================================================
            if pos_size <= 0:
                print("POSITION 0 -> MULTIPLIER BUY ENTRY")
                sys.stdout.flush()

                entry_size = calculate_reentry_size()
                state["cycle_entry_size"] = entry_size
                save_state(state)

                resp = place_market_order("buy", entry_size)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", price, order_id)

                    time.sleep(1)
                    process_new_fills()

                    last_buy = get_last_buy_fill()
                    if last_buy:
                        fill_price = float(last_buy["price"])
                    else:
                        fill_price = price

                    state["cycle_entry_price"] = fill_price
                    state["cycle_entry_sell_price"] = fill_price + GRID
                    state["cycle_base_series"] = get_series_base(fill_price)
                    save_state(state)

                time.sleep(SLEEP_SECONDS)
                continue

            # ============================================================
            # MULTI BUY LOOP (PRICE JUMP DOWN)
            # ============================================================
            while True:
                next_buy = get_next_buy_price()
                if next_buy is None:
                    break

                if price > next_buy:
                    break

                if already_executed_same_price("buy", next_buy):
                    break

                dynamic_lot = calculate_dynamic_lot(next_buy)

                print("GRID BUY TRIGGERED AT:", next_buy, "BUY SIZE:", dynamic_lot)
                sys.stdout.flush()

                resp = place_market_order("buy", dynamic_lot)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", next_buy, order_id)

                    time.sleep(1)
                    process_new_fills()

                    pos_size = get_open_position_size()
                else:
                    break

            # ============================================================
            # MULTI SELL LOOP (PRICE JUMP UP)
            # ============================================================
            while True:
                sell_level = get_next_sell_target(price)
                if sell_level is None:
                    break

                sell_price = float(sell_level["sell_price"])
                sell_size = float(sell_level["size"])

                if already_executed_same_price("sell", sell_price):
                    break

                sell_size = min(sell_size, pos_size)

                if sell_size <= 0:
                    break

                print("GRID SELL TRIGGERED AT:", sell_price, "SELL SIZE:", sell_size)
                sys.stdout.flush()

                resp = place_market_order("sell", sell_size)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("sell", sell_price, order_id)

                    time.sleep(1)
                    process_new_fills()

                    pos_size = get_open_position_size()

                    if pos_size <= 0:
                        reset_all_levels()
                        break
                else:
                    break

        except Exception as e:
            print("RUNTIME ERROR:", str(e))
            traceback.print_exc()
            sys.stdout.flush()

        time.sleep(SLEEP_SECONDS)

except KeyboardInterrupt:
    print("BOT STOPPED MANUALLY.")
    sys.stdout.flush()

finally:
    release_lock()
