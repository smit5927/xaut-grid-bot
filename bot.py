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
# DELTA XAUTUSD GRID BOT (FULL FINAL FIX - NO LOGIC MISSING)
# ============================================================
# INCLUDED (NOTHING MISSING):
#
# ✅ Lock system (bot.lock)
# ✅ State save/load (state.json)
# ✅ Manual trade sync (fills)
# ✅ Manual BUY split logic:
#       Example: manual 75 buy => 50 cycle + 25 normal
#       manual 120 buy => 50 cycle + 70 normal
#       manual 30 buy => 30 normal
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
# ✅ FIXED FRACTIONAL LEVELS BUG (NO PROPORTIONAL SCALING)
# ✅ FIXED MANUAL BUY FILL SPLIT INSIDE process_new_fills()
# ✅ FIXED REBUILD CYCLE FLAG RESTORE
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
PENDING_ORDER_WAIT_SECONDS = int(os.getenv("PENDING_ORDER_WAIT_SECONDS", "60"))

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"
STATE_SCHEMA_VERSION = 3

USER_AGENT = "xautusd-grid-bot-FULL-FINAL-NO-SHORT-MANUALSAFE"

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
print("PENDING_ORDER_WAIT_SECONDS:", PENDING_ORDER_WAIT_SECONDS)
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

state_needs_fill_bootstrap = False


def default_state():
    return {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "levels": [],
        "pending_buybacks": [],
        "pending_orders": {},

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
        "last_grid_buy_price": None,

        # processed fill IDs (persisted list)
        "processed_fill_ids": [],

        # reentry lock
        "last_reentry_time": 0,

        # duplicate grid protection
        "pending_grid_prices": {}
    }


def load_state():
    global state_needs_fill_bootstrap

    if not os.path.exists(STATE_FILE):
        state_needs_fill_bootstrap = True
        return default_state()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        raw_version = int(data.get("state_schema_version", 1) or 1)
        if raw_version < STATE_SCHEMA_VERSION:
            state_needs_fill_bootstrap = True

        d = default_state()
        d.update(data)
        d["state_schema_version"] = STATE_SCHEMA_VERSION

        if d.get("levels") is None:
            d["levels"] = []

        if d.get("pending_buybacks") is None:
            d["pending_buybacks"] = []

        if d.get("pending_orders") is None or not isinstance(d.get("pending_orders"), dict):
            d["pending_orders"] = {}

        if d.get("processed_fill_ids") is None:
            d["processed_fill_ids"] = []
        elif not d.get("processed_fill_ids"):
            state_needs_fill_bootstrap = True

        # FIX: remove duplicates safely
        unique_ids = []
        seen = set()
        for x in d["processed_fill_ids"]:
            if x not in seen:
                unique_ids.append(x)
                seen.add(x)

        d["processed_fill_ids"] = unique_ids

        if len(d["processed_fill_ids"]) > 5000:
            d["processed_fill_ids"] = d["processed_fill_ids"][-2000:]

        if d.get("last_reentry_time") is None:
            d["last_reentry_time"] = 0

        if d.get("pending_grid_prices") is None:
            d["pending_grid_prices"] = {}

        if d.get("last_grid_buy_price") is not None:
            d["last_grid_buy_price"] = float(d["last_grid_buy_price"])

        # v2 had unsafe historical buyback recovery. Never carry that backlog forward.
        if raw_version < STATE_SCHEMA_VERSION:
            d["pending_buybacks"] = []
            d["pending_orders"] = {}

        clean_buybacks = []
        for b in d.get("pending_buybacks", []):
            try:
                size = float(b.get("size", 0))
                if size <= 0:
                    continue
                clean_buybacks.append({
                    "id": str(b.get("id") or f"legacy-{len(clean_buybacks) + 1}"),
                    "buy_price": float(b.get("buy_price")),
                    "size": size,
                    "is_cycle": bool(b.get("is_cycle", False)),
                    "source_sell_price": b.get("source_sell_price"),
                    "source_fill_price": b.get("source_fill_price"),
                    "source_fill_id": b.get("source_fill_id")
                })
            except:
                continue

        d["pending_buybacks"] = sorted(clean_buybacks, key=lambda x: float(x["buy_price"]), reverse=True)

        return d
    except:
        state_needs_fill_bootstrap = True
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


def get_fill_order_id(fill):
    for key in ("order_id", "order_id_str", "orderId", "orderID"):
        value = fill.get(key)
        if value is not None:
            return str(value)

    order_data = fill.get("order")
    if isinstance(order_data, dict) and order_data.get("id") is not None:
        return str(order_data.get("id"))

    return None


def remember_pending_order(order_id, action, trigger_price, size=None, source=None, extra=None):
    if order_id is None:
        return

    oid = str(order_id)
    data = {
        "action": action,
        "trigger_price": float(trigger_price),
        "remaining_size": float(size) if size is not None else None,
        "source": source or action,
        "created_at": int(time.time())
    }

    if extra:
        data.update(extra)

    state["pending_orders"][oid] = data

    # Market orders should fill quickly; this only prevents stale state growth.
    if len(state["pending_orders"]) > 200:
        ordered = sorted(state["pending_orders"].items(), key=lambda x: int(x[1].get("created_at", 0)))
        state["pending_orders"] = dict(ordered[-100:])

    save_state()


def get_pending_order(order_id):
    if order_id is None:
        return None
    return state.get("pending_orders", {}).get(str(order_id))


def consume_pending_order_fill(order_id, fill_size):
    if order_id is None:
        return

    oid = str(order_id)
    pending = state.get("pending_orders", {}).get(oid)
    if not pending:
        return

    remaining = pending.get("remaining_size")
    if remaining is None:
        state["pending_orders"].pop(oid, None)
        save_state()
        return

    remaining = float(remaining) - float(fill_size)
    if remaining <= 0.00001:
        state["pending_orders"].pop(oid, None)
    else:
        state["pending_orders"][oid]["remaining_size"] = remaining

    save_state()

# ============================================================
# CLEAN STALE RESERVED GRID PRICES
# ============================================================

def cleanup_stale_pending_grid_prices():

    reserved = state.get("pending_grid_prices", {})

    if not reserved:
        return

    now = int(time.time())

    remove_keys = []

    for key, data in reserved.items():

        try:
            created_at = int(data.get("created_at", 0))
        except:
            created_at = 0

        # remove after 60 seconds
        if now - created_at > 60:
            remove_keys.append(key)

    if remove_keys:

        for key in remove_keys:
            print("REMOVING STALE RESERVED PRICE:", key)
            sys.stdout.flush()

            state["pending_grid_prices"].pop(key, None)

        save_state()

def has_fresh_pending_orders():
    pending_orders = state.get("pending_orders", {})
    if not pending_orders:
        return False

    now = int(time.time())
    stale_order_ids = []
    fresh_count = 0

    for oid, pending in pending_orders.items():
        created_at = int(pending.get("created_at", 0) or 0)
        if now - created_at <= PENDING_ORDER_WAIT_SECONDS:
            fresh_count += 1
        else:
            stale_order_ids.append(oid)

    if stale_order_ids:
        for oid in stale_order_ids:
            state["pending_orders"].pop(oid, None)
        save_state()

    return fresh_count > 0

def cleanup_stale_grid_reservations():

    now = int(time.time())

    stale_keys = []

    for price_key, data in state.get("pending_grid_prices", {}).items():

        created_at = int(data.get("created_at", 0))

        # 60 sec expiry
        if now - created_at > 60:
            stale_keys.append(price_key)

    if stale_keys:

        for k in stale_keys:
            del state["pending_grid_prices"][k]

        save_state()

        print("CLEANED STALE GRID RESERVATIONS:", stale_keys)
        sys.stdout.flush()


def mark_action(action, price, order_id=None, size=None, source=None, extra=None):
    state["last_action"] = action
    state["last_action_price"] = float(price)
    state["last_order_id"] = order_id
    save_state()

    if order_id is not None:
        remember_pending_order(order_id, action, price, size=size, source=source, extra=extra)


# ============================================================
# SERIES / DYNAMIC LOT SYSTEM
# ============================================================

def get_series_floor(price):
    if SERIES_STEP <= 0:
        return None
    return math.floor(float(price) / SERIES_STEP) * SERIES_STEP


def calculate_dynamic_lot(current_series, base_series):
    if SERIES_ADD_LOT <= 0 or SERIES_STEP <= 0:
        return float(LOT_SIZE)

    if base_series is None:
        return float(LOT_SIZE)

    diff_steps = int(round((float(base_series) - float(current_series)) / SERIES_STEP))
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


def sort_pending_buybacks():
    state["pending_buybacks"] = sorted(
        state.get("pending_buybacks", []),
        key=lambda x: float(x["buy_price"]),
        reverse=True
    )
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


def add_pending_buyback(buy_price, size, is_cycle=False, source_sell_price=None, source_fill_price=None, source_fill_id=None):
    buy_price = float(buy_price)
    size = float(size)

    if size <= 0:
        return None

    buyback_id = str(source_fill_id or f"buyback-{int(time.time() * 1000)}-{len(state.get('pending_buybacks', [])) + 1}")

    # Merge same-price buybacks so the log stays readable and order size remains correct.
    for b in state.get("pending_buybacks", []):
        if abs(float(b["buy_price"]) - buy_price) < 0.01 and bool(b.get("is_cycle", False)) == bool(is_cycle):
            b["size"] = float(b.get("size", 0)) + size
            if source_sell_price is not None:
                b["source_sell_price"] = source_sell_price
            if source_fill_price is not None:
                b["source_fill_price"] = source_fill_price
            if source_fill_id is not None:
                b["source_fill_id"] = source_fill_id
            sort_pending_buybacks()
            return b

    buyback = {
        "id": buyback_id,
        "buy_price": buy_price,
        "size": size,
        "is_cycle": bool(is_cycle),
        "source_sell_price": source_sell_price,
        "source_fill_price": source_fill_price,
        "source_fill_id": source_fill_id
    }

    state["pending_buybacks"].append(buyback)
    sort_pending_buybacks()
    return buyback


def reduce_pending_buyback(buyback_id=None, buy_price=None, used_size=0):
    used_size = float(used_size)
    if used_size <= 0:
        return None

    for i, b in enumerate(state.get("pending_buybacks", [])):
        id_match = buyback_id is not None and str(b.get("id")) == str(buyback_id)
        price_match = buy_price is not None and abs(float(b.get("buy_price")) - float(buy_price)) < 0.01

        if not id_match and not price_match:
            continue

        current_size = float(b.get("size", 0))
        used = min(used_size, current_size)

        if used >= current_size - 0.00001:
            removed = state["pending_buybacks"].pop(i)
            save_state()
            return removed

        state["pending_buybacks"][i]["size"] = current_size - used
        save_state()
        return state["pending_buybacks"][i]

    return None


def get_total_level_size():
    total = 0.0
    for lv in state["levels"]:
        total += float(lv.get("size", 0))
    return float(total)


def reset_all_levels():
    state["levels"] = []
    state["pending_buybacks"] = []
    state["pending_orders"] = {}
    state["pending_grid_prices"] = {}
    state["last_grid_buy_price"] = None
    state["cycle_entry_size"] = None
    state["cycle_entry_price"] = None
    state["cycle_entry_sell_price"] = None
    state["cycle_base_series"] = None
    save_state()


def reduce_sell_level_by_index(index, sold_size):
    sold_size = float(sold_size)

    if index is None or index < 0 or index >= len(state["levels"]):
        return None

    lv = state["levels"][index]
    current_size = float(lv["size"])
    used_size = min(sold_size, current_size)

    sold_info = {
        "buy_price": float(lv["buy_price"]),
        "sell_price": float(lv["sell_price"]),
        "size": used_size,
        "is_cycle": bool(lv.get("is_cycle", False))
    }

    if used_size >= current_size - 0.00001:
        state["levels"].pop(index)
    else:
        state["levels"][index]["size"] = current_size - used_size

    save_state()
    return sold_info


def reduce_sell_level_by_target(target_price, sold_size):
    target_price = float(target_price)

    for i, lv in enumerate(state["levels"]):
        if abs(float(lv["sell_price"]) - target_price) < 0.01:
            return reduce_sell_level_by_index(i, sold_size)

    return None


def reduce_sell_level_by_fill(fill_price, sold_size):
    fill_price = float(fill_price)

    eligible = []
    for i, lv in enumerate(state["levels"]):
        sell_price = float(lv["sell_price"])
        if sell_price <= fill_price + 0.20:
            eligible.append((i, sell_price))

    if eligible:
        eligible = sorted(eligible, key=lambda x: x[1])
        return reduce_sell_level_by_index(eligible[0][0], sold_size)

    closest_index = None
    closest_diff = 999999

    for i, lv in enumerate(state["levels"]):
        diff = abs(float(lv["sell_price"]) - fill_price)
        if diff < closest_diff:
            closest_diff = diff
            closest_index = i

    if closest_index is not None and closest_diff <= 5.0:
        return reduce_sell_level_by_index(closest_index, sold_size)

    return None


def reduce_exact_sell_level(fill_price, sold_size):
    fill_price = float(fill_price)
    return reduce_sell_level_by_target(fill_price, sold_size)


def reduce_closest_sell_level(fill_price, sold_size):
    return reduce_sell_level_by_fill(fill_price, sold_size)


def get_next_downside_buy_price():
    if not state["levels"]:
        return None

    candidates = []
    lowest_buy = min([float(lv["buy_price"]) for lv in state["levels"]])
    candidates.append(float(lowest_buy - GRID))

    if state.get("last_grid_buy_price") is not None:
        candidates.append(float(state["last_grid_buy_price"]) - GRID)

    return max(candidates)


def get_next_buy_target():
    reserved_prices = state.get("pending_grid_prices", {})    
    
    candidates = []

    for b in state.get("pending_buybacks", []):

        # skip reserved prices
        price_key = str(round(float(b["buy_price"]), 2))

        if price_key in reserved_prices:
            continue

        if float(b.get("size", 0)) > 0:
            candidates.append({
                "source": "buyback",
                "buy_price": float(b["buy_price"]),
                "size": float(b["size"]),
                "is_cycle": bool(b.get("is_cycle", False)),
                "buyback_id": b.get("id")
            })

    downside_buy = get_next_downside_buy_price()

    if downside_buy is not None:

         price_key = str(round(float(downside_buy), 2))

         if price_key in reserved_prices:
             downside_buy = None

    if downside_buy is not None:
        candidates.append({
            "source": "downside",
            "buy_price": float(downside_buy),
            "size": None,
            "is_cycle": False,
            "buyback_id": None
        })

    if not candidates:
        return None

    candidates = sorted(candidates, key=lambda x: float(x["buy_price"]), reverse=True)
    return candidates[0]


def get_next_buy_price():
    target = get_next_buy_target()
    if target is None:
        return None
    return float(target["buy_price"])


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
# MANUAL TRADE SAFE SYNC HELPERS
# ============================================================

def get_cycle_target_size():
    return float(calculate_reentry_size())


def get_current_cycle_size():
    total = 0.0
    for lv in state["levels"]:
        if lv.get("is_cycle") is True:
            total += float(lv.get("size", 0))
    return float(total)


def manual_trade_position_sync(pos_size, price):
    """
    If user does manual buy/sell, levels sum mismatch occurs.
    We fix it WITHOUT breaking original logic.

    Rule:
    - If pos_size > levels_sum => missing is manual buy
      -> first fill cycle if cycle missing, then remaining as normal
    - If pos_size < levels_sum => extra is manual sell
      -> reduce levels starting from highest buy (safest)
    """

    pos_size = float(pos_size)
    price = float(price)

    total_levels = get_total_level_size()
    diff = pos_size - total_levels

    if abs(diff) < 0.01:
        return

    # Manual BUY happened
    if diff > 0:
        missing = diff

        cycle_target = get_cycle_target_size()
        current_cycle = get_current_cycle_size()

        # First try to fill cycle level if not complete
        cycle_missing = max(0.0, cycle_target - current_cycle)

        if cycle_missing > 0:
            cycle_add = min(missing, cycle_missing)
            if cycle_add > 0:
                print("MANUAL SYNC: ADDING CYCLE LEVEL:", cycle_add, "AT PRICE:", price)
                sys.stdout.flush()
                add_level(price, cycle_add, is_cycle=True)
                missing -= cycle_add

        # Remaining becomes normal grid levels
        if missing > 0:
            print("MANUAL SYNC: ADDING NORMAL LEVEL:", missing, "AT PRICE:", price)
            sys.stdout.flush()
            add_level(price, missing, is_cycle=False)

        return

    # Manual SELL happened
    if diff < 0:
        extra = abs(diff)

        print("MANUAL SYNC: DETECTED MANUAL SELL, NEED REMOVE SIZE:", extra)
        sys.stdout.flush()

        # remove from highest buy first
        state["levels"] = sorted(state["levels"], key=lambda x: float(x["buy_price"]), reverse=True)

        i = 0
        while i < len(state["levels"]) and extra > 0:
            lv = state["levels"][i]
            lv_size = float(lv.get("size", 0))

            if extra >= lv_size - 0.00001:
                extra -= lv_size
                state["levels"].pop(i)
                continue
            else:
                lv["size"] = lv_size - extra
                extra = 0
                break

        sort_levels()
        return


# ============================================================
# PROCESS NEW FILLS (FIXED DUPLICATE BUG + PARTIAL SUPPORT)
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
        order_id = get_fill_order_id(f)
        pending = get_pending_order(order_id)

        if fill_size <= 0:
            continue

        print("PROCESSING NEW FILL:", fill_side, fill_price, fill_size, "ID:", fid, "ORDER:", order_id, "PENDING:", pending)
        sys.stdout.flush()

        # ============================================================
        # FIX: MANUAL BUY SPLIT LOGIC INSIDE FILL PROCESSING
        # ============================================================
        if fill_side == "buy":
            source = pending.get("source") if pending else None

            if source == "entry":
                print("FILL BUY -> ENTRY CYCLE:", fill_size)
                sys.stdout.flush()
                add_level(fill_price, fill_size, is_cycle=True)

            elif source == "buyback":
                print("FILL BUY -> RECYCLED BUYBACK:", fill_size, "AT:", fill_price)
                sys.stdout.flush()
                reduce_pending_buyback(
                    buyback_id=pending.get("buyback_id"),
                    buy_price=pending.get("trigger_price"),
                    used_size=fill_size
                )
                add_level(fill_price, fill_size, is_cycle=bool(pending.get("is_cycle", False)))

            elif source == "downside":
                print("FILL BUY -> DOWNSIDE GRID:", fill_size)
                sys.stdout.flush()
                add_level(fill_price, fill_size, is_cycle=False)

            else:
                cycle_target = get_cycle_target_size()
                current_cycle = get_current_cycle_size()
                cycle_missing = max(0.0, cycle_target - current_cycle)

                if cycle_missing > 0:
                    cycle_add = min(fill_size, cycle_missing)
                    if cycle_add > 0:
                        print("FILL BUY -> SPLIT: ADDING CYCLE:", cycle_add)
                        sys.stdout.flush()
                        add_level(fill_price, cycle_add, is_cycle=True)

                    remaining = fill_size - cycle_add
                    if remaining > 0:
                        print("FILL BUY -> SPLIT: ADDING NORMAL:", remaining)
                        sys.stdout.flush()
                        add_level(fill_price, remaining, is_cycle=False)
                else:
                    add_level(fill_price, fill_size, is_cycle=False)

            state["last_grid_buy_price"] = fill_price
            consume_pending_order_fill(order_id, fill_size)

            # ============================================================
            # REMOVE RESERVED GRID PRICE AFTER BUY FILL
            # ============================================================

            price_key = str(round(float(fill_price), 2))

            if price_key in state["pending_grid_prices"]:
                del state["pending_grid_prices"][price_key]

            save_state()


        elif fill_side == "sell":
            sold_info = None
            source = pending.get("source") if pending else None

            if source == "sell":
                sold_info = reduce_sell_level_by_target(pending.get("trigger_price"), fill_size)

            if sold_info is None:
                sold_info = reduce_sell_level_by_fill(fill_price, fill_size)

            if sold_info is not None:
                buyback_price = fill_price - GRID
                print("FILL SELL -> ADD BUYBACK:", buyback_price, "SIZE:", sold_info["size"])
                sys.stdout.flush()
                add_pending_buyback(
                    buyback_price,
                    sold_info["size"],
                    is_cycle=bool(sold_info.get("is_cycle", False)),
                    source_sell_price=sold_info.get("sell_price"),
                    source_fill_price=fill_price,
                    source_fill_id=fid
                )

            consume_pending_order_fill(order_id, fill_size)

        state["last_fill_id"] = fid
        state["last_fill_price"] = fill_price
        state["last_fill_side"] = fill_side

        state["processed_fill_ids"].append(fid)

    if len(state["processed_fill_ids"]) > 5000:
        state["processed_fill_ids"] = state["processed_fill_ids"][-2000:]

    save_state()

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

    FIXED: No proportional scaling (fractional bug removed).
    Instead we trim open_levels to match pos_size.
    """

    print("REBUILDING LEVELS FROM FILLS HISTORY...")
    sys.stdout.flush()

    reset_all_levels()

    fills = get_fills(page_size=RECOVERY_FILL_SCAN)
    fills = [f for f in fills if f.get("product_symbol") == SYMBOL]
    fills = sorted(fills, key=lambda x: x.get("created_at", ""))

    open_levels = []
    last_grid_buy_price = None

    for f in fills:
        side = f.get("side")
        price = float(f.get("price"))
        size = float(f.get("size", 0))

        if size <= 0:
            continue

        if side == "buy":
            last_grid_buy_price = price
            open_levels.append({
                "buy_price": price,
                "sell_price": price + GRID,
                "size": size,
                "is_cycle": False
            })

        elif side == "sell":
            remaining_sell = size

            while remaining_sell > 0 and open_levels:
                eligible = []
                for i, lv in enumerate(open_levels):
                    sell_price = float(lv["sell_price"])
                    if sell_price <= price + 0.20:
                        eligible.append((i, sell_price))

                if eligible:
                    target_index = sorted(eligible, key=lambda x: x[1])[0][0]
                else:
                    target_index = None
                    closest_diff = 999999

                    for i, lv in enumerate(open_levels):
                        diff = abs(float(lv["sell_price"]) - price)
                        if diff < closest_diff:
                            closest_diff = diff
                            target_index = i

                    if target_index is None or closest_diff > 5.0:
                        break

                lv = open_levels[target_index]
                lv_size = float(lv["size"])

                if remaining_sell >= lv_size - 0.00001:
                    remaining_sell -= lv_size
                    open_levels.pop(target_index)
                    continue

                lv["size"] = lv_size - remaining_sell
                remaining_sell = 0
                break

    open_levels = sorted(open_levels, key=lambda x: float(x["buy_price"]))

    total = sum([float(x["size"]) for x in open_levels])

    if total <= 0:
        reset_all_levels()
        return

    # FIX: NO FRACTIONAL SCALING
    # Instead trim from highest buy until total == pos_size
    if abs(total - pos_size) > 0.01:
        print("REBUILD MISMATCH -> TRIMMING LEVELS")
        print("LEVEL TOTAL:", total, "EXCHANGE POS:", pos_size)
        sys.stdout.flush()

        if total > pos_size:
            extra = total - pos_size

            open_levels = sorted(open_levels, key=lambda x: float(x["buy_price"]), reverse=True)

            i = 0
            while i < len(open_levels) and extra > 0:
                lv = open_levels[i]
                lv_size = float(lv["size"])

                if extra >= lv_size - 0.00001:
                    extra -= lv_size
                    open_levels.pop(i)
                    continue
                else:
                    lv["size"] = lv_size - extra
                    extra = 0
                    break

            open_levels = sorted(open_levels, key=lambda x: float(x["buy_price"]))

    # ============================================================
    # FIX: RESTORE CYCLE FLAG AFTER REBUILD
    # Cycle is always first target size (LOT_SIZE*ENTRY_MULTIPLIER)
    # ============================================================
    cycle_target = get_cycle_target_size()
    cycle_remaining = cycle_target

    for lv in open_levels:
        if cycle_remaining <= 0:
            break

        lv_size = float(lv["size"])
        if lv_size <= 0:
            continue

        if lv_size <= cycle_remaining + 0.00001:
            lv["is_cycle"] = True
            cycle_remaining -= lv_size
        else:
            # split level if needed
            cycle_part = cycle_remaining
            normal_part = lv_size - cycle_part

            lv["size"] = cycle_part
            lv["is_cycle"] = True

            open_levels.append({
                "buy_price": lv["buy_price"],
                "sell_price": lv["sell_price"],
                "size": normal_part,
                "is_cycle": False
            })

            cycle_remaining = 0
            break

    open_levels = sorted(open_levels, key=lambda x: float(x["buy_price"]))

    state["levels"] = []
    for lv in open_levels:
        if float(lv["size"]) > 0.00001:
            state["levels"].append(lv)

    state["last_grid_buy_price"] = last_grid_buy_price
    sort_levels()

    print("REBUILD DONE. LEVELS:", state["levels"])
    sys.stdout.flush()


# ============================================================
# MISMATCH PROTECTION + MANUAL TRADE SAFE MODE
# ============================================================

def mismatch_protection_check():
    pos_size = get_open_position_size()

    if pos_size == 0:
        reset_all_levels()
        return

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

    # MANUAL TRADE SAFE MODE (MAIN FIX)
    if abs(total_levels - pos_size) > 0.01:
        if has_fresh_pending_orders():
            print("MISMATCH DETECTED BUT BOT ORDER IS STILL PENDING -> WAITING FOR FILL SYNC")
            print("EXCHANGE POS:", pos_size, "STATE LEVEL SUM:", total_levels)
            sys.stdout.flush()
            return

        try:
            live_price = get_live_price()
        except:
            live_price = state.get("last_fill_price") or 0

        print("MISMATCH DETECTED -> TRYING MANUAL TRADE SYNC")
        print("EXCHANGE POS:", pos_size, "STATE LEVEL SUM:", total_levels)
        sys.stdout.flush()

        manual_trade_position_sync(pos_size, live_price)

        # re-check after sync
        total_levels2 = get_total_level_size()

        if abs(total_levels2 - pos_size) > 0.5:
            print("MANUAL SYNC FAILED -> FULL RECOVERY FROM FILLS")
            sys.stdout.flush()
            rebuild_levels_from_fills(pos_size)


def bootstrap_current_fills_as_processed(page_size=None):
    """
    Critical safety guard.
    On a fresh deploy/restart with an empty state file, Delta returns old fills.
    Those historical fills must not be treated as new trades, otherwise old sells
    create stale buybacks and the bot can place many immediate market buys.
    """

    global processed_fill_set

    if page_size is None:
        page_size = RECOVERY_FILL_SCAN

    fills = get_fills(page_size=page_size)
    fill_ids = []

    for f in fills:
        if f.get("product_symbol") != SYMBOL:
            continue

        fid = f.get("id")
        if fid is None:
            continue

        fill_ids.append(fid)

    if not fill_ids:
        return

    for fid in fill_ids:
        if fid not in processed_fill_set:
            processed_fill_set.add(fid)
            state["processed_fill_ids"].append(fid)

    # Keep startup state clean. Pending orders/buybacks are only valid if this
    # exact bot created them in the current persisted state.
    state["pending_orders"] = {}

    if len(state["processed_fill_ids"]) > 5000:
        state["processed_fill_ids"] = state["processed_fill_ids"][-2000:]

    state["state_schema_version"] = STATE_SCHEMA_VERSION
    save_state()

    print("BOOTSTRAP SAFETY: MARKED EXISTING FILLS AS PROCESSED:", len(fill_ids))
    sys.stdout.flush()


def pending_buyback_exists_for_fill(fill_id):
    if fill_id is None:
        return False

    for b in state.get("pending_buybacks", []):
        if str(b.get("source_fill_id")) == str(fill_id):
            return True

    return False


def recover_recent_sell_buybacks():
    """
    Disabled on purpose.
    Historical sell recovery can create stale buyback orders after deployment.
    Buybacks are created only from fresh fills processed while this bot is
    running with a valid processed_fill_ids state.
    """
    return


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

    if state.get("cycle_base_series") is None and pos_size > 0:
        state["cycle_base_series"] = get_series_floor(price)
        save_state()

    mismatch_protection_check()

    if state_needs_fill_bootstrap:
        print("STATE UPGRADE/FRESH START -> BOOTSTRAPPING FILL IDS ONLY")
        sys.stdout.flush()
        bootstrap_current_fills_as_processed()

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
            PENDING_ORDER_WAIT_SECONDS = int(os.getenv("PENDING_ORDER_WAIT_SECONDS", "60"))

            process_new_fills()
            cleanup_stale_grid_reservations()
            mismatch_protection_check()

            price = get_live_price()
            pos_size = get_open_position_size()

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
            print(f"{now} PRICE:{price} POS:{pos_size} CYCLE_SERIES:{state.get('cycle_base_series')} DYNAMIC_LOT:{dynamic_lot} NEXT_BUY:{get_next_buy_price()} LEVELS:{state.get('levels')} BUYBACKS:{state.get('pending_buybacks')}")
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

                state["cycle_base_series"] = get_series_floor(price)
                mark_reentry_time()

                resp = place_market_order("buy", entry_size)

                if resp.get("success") is True:
                    order_id = resp.get("result", {}).get("id")
                    mark_action("buy", price, order_id, size=entry_size, source="entry")

                    time.sleep(1)
                    process_new_fills()

                time.sleep(SLEEP_SECONDS)
                continue

            # ============================================================
            # MULTI BUY LOOP (PRICE JUMP DOWN)
            # ============================================================
            while True:

                if has_fresh_pending_orders():
                    print("PENDING ORDER EXISTS -> WAITING")
                    sys.stdout.flush()
                    break

                buy_target = get_next_buy_target()

                if buy_target is None:
                    break

                next_buy = float(buy_target["buy_price"])

                if price > next_buy:
                    break

                if already_executed_same_price("buy", next_buy):
                    break

                current_series = get_series_floor(price)
                dynamic_lot = calculate_dynamic_lot(current_series, state.get("cycle_base_series"))
                buy_size = float(buy_target["size"]) if buy_target.get("source") == "buyback" else dynamic_lot

                print("GRID BUY TRIGGERED AT:", next_buy, "BUY SIZE:", buy_size, "SOURCE:", buy_target.get("source"))
                sys.stdout.flush()

                resp = place_market_order("buy", buy_size)

                if resp.get("success") is True:

                    # ============================================================
                    # RESERVE THIS GRID PRICE IMMEDIATELY
                    # ============================================================

                    price_key = str(round(float(next_buy), 2))

                    state["pending_grid_prices"][price_key] = {
                        "price": float(next_buy),
                        "created_at": int(time.time())
                    }

                    save_state()

                    order_id = resp.get("result", {}).get("id")

                    mark_action(
                        "buy",
                        next_buy,
                        order_id,
                        size=buy_size,
                        source=buy_target.get("source"),
                        extra={
                            "buyback_id": buy_target.get("buyback_id"),
                            "is_cycle": bool(buy_target.get("is_cycle", False))
                        }
                    )

                    time.sleep(1)
                    process_new_fills()

                    pos_size = get_open_position_size()

                    if pos_size < 0:
                        break

                else:
                    break

                # refresh live price for jump down multi loop
            price = get_live_price()

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
                    mark_action(
                        "sell",
                        sell_price,
                        order_id,
                        size=sell_size,
                        source="sell",
                        extra={
                            "buy_price": float(sell_level.get("buy_price")),
                            "sell_price": sell_price,
                            "is_cycle": bool(sell_level.get("is_cycle", False))
                        }
                    )

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

                # refresh live price for jump up multi loop
                price = get_live_price()

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
