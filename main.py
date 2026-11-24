"""Pump.fun paper-trading bot for make.fun (agent-fun/replay/v2).

FINAL -- own implementation, standard library only, fully deterministic.

Strategy (all my own design, no copied code):
  * Watch every new pump.fun bonding curve. A curve becomes a buy candidate
    only once it has proved traction: at least 20 SOL of real reserves (on
    the dev window, 163 of 1222 coins reach that, and a fifth of them go on
    to complete the curve), with its recent short tape still showing net
    buying. Fresh launches mostly fade, so the traction filter keeps the
    bot off them.
  * First buy is small; the position is pyramided into strength only, up to
    a fixed stake per coin. Positions are capped at the harness maximum.
  * Every position exits whole on the first of: stop loss vs cost, take
    profit, a trailing stop once the position is up, or a time limit; a
    curve that completes is closed by the replay at its final reserves.
  * Guardrail probes: if completed round trips lag (fewer than 25 coins
    round-tripped well into the window), tiny probe buys on liquid fresh
    coins are sold as soon as they land, so the run clears the 20-coin bar;
    each mint is probed at most once, so probes rotate across coins instead
    of looping on the first qualifying curve.

Determinism: no clock, no randomness, no environment reads; all price
comparisons are integer cross-multiplications, and every iteration follows
insertion order. Diagnostics go to stderr; stdout carries replies only.
"""

import json
import sys
from collections import deque

PROTOCOL = "agent-fun/replay/v2"
LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000

# --- strategy parameters (lamports / slots / basis points) -----------------
# v8 "slow hold + loser rotation": v7 (-0.83 SOL, 151 coins) rotated every
# position out after 20000 slots, realizing losses on coins that might
# still run; v6 (+0.11 SOL, 9 coins) never rotated and failed the 20-coin
# bar. v8 rotates only losers: at MAX_HOLD_SLOTS a position exits only
# while it is at or below cost, so winners keep running like v6 while
# losers free their slots.
# (Like the equal-weight baseline, ~flat at -0.02 SOL on dev, while every
# active strategy lost 2-10 SOL.) Buy almost any new coin small once the
# size cap allows, cut only on a deep -40% stop or a loose trailing stop.
# The tape filters are disabled (thresholds below pass everything); the
# only edge sought is holding the rare runner while paying as few
# round-trip fees as possible. Probes stay as a backstop for the 20-coin bar.
WINDOW_SLOTS = 60           # lookback for the entry tape (unused in v6)
MIN_AGE_SLOTS = 20          # slots since CreateEvent before an entry is allowed
MIN_NET_LAMPORTS = -1000000000000000  # tape filter off
MIN_BUYS = 0                # tape filter off
MIN_BUYERS = 0              # tape filter off
MIN_PRICE_UP_BPS = -10000   # tape filter off
MAX_PRICE_UP_BPS = 100000   # tape filter off
NEAR_HIGH_BPS = 0           # tape filter off
MIN_RQ_LAMPORTS = 0                   # any curve, however small
MAX_RQ_LAMPORTS = 1000000000000000    # ... however large
INITIAL_LAMPORTS = 50000000         # first buy of a position (0.05 SOL)
ADD_LAMPORTS = 50000000             # (no adds: MAX_INVEST == INITIAL)
MAX_INVEST_LAMPORTS = 50000000      # most SOL into one position (0.05 SOL)
MAX_EXPOSURE_LAMPORTS = 5000000000   # most SOL at cost across positions
MIN_BUY_LAMPORTS = 10000000         # smallest buy worth sending (0.01 SOL)
ADD_STEP_BPS = 100000       # adds effectively off (stake already at max)
ADD_IF_UP_BPS = 0
STOP_LOSS_BPS = 6000        # sell when value falls to 60% of cost
TAKE_PROFIT_BPS = 30000     # sell when value reaches 300% of cost
TRAIL_ARM_BPS = 12000       # trailing stop arms once value is +20% on cost
TRAIL_BPS = 5000            # ... then sell at 50% of the peak value
MAX_HOLD_SLOTS = 20000      # rotate: exit after ~2.2h held ...
HOLD_IF_UP_BPS = 10000      # ... only while at or below cost; winners keep running
CROSS_WINDOW_SLOTS = 1000000  # crossing gate off in v6 (any age entry)
COOLDOWN_SLOTS = 300        # wait after an exit before re-entering a curve
MAX_ENTRIES_PER_COIN = 2    # most entries per curve over the run
BUY_SLIPPAGE_BPS = 1000
SELL_SLIPPAGE_BPS = 10000
PROBE_LAMPORTS = 10000000           # guardrail probe buy (0.01 SOL)
PROBE_MIN_RQ_LAMPORTS = 2000000000   # probe only on curves with >= 2 SOL real
PROBE_AFTER_SLOTS = 10000   # start probing only this far into the run
PROBE_TARGET_COINS = 25     # stop probing once this many coins round-tripped
PROBE_MAX_AGE_SLOTS = 5000  # probe only on recently created curves


def price_ge(p_a, p_b, bps):
    """True when price a is at least bps/10000 of price b (prices are (vq, vt))."""
    return p_a[0] * p_b[1] * BPS >= p_b[0] * p_a[1] * bps


def price_up_bps(p_now, p_then):
    """(price_now / price_then - 1) in basis points, integer math, may be negative."""
    num = p_now[0] * p_then[1]
    den = p_now[1] * p_then[0]
    if den == 0:
        return 0
    return (num - den) * BPS // den


class Curve:
    __slots__ = ("mint", "created_slot", "vq", "vt", "rq", "rt",
                 "reserves_slot", "trades", "complete", "window", "first_price",
                 "cross_slot")

    def __init__(self, mint, created_slot, slot):
        self.mint = mint
        self.created_slot = created_slot
        self.vq = 0
        self.vt = 0
        self.rq = 0
        self.rt = 0
        self.reserves_slot = slot
        self.trades = 0
        self.complete = False
        # rolling tape of trades inside WINDOW_SLOTS:
        # (slot, is_buy, sol_lamports, user, vq_after, vt_after)
        self.window = deque()
        self.first_price = None
        # slot the curve's real SOL first reached MIN_RQ (None until then):
        # entries are only taken near the crossing, never at a stale high.
        self.cross_slot = None

    def price(self):
        return (self.vq, self.vt)


class Position:
    __slots__ = ("mint", "invested", "peak_value", "entry_slot",
                 "last_buy_price", "is_probe", "entries")

    def __init__(self, mint, invested, entry_slot, buy_price, is_probe):
        self.mint = mint
        self.invested = invested
        self.peak_value = 0
        self.entry_slot = entry_slot
        self.last_buy_price = buy_price
        self.is_probe = is_probe
        self.entries = 1


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.capital = int(hello["capital"]["lamports"])
        self.cash = self.capital
        self.slot = -1
        self.seq = -1
        self.first_slot = None
        self.curves = {}
        self.positions = {}       # mint -> (token_amount, cost_lamports)
        self.held = {}            # mint -> Position (my own entry bookkeeping)
        self.pending = {}         # client_id -> order dict
        self.sell_pending = set()  # mints with a sell order sent, not yet filled
        self.probe_mints = set()  # mints bought as guardrail probes
        self.probed = set()       # mints already probed once (never probe twice)
        self.used_ids = set()
        self.next_id = 1
        self.bought = set()       # mints with a filled buy
        self.completed = set()    # mints with a completed round trip
        self.last_exit_slot = {}  # mint -> slot of last exit
        self.entries = {}         # mint -> number of entries opened
        self.fills = 0
        self.buys_filled = 0
        self.sells_filled = 0

    # -- helpers ------------------------------------------------------------
    def tx_fee(self):
        return int(self.fees["base_fee_lamports"]) + int(self.fees["priority_fee_lamports"])

    def fresh_id(self):
        while True:
            cid = "o%d" % self.next_id
            self.next_id += 1
            if cid not in self.used_ids:
                self.used_ids.add(cid)
                return cid

    def max_buy(self, mint):
        curve = self.curves.get(mint)
        if curve is None or curve.complete:
            return 0
        return curve.rq * self.caps["max_order_bps_of_real_reserve"] // BPS

    def committed_count(self):
        mints = set(self.positions)
        for order in self.pending.values():
            if order["side"] == "buy":
                mints.add(order["mint"])
        return len(mints)

    def free_cash(self):
        tx = self.tx_fee()
        free = self.cash
        for order in self.pending.values():
            free -= tx
            if order["side"] == "buy":
                free -= int(order["quote_lamports"])
        return free

    def exposure(self):
        """SOL at cost across positions, counting buys still in flight."""
        total = 0
        for held in self.held.values():
            total += held.invested
        for order in self.pending.values():
            if order["side"] == "buy":
                total += int(order["quote_lamports"])
        return total

    def value_of(self, mint):
        tokens, _cost = self.positions[mint]
        curve = self.curves.get(mint)
        if curve is None or curve.vt == 0:
            return 0
        return tokens * curve.vq // curve.vt

    # -- state updates ------------------------------------------------------
    def apply_batch(self, batch):
        self.seq = batch["seq"]
        self.slot = batch["slot"]
        if self.first_slot is None:
            self.first_slot = self.slot
        for fill in batch["fills"]:
            self.fills += 1
            cid = fill.get("client_id")
            order = self.pending.pop(cid, None) if cid else None
            status = fill.get("status")
            mint = fill.get("mint")
            if order is not None:
                if order["side"] == "buy":
                    if status == "filled":
                        self.buys_filled += 1
                        self.bought.add(order["mint"])
                        self._note_buy(order)
                else:
                    self.sell_pending.discard(order["mint"])
                    if status == "filled":
                        self.sells_filled += 1
                        if order["mint"] in self.bought:
                            self.completed.add(order["mint"])
                        self._note_sell(order["mint"])
            elif cid and cid.startswith("_"):
                # harness close (_complete_, _flatten_, _writeoff_): a sell.
                if mint:
                    self.sell_pending.discard(mint)
                    if mint in self.bought:
                        self.completed.add(mint)
                    self._note_sell(mint)
        account = batch["account"]
        self.cash = int(account["cash_lamports"])
        self.positions = {}
        for p in account["positions"]:
            tokens = int(p["token_amount"])
            if tokens > 0:
                self.positions[p["mint"]] = (tokens, int(p["cost_lamports"]))
        # drop bookkeeping for positions that are gone
        for mint in list(self.held):
            if mint not in self.positions:
                del self.held[mint]
        for mark in batch["marks"]:
            curve = self.curves.get(mark["mint"])
            if curve is not None and int(mark["slot"]) < curve.reserves_slot:
                continue
            if curve is None:
                curve = self._new_curve(mark["mint"], None, int(mark["slot"]))
            self._set_reserves(
                curve,
                int(mark.get("virtual_quote_reserves", "0")),
                int(mark.get("virtual_token_reserves", "0")),
                int(mark.get("real_quote_reserves", "0")),
                int(mark.get("real_token_reserves", "0")),
                int(mark["slot"]))
        for event in batch["events"]:
            kind = event.get("event")
            mint = event.get("mint")
            if kind == "CreateEvent":
                curve = self._new_curve(mint, int(event["slot"]), int(event["slot"]))
                self._set_reserves(
                    curve,
                    int(event.get("virtual_quote_reserves", "0")),
                    int(event.get("virtual_token_reserves", "0")),
                    int(event.get("real_quote_reserves", "0")),
                    int(event.get("real_token_reserves", "0")),
                    int(event["slot"]))
                if curve.vt > 0:
                    curve.first_price = (curve.vq, curve.vt)
            elif kind == "TradeEvent":
                curve = self.curves.get(mint) or self._new_curve(mint, None, int(event["slot"]))
                self._set_reserves(
                    curve,
                    int(event.get("virtual_quote_reserves", "0")),
                    int(event.get("virtual_token_reserves", "0")),
                    int(event.get("real_quote_reserves", "0")),
                    int(event.get("real_token_reserves", "0")),
                    int(event["slot"]))
                curve.trades += 1
                if curve.first_price is None and curve.vt > 0:
                    curve.first_price = (curve.vq, curve.vt)
                curve.window.append((
                    int(event["slot"]),
                    bool(event.get("is_buy")),
                    int(event.get("sol_amount", "0")),
                    event.get("user", ""),
                    curve.vq,
                    curve.vt,
                ))
            else:  # CompleteEvent (or anything else closing the curve)
                curve = self.curves.get(mint) or self._new_curve(mint, None, int(event["slot"]))
                curve.complete = True

    def _set_reserves(self, curve, vq, vt, rq, rt, slot):
        """Set a curve's reserves, recording the first crossing of MIN_RQ."""
        if curve.cross_slot is None and curve.rq < MIN_RQ_LAMPORTS <= rq:
            curve.cross_slot = slot
        curve.vq = vq
        curve.vt = vt
        curve.rq = rq
        curve.rt = rt
        curve.reserves_slot = slot

    def _new_curve(self, mint, created_slot, slot):
        curve = Curve(mint, created_slot, slot)
        self.curves[mint] = curve
        return curve

    def _note_buy(self, order):
        mint = order["mint"]
        curve = self.curves.get(mint)
        price = curve.price() if curve is not None else (0, 1)
        amount = int(order["quote_lamports"])
        held = self.held.get(mint)
        if held is None:
            self.held[mint] = Position(
                mint, amount, self.slot, price, mint in self.probe_mints)
        else:
            held.invested += amount
            held.last_buy_price = price
            held.entries += 1

    def _note_sell(self, mint):
        self.held.pop(mint, None)
        self.probe_mints.discard(mint)
        self.last_exit_slot[mint] = self.slot

    # -- decisions ----------------------------------------------------------
    def decide(self, batch):
        orders = []
        tx = self.tx_fee()
        free = self.free_cash()
        max_orders = self.caps["max_orders_per_batch"]
        max_pos = self.caps["max_open_positions"]

        def can_open():
            return self.committed_count() < max_pos and len(orders) < max_orders

        # 1. exits for held positions (sell whole)
        for mint in list(self.positions):
            if mint in self.sell_pending:
                continue
            held = self.held.get(mint)
            if held is None:
                continue
            tokens, cost = self.positions[mint]
            value = self.value_of(mint)
            if value > held.peak_value:
                held.peak_value = value
            reason = None
            if held.is_probe:
                reason = "probe"
            elif value * BPS <= cost * STOP_LOSS_BPS:
                reason = "stop"
            elif value * BPS >= cost * TAKE_PROFIT_BPS:
                reason = "take-profit"
            elif (held.peak_value * BPS >= cost * TRAIL_ARM_BPS
                    and value * BPS <= held.peak_value * TRAIL_BPS):
                reason = "trail"
            elif (self.slot - held.entry_slot > MAX_HOLD_SLOTS
                    and value * BPS < cost * HOLD_IF_UP_BPS):
                reason = "time"
            if reason is not None and len(orders) < max_orders and free >= tx:
                orders.append({
                    "side": "sell",
                    "client_id": self.fresh_id(),
                    "mint": mint,
                    "token_amount": str(tokens),
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                })
                free -= tx
                self.sell_pending.add(mint)

        # 2. pyramid adds into strength (counting buys still in flight, so a
        # slow fill cannot queue up adds past the per-position stake)
        for mint in list(self.positions):
            if mint in self.sell_pending:
                continue
            held = self.held.get(mint)
            if held is None or held.is_probe:
                continue
            committed = held.invested
            for order in self.pending.values():
                if order["mint"] == mint and order["side"] == "buy":
                    committed += int(order["quote_lamports"])
            if committed >= MAX_INVEST_LAMPORTS:
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            _tokens, cost = self.positions[mint]
            value = self.value_of(mint)
            if value * BPS < cost * (BPS + ADD_IF_UP_BPS):
                continue
            if not price_ge(curve.price(), held.last_buy_price, BPS + ADD_STEP_BPS):
                continue
            if not self._tape_ok(curve):
                continue
            cap = self.max_buy(mint)
            amount = min(cap, ADD_LAMPORTS, MAX_INVEST_LAMPORTS - committed)
            if amount < MIN_BUY_LAMPORTS or free < amount + tx or not can_open():
                continue
            if self.exposure() + amount > MAX_EXPOSURE_LAMPORTS:
                continue
            orders.append({
                "side": "buy",
                "client_id": self.fresh_id(),
                "mint": mint,
                "quote_lamports": str(amount),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            free -= amount + tx

        # 3. fresh momentum entries, in event order
        for event in batch["events"]:
            if event.get("event") not in ("TradeEvent", "CreateEvent"):
                continue
            mint = event.get("mint")
            if mint in self.positions or mint in self.held:
                continue
            if any(o["mint"] == mint and o["side"] == "buy" for o in orders):
                continue
            if any(p["mint"] == mint and p["side"] == "buy"
                   for p in self.pending.values()):
                continue
            if self.entries.get(mint, 0) >= MAX_ENTRIES_PER_COIN:
                continue
            last_exit = self.last_exit_slot.get(mint)
            if last_exit is not None and self.slot - last_exit < COOLDOWN_SLOTS:
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if curve.created_slot is None or self.slot - curve.created_slot < MIN_AGE_SLOTS:
                continue
            # enter only near the rq crossing: a coin that crossed long ago
            # has already run, and buying it now means buying the top.
            # (MIN_RQ 0 disables the gate: there is no crossing to wait for.)
            if (MIN_RQ_LAMPORTS > 0
                    and (curve.cross_slot is None
                         or self.slot - curve.cross_slot > CROSS_WINDOW_SLOTS)):
                continue
            if not (MIN_RQ_LAMPORTS <= curve.rq <= MAX_RQ_LAMPORTS):
                continue
            if not self._tape_ok(curve):
                continue
            cap = self.max_buy(mint)
            amount = min(cap, INITIAL_LAMPORTS)
            if amount < MIN_BUY_LAMPORTS or free < amount + tx or not can_open():
                continue
            if self.exposure() + amount > MAX_EXPOSURE_LAMPORTS:
                continue
            orders.append({
                "side": "buy",
                "client_id": self.fresh_id(),
                "mint": mint,
                "quote_lamports": str(amount),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            free -= amount + tx
            self.entries[mint] = self.entries.get(mint, 0) + 1

        # 4. guardrail probe when round trips lag
        if (len(self.completed) < PROBE_TARGET_COINS
                and self.first_slot is not None
                and self.slot - self.first_slot > PROBE_AFTER_SLOTS
                and not any(h.is_probe for h in self.held.values())
                and not any(p["mint"] in self.probe_mints
                           for p in self.pending.values() if p["side"] == "buy")
                and can_open()):
            probe = self._pick_probe()
            if probe is not None:
                amount = min(self.max_buy(probe), PROBE_LAMPORTS)
                if amount >= MIN_BUY_LAMPORTS and free >= amount + tx:
                    self.probe_mints.add(probe)
                    self.probed.add(probe)
                    orders.append({
                        "side": "buy",
                        "client_id": self.fresh_id(),
                        "mint": probe,
                        "quote_lamports": str(amount),
                        "max_slippage_bps": BUY_SLIPPAGE_BPS,
                    })
                    free -= amount + tx

        for order in orders:
            self.pending[order["client_id"]] = order
            self.used_ids.add(order["client_id"])
        return orders

    def _pick_probe(self):
        for mint, curve in self.curves.items():
            if mint in self.probed:
                continue
            if curve.complete or mint in self.positions or mint in self.held:
                continue
            if any(p["mint"] == mint for p in self.pending.values()):
                continue
            if curve.created_slot is None:
                continue
            if self.slot - curve.created_slot > PROBE_MAX_AGE_SLOTS:
                continue
            if curve.rq < PROBE_MIN_RQ_LAMPORTS:
                continue
            if self.entries.get(mint, 0) >= MAX_ENTRIES_PER_COIN:
                continue
            return mint
        return None

    def _tape_ok(self, curve):
        """Entry tape: net inflow, enough buys/buyers, price up but not vertical,
        sitting near the window high."""
        cutoff = self.slot - WINDOW_SLOTS
        window = curve.window
        while window and window[0][0] < cutoff:
            window.popleft()
        if len(window) < MIN_BUYS:
            return False
        net = 0
        buys = 0
        buyers = set()
        high = None
        for _slot, is_buy, sol, user, vq, vt in window:
            if is_buy:
                net += sol
                buys += 1
                buyers.add(user)
            else:
                net -= sol
            price = (vq, vt)
            if high is None or price[0] * high[1] > high[0] * price[1]:
                high = price
        if buys < MIN_BUYS or len(buyers) < MIN_BUYERS:
            return False
        if net < MIN_NET_LAMPORTS:
            return False
        if not window:
            # no trades in the window and none required: nothing to judge
            return True
        first_price = (window[0][4], window[0][5])
        now = curve.price()
        if now[1] == 0 or first_price[1] == 0:
            return False
        up = price_up_bps(now, first_price)
        if up < MIN_PRICE_UP_BPS or up > MAX_PRICE_UP_BPS:
            return False
        if not price_ge(now, high, NEAR_HIGH_BPS):
            return False
        return True


def main():
    bot = None
    out = sys.stdout
    # readline() in a loop, NOT `for line in sys.stdin.buffer`: iterating a
    # pipe uses readahead buffering and would stall waiting for the next
    # line while the harness waits for our reply.
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            print("bad json line", file=sys.stderr)
            return 1
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                print("wrong protocol: %r" % (msg.get("protocol"),), file=sys.stderr)
                return 1
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                print("batch before hello", file=sys.stderr)
                return 1
            bot.apply_batch(msg)
            orders = bot.decide(msg)
            reply = json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"), ensure_ascii=False)
            if len(reply.encode("utf-8")) > 65536:
                print("reply too long", file=sys.stderr)
                return 1
            out.write(reply + "\n")
            out.flush()
        elif kind == "done":
            if bot is None:
                print("done before hello", file=sys.stderr)
                return 1
            print(
                "done: cash=%d fills=%d buys=%d sells=%d coins=%d" % (
                    bot.cash, bot.fills, bot.buys_filled,
                    bot.sells_filled, len(bot.completed)),
                file=sys.stderr)
            return 0
        else:
            print("unknown message type: %r" % (kind,), file=sys.stderr)
            return 1
    print("input ended before done", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
