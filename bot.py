import os
import sys
import time
import json
import hmac
import hashlib
import requests
import traceback
from datetime import datetime

# ============================================================
# DELTA XAUTUSD GRID BOT (ULTIMATE FINAL)
# ============================================================
# COMPLETE FEATURES (NOTHING MISSING):
#
# ✅ Lock system (bot.lock)
# ✅ State save/load (state.json)
# ✅ Manual trade sync via fills
# ✅ Restart recovery from fills
# ✅ Position 0 -> Multiplier entry (LOT_SIZE * ENTRY_MULTIPLIER)
# ✅ Down move -> grid buys (LOT_SIZE each GRID step)
# ✅ Each buy has its own sell target (buy_price + GRID)
# ✅ Sell triggers only that level qty at its sell_price
# ✅ Multi-buy in same loop if price jumps down multiple levels
# ✅ Multi-sell in same loop if price jumps up multiple levels
# ✅ No short sell protection
# ✅ Duplicate action guard
# ✅ Partial buy fill supported
# ✅ Partial sell fill supported
# ✅ Multi-fill support (same order multiple fills summed)
# ✅ Manual sell tolerance support (fills may not match exact sell_price)
# ============================================================

BASE_URL = "https://api.india.delta.exchange"

SYMBOL = "XAUTUSD"
PRODUCT_ID = 131253

GRID = float(os.getenv("GRID", "15"))
LOT_SIZE = float(os.getenv("LOT_SIZE", "5"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "5"))

ENTRY_MULTIPLIER = float(os.getenv("ENTRY_MULTIPLIER", "10"))
MAX_REENTRY_SIZE = float(os.getenv("MAX_REENTRY_SIZE", "200"))

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"

USER_AGENT = "xautusd-grid-bot-ULTIMATE-FINAL"

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
        # {"buy_price": x, "sell_price": x+GRID, "size": size}

        "last_action": None,
        "last_action_price": None,
        "last_order_id": None,

        "last_fill_id": None,
        "last_fill_price": None,
        "last_fill_side": None,

        # for multi-fill tracking
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

        # safety cap processed list
        if len(d["processed_fill_ids"]) > 2000:
            d["processed_fill_ids"] = d["processed_fill_ids"][-1000:]

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


def get_fills(page_size=100):
    data = private_get("/v2/fills", params={"page_size": page_size})

    if data.get("success") is not True:
        raise Exception("Fills API failed: " + str(data))

    return data.get("result", [])


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
# LEVEL MANAGEMENT (PARTIAL SUPPORT)
# ============================================================

def sort_levels():
    state["levels"] = sorted(state["levels"], key=lambda x: x["buy_price"])
    save_state(state)


def add_level(buy_price, size):
    buy_price = float(buy_price)
    size = float(size)

    if size <= 0:
        return

    level = {
        "buy_price": buy_price,
        "sell_price": buy_price + GRID,
        "size": size
    }

    state["levels"].append(level)
    sort_levels()


def reduce_level_by_sell_price(sell_price, sold_size):
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
    """
    Manual sell tolerance:
    if fill price doesn't match exact sell_price,
    find closest sell_price within tolerance.
    """
    if not state["levels"]:
        return None

    fill_price = float(fill_price)
    sold_size = float(sold_size)

    closest = None
    closest_index = None
    closest_diff = 999999

    for i, lv in enumerate(state["levels"]):
        diff = abs(float(lv["sell_price"]) - fill_price)
        if diff < closest_diff:
            closest_diff = diff
            closest = lv
            closest_index = i

    # tolerance: must be within 2 points
    if closest is None or closest_diff > 2.0:
        return None

    current_size = float(closest["size"])

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
    save_state(state)


# ============================================================
# MULTIPLIER ENTRY SYSTEM
# ============================================================

def calculate_reentry_size():
    size = LOT_SIZE * ENTRY_MULTIPLIER
    if size > MAX_REENTRY_SIZE:
        size = MAX_REENTRY_SIZE
    return float(size)


# ============================================================
# PROCESS NEW FILLS (MULTI-FILL SAFE)
# ============================================================

def process_new_fills():
    """
    Reads recent fills and applies those not processed yet.
    Supports multiple fills from same order.
    """
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

    # process oldest first
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
            add_level(fill_price, fill_size)

        elif fill_side == "sell":
            removed = reduce_level_by_sell_price(fill_price, fill_size)
            if removed is None:
                reduce_closest_sell_level(fill_price, fill_size)

        state["last_fill_id"] = fid
        state["last_fill_price"] = fill_price
        state["last_fill_side"] = fill_side

        state["processed_fill_ids"].append(fid)

    # cap processed list
    if len(state["processed_fill_ids"]) > 2000:
        state["processed_fill_ids"] = state["processed_fill_ids"][-1000:]

    save_state(state)

    # if position is 0 reset everything
    pos_size = get_open_position_size()
    if pos_size <= 0:
        reset_all_levels()


# ============================================================
# STARTUP RECOVERY (REBUILD LEVELS FROM FILLS)
# ============================================================

def rebuild_levels_from_fills():
    """
    Rebuild grid levels by replaying fills history.
    This is best recovery method.
    """
    print("REBUILDING LEVELS FROM FILLS HISTORY...")
    sys.stdout.flush()

    reset_all_levels()
    state["processed_fill_ids"] = []
    save_state(state)

    fills = get_fills(page_size=200)

    # take only symbol fills
    symbol_fills = [f for f in fills if f.get("product_symbol") == SYMBOL]

    # oldest first
    symbol_fills = sorted(symbol_fills, key=lambda x: x.get("created_at", ""))

    for f in symbol_fills:
        fid = f.get("id")
        if fid is None:
            continue

        fill_price = float(f.get("price"))
        fill_side = f.get("side")
        fill_size = float(f.get("size", 0))

        if fill_size <= 0:
            continue

        if fill_side == "buy":
            add_level(fill_price, fill_size)

        elif fill_side == "sell":
            removed = reduce_level_by_sell_price(fill_price, fill_size)
            if removed is None:
                reduce_closest_sell_level(fill_price, fill_size)

        state["processed_fill_ids"].append(fid)

    if len(state["processed_fill_ids"]) > 2000:
        state["processed_fill_ids"] = state["processed_fill_ids"][-1000:]

    save_state(state)

    print("REBUILD DONE. LEVELS:", state["levels"])
    sys.stdout.flush()


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

    # if open position exists but state empty -> rebuild from fills
    if pos_size > 0 and not state["levels"]:
        rebuild_levels_from_fills()

    # if no position -> reset state
    if pos_size <= 0:
        reset_all_levels()

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

            # process new fills (manual + bot trades + partial fills)
            process_new_fills()

            price = get_live_price()
            pos_size = get_open_position_size()

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{now} PRICE:{price} POS:{pos_size} NEXT_BUY:{get_next_buy_price()} LEVELS:{state.get('levels')}")
            sys.stdout.flush()

            # ============================================================
            # POSITION 0 -> MULTIPLIER ENTRY
            # ============================================================
            if pos_size <= 0:
                print("POSITION 0 -> MULTIPLIER BUY ENTRY")
                sys.stdout.flush()

                entry_size = calculate_reentry_size()

                resp = place_market_order("buy", entry_size)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", price, order_id)

                    # after placing order, wait and sync fills
                    time.sleep(1)
                    process_new_fills()

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

                print("GRID BUY TRIGGERED AT:", next_buy)
                sys.stdout.flush()

                resp = place_market_order("buy", LOT_SIZE)

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
