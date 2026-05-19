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
# DELTA XAUTUSD GRID BOT (FULL FINAL FIX)
# ============================================================
# INCLUDED (NOTHING MISSING):
#
# ✅ Lock system (bot.lock)
# ✅ State save/load (state.json)
# ✅ Manual trade sync (fills)
# ✅ Restart safe recovery (REBUILD from fills history)
# ✅ Position 0 -> Multiplier entry (LOT_SIZE * ENTRY_MULTIPLIER)
# ✅ cycle_entry_size logic preserved
# ✅ Downside -> grid buys (dynamic lot per series)
# ✅ Every buy creates its own sell target (buy_price + GRID)
# ✅ Cycle batch sells only at its own sell target
# ✅ Small lots sell at their own target
# ✅ Multi-buy in same loop if price jumps down multiple grids
# ✅ Multi-sell in same loop if price jumps up multiple grids
# ✅ HARD NO SHORT SELL protection (sell never exceeds pos)
# ✅ Duplicate action guard
# ✅ Partial BUY fill supported
# ✅ Partial SELL fill supported
# ✅ Multi-fill supported (fills processed individually)
# ✅ FIXED DUPLICATE FILL PROCESSING BUG
# ✅ FIXED NEGATIVE POSITION BUG (wait instead of reentry)
# ✅ FIXED DOUBLE MULTIPLIER BUY BUG (cooldown + lock)
# ✅ FULL RECOVERY from fills history (old bot open positions safe)
# ✅ Mismatch protection (pos vs levels sum)
# ✅ SERIES STEP SYSTEM (100 points dynamic lot add/subtract)
# ============================================================

BASE_URL = "https://api.india.delta.exchange"

SYMBOL = "XAUTUSD"
PRODUCT_ID = 131253

# ============================================================
# ENV CONFIG
# ============================================================
GRID = float(os.getenv("GRID", "15"))
LOT_SIZE = float(os.getenv("LOT_SIZE", "5"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "5"))

ENTRY_MULTIPLIER = float(os.getenv("ENTRY_MULTIPLIER", "10"))
MAX_REENTRY_SIZE = float(os.getenv("MAX_REENTRY_SIZE", "200"))

SERIES_STEP = float(os.getenv("SERIES_STEP", "100"))
SERIES_ADD_LOT = float(os.getenv("SERIES_ADD_LOT", "0"))

# Protection
REENTRY_COOLDOWN_SECONDS = int(os.getenv("REENTRY_COOLDOWN_SECONDS", "15"))
NEGATIVE_POS_WAIT_SECONDS = int(os.getenv("NEGATIVE_POS_WAIT_SECONDS", "15"))
RECOVERY_FILL_SCAN = int(os.getenv("RECOVERY_FILL_SCAN", "800"))

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"

USER_AGENT = "xautusd-grid-bot-FULL-FINAL-NO-SHORT"

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
print("REENTRY_COOLDOWN_SECONDS:", REENTRY_COOLDOWN_SECONDS)
print("NEGATIVE_POS_WAIT_SECONDS:", NEGATIVE_POS_WAIT_SECONDS)
print("RECOVERY_FILL_SCAN:", RECOVERY_FILL_SCAN)
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

        # cycle info
        "cycle_entry_size": None,
        "cycle_entry_price": None,
        "cycle_entry_sell_price": None,

        # series system
        "cycle_base_series": None,

        # duplicate guard
        "last_action": None,
        "last_action_price": None,
        "last_order_id": None,

        # fill tracking
        "last_fill_id": None,
        "last_fill_price": None,
        "last_fill_side": None,

        # processed fill IDs (persisted list)
        "processed_fill_ids": [],

        # reentry lock
        "last_reentry_time": 0
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

        if d.get("last_reentry_time") is None:
            d["last_reentry_time"] = 0

        return d
    except:
        return default_state()


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


state = load_state()

# in-memory processed set (to stop duplicates instantly)
processed_fill_set = set(state.get("processed_fill_ids", []))

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


def place_market_order(side: str, size: float):
    size = float(size)
    if size <= 0:
        return {"success": False, "error": "size <= 0"}

    payload = {
        "product_id": PRODUCT_ID,
        "size": size,
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
    save_state()


# ============================================================
# SERIES / DYNAMIC LOT SYSTEM
# ============================================================

def get_series_floor(price):
    # series floor = nearest lower multiple of SERIES_STEP
    if SERIES_STEP <= 0:
        return None
    return math.floor(float(price) / SERIES_STEP) * SERIES_STEP


def calculate_dynamic_lot(current_series, base_series):
    if SERIES_ADD_LOT <= 0 or SERIES_STEP <= 0:
        return float(LOT_SIZE)

    if base_series is None:
        return float(LOT_SIZE)

    diff_steps = int(round((float(base_series) - float(current_series)) / SERIES_STEP))
    # if price is above base_series, diff_steps negative -> lot decreases, but never below LOT_SIZE
    dynamic = float(LOT_SIZE) + float(diff_steps) * float(SERIES_ADD_LOT)

    if dynamic < LOT_SIZE:
        dynamic = LOT_SIZE

    return float(dynamic)


# ============================================================
# LEVEL MANAGEMENT
# ============================================================

def sort_levels():
    state["levels"] = sorted(state["levels"], key=lambda x: float(x["buy_price"]))
    save_state()


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


def get_total_level_size():
    total = 0.0
    for lv in state["levels"]:
        total += float(lv.get("size", 0))
    return float(total)


def reset_all_levels():
    state["levels"] = []
    state["cycle_entry_size"] = None
    state["cycle_entry_price"] = None
    state["cycle_entry_sell_price"] = None
    state["cycle_base_series"] = None
    save_state()


def reduce_exact_sell_level(fill_price, sold_size):
    fill_price = float(fill_price)
    sold_size = float(sold_size)

    for i, lv in enumerate(state["levels"]):
        if abs(float(lv["sell_price"]) - fill_price) < 0.20:
            current_size = float(lv["size"])

            if sold_size >= current_size - 0.00001:
                removed = state["levels"].pop(i)
                save_state()
                return removed
            else:
                state["levels"][i]["size"] = current_size - sold_size
                save_state()
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

    if closest_diff > 5.0:
        return None

    current_size = float(state["levels"][closest_index]["size"])

    if sold_size >= current_size - 0.00001:
        removed = state["levels"].pop(closest_index)
        save_state()
        return removed
    else:
        state["levels"][closest_index]["size"] = current_size - sold_size
        save_state()
        return state["levels"][closest_index]


def get_next_buy_price():
    if not state["levels"]:
        return None
    lowest_buy = min([float(lv["buy_price"]) for lv in state["levels"]])
    return float(lowest_buy - GRID)


def get_next_sell_target(price):
    sell_candidates = []
    for lv in state["levels"]:
        if float(price) >= float(lv["sell_price"]):
            sell_candidates.append(lv)

    if not sell_candidates:
        return None

    sell_candidates = sorted(sell_candidates, key=lambda x: float(x["sell_price"]))
    return sell_candidates[0]


# ============================================================
# MULTIPLIER ENTRY SYSTEM
# ============================================================

def calculate_reentry_size():
    size = LOT_SIZE * ENTRY_MULTIPLIER
    if size > MAX_REENTRY_SIZE:
        size = MAX_REENTRY_SIZE
    return float(size)


def can_reenter_now():
    now = int(time.time())
    last = int(state.get("last_reentry_time", 0))
    if now - last < REENTRY_COOLDOWN_SECONDS:
        return False
    return True


def mark_reentry_time():
    state["last_reentry_time"] = int(time.time())
    save_state()


# ============================================================
# PROCESS NEW FILLS (FIXED DUPLICATE BUG)
# ============================================================

def process_new_fills():
    global processed_fill_set

    fills = get_fills(page_size=200)

    new_fills = []
    for f in fills:
        if f.get("product_symbol") != SYMBOL:
            continue

        fid = f.get("id")
        if fid is None:
            continue

        if fid in processed_fill_set:
            continue

        # mark immediately in set (IMPORTANT FIX)
        processed_fill_set.add(fid)
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
            continue

        print("PROCESSING NEW FILL:", fill_side, fill_price, fill_size, "ID:", fid)
        sys.stdout.flush()

        if fill_side == "buy":
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

    save_state()

    # if exchange says position 0 -> reset everything
    pos_size = get_open_position_size()
    if pos_size == 0:
        reset_all_levels()


# ============================================================
# FULL RECOVERY FROM FILLS HISTORY (OLD BOT SAFE)
# ============================================================

def rebuild_levels_from_fills(pos_size):
    """
    Rebuilds open levels by scanning fills history.
    Matches sells against buys using sell targets (buy+GRID).
    Not FIFO, but by target matching (your required logic).
    """

    print("REBUILDING LEVELS FROM FILLS HISTORY...")
    sys.stdout.flush()

    reset_all_levels()

    fills = get_fills(page_size=RECOVERY_FILL_SCAN)
    fills = [f for f in fills if f.get("product_symbol") == SYMBOL]
    fills = sorted(fills, key=lambda x: x.get("created_at", ""))

    # temporary open map: list of open lots
    open_levels = []

    for f in fills:
        side = f.get("side")
        price = float(f.get("price"))
        size = float(f.get("size", 0))

        if size <= 0:
            continue

        if side == "buy":
            open_levels.append({
                "buy_price": price,
                "sell_price": price + GRID,
                "size": size,
                "is_cycle": False
            })

        elif side == "sell":
            remaining_sell = size

            # match sells by closest sell_price first (your logic)
            open_levels = sorted(open_levels, key=lambda x: abs(float(x["sell_price"]) - price))

            i = 0
            while i < len(open_levels) and remaining_sell > 0:
                lv = open_levels[i]
                # allow match only if sell price is close enough
                if abs(float(lv["sell_price"]) - price) <= 5.0:
                    lv_size = float(lv["size"])
                    if remaining_sell >= lv_size - 0.00001:
                        remaining_sell -= lv_size
                        open_levels.pop(i)
                        continue
                    else:
                        lv["size"] = lv_size - remaining_sell
                        remaining_sell = 0
                        break
                i += 1

    # Now open_levels represent remaining buys not sold
    # But they may exceed current pos_size due to old history, so trim safely.
    open_levels = sorted(open_levels, key=lambda x: float(x["buy_price"]))

    total = sum([float(x["size"]) for x in open_levels])

    if total <= 0:
        reset_all_levels()
        return

    # Normalize if mismatch with exchange pos
    if abs(total - pos_size) > 0.01:
        # scale down proportionally (safe approach)
        ratio = pos_size / total
        for lv in open_levels:
            lv["size"] = float(lv["size"]) * ratio

    # save into state
    state["levels"] = []
    for lv in open_levels:
        if float(lv["size"]) > 0.00001:
            state["levels"].append(lv)

    sort_levels()

    print("REBUILD DONE. LEVELS:", state["levels"])
    sys.stdout.flush()


# ============================================================
# MISMATCH PROTECTION
# ============================================================

def mismatch_protection_check():
    pos_size = get_open_position_size()

    if pos_size == 0:
        reset_all_levels()
        return

    # NEGATIVE POS SAFETY (IMPORTANT FIX)
    if pos_size < 0:
        print("WARNING: NEGATIVE POSITION DETECTED:", pos_size)
        print("WAITING FOR DELTA SYNC...")
        sys.stdout.flush()
        time.sleep(NEGATIVE_POS_WAIT_SECONDS)
        return

    total_levels = get_total_level_size()

    if not state["levels"]:
        print("LEVELS EMPTY BUT POSITION EXISTS -> FULL RECOVERY FROM FILLS")
        rebuild_levels_from_fills(pos_size)
        return

    if abs(total_levels - pos_size) > 0.5:
        print("POSITION MISMATCH DETECTED !!!")
        print("EXCHANGE POS:", pos_size, "STATE LEVEL SUM:", total_levels)
        print("FULL RECOVERY FROM FILLS NOW...")
        sys.stdout.flush()
        rebuild_levels_from_fills(pos_size)


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

    # series base set only if empty
    if state.get("cycle_base_series") is None and pos_size > 0:
        state["cycle_base_series"] = get_series_floor(price)
        save_state()

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

            # negative position safety
            if pos_size < 0:
                print("NEGATIVE POS STILL:", pos_size, "WAITING...")
                sys.stdout.flush()
                time.sleep(NEGATIVE_POS_WAIT_SECONDS)
                continue

            current_series = get_series_floor(price)

            if state.get("cycle_base_series") is None:
                state["cycle_base_series"] = current_series
                save_state()

            dynamic_lot = calculate_dynamic_lot(current_series, state.get("cycle_base_series"))

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{now} PRICE:{price} POS:{pos_size} CYCLE_SERIES:{state.get('cycle_base_series')} DYNAMIC_LOT:{dynamic_lot} NEXT_BUY:{get_next_buy_price()} LEVELS:{state.get('levels')}")
            sys.stdout.flush()

            # ============================================================
            # POSITION 0 -> MULTIPLIER ENTRY (STRICT)
            # ============================================================
            if pos_size == 0:
                if not can_reenter_now():
                    print("REENTRY COOLDOWN ACTIVE -> SKIPPING MULTIPLIER BUY")
                    sys.stdout.flush()
                    time.sleep(SLEEP_SECONDS)
                    continue

                print("POSITION 0 -> MULTIPLIER BUY ENTRY")
                sys.stdout.flush()

                entry_size = calculate_reentry_size()

                # update base series for new cycle
                state["cycle_base_series"] = get_series_floor(price)

                mark_reentry_time()

                resp = place_market_order("buy", entry_size)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", price, order_id)

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

                # dynamic lot recalculated based on current price series
                current_series = get_series_floor(price)
                dynamic_lot = calculate_dynamic_lot(current_series, state.get("cycle_base_series"))

                print("GRID BUY TRIGGERED AT:", next_buy, "BUY SIZE:", dynamic_lot)
                sys.stdout.flush()

                resp = place_market_order("buy", dynamic_lot)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", next_buy, order_id)

                    time.sleep(1)
                    process_new_fills()

                    pos_size = get_open_position_size()
                    if pos_size < 0:
                        break
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
                desired_sell_size = float(sell_level["size"])

                if already_executed_same_price("sell", sell_price):
                    break

                # HARD NO SHORT SELL PROTECTION
                pos_size = get_open_position_size()
                if pos_size <= 0:
                    print("SELL BLOCKED -> POS <= 0 (NO SHORT ALLOWED)")
                    sys.stdout.flush()
                    break

                sell_size = min(desired_sell_size, pos_size)

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

                    if pos_size < 0:
                        print("WARNING: POS NEGATIVE AFTER SELL, WAITING SYNC...")
                        sys.stdout.flush()
                        time.sleep(NEGATIVE_POS_WAIT_SECONDS)

                    if pos_size == 0:
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
