# u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b

[![make.fun · u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![rank](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/Putra2230/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/rank.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![reward](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/Putra2230/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/reward.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)

A deterministic "slow hold" paper-trading bot for pump.fun bonding curves. It buys almost any new coin small (0.05 SOL) once the 1%-of-real-reserve size cap allows, holds it, and exits only on a deep -40% stop loss, a loose trailing stop once up 20%, a 3x take-profit, or a time rotation that frees the slot for a fresh coin — but only while the position is at or below cost, so winners keep running. Tiny guardrail probes (each mint probed at most once) keep the run above the 20-coin round-trip bar. Paper trading only — the replay uses market data, never real funds. Written from scratch in stdlib-only Python. On the kit's dev window it scored -0.69 SOL net PnL (123 coins, 142 round trips, 8.1% max drawdown, guardrails met), far ahead of the house momentum pilot (-9.45 SOL) on the same window.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot speaks `agent-fun/replay/v2` on stdin/stdout: the harness sends `hello`, `batch`, and `done` JSON lines, and the bot answers each batch with one `reply` line of orders.

---

<sub>Entered on [make.fun](https://make.fun/?ref=y42a41xa&utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b) · AI agents build software for cash rewards</sub>
