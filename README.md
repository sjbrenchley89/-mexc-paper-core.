# MEXC paper core

Start with [the numbered, no-code walkthrough](START-HERE.md).

Stage 1 is paper-only. It does not use exchange credentials, read real balances, or place real orders. Live mode, Tradovate and portal imports are refused. Default: Option C (TradingView, scanner, manual news veto), copy slot on but disconnected, BTCUSDT, $50 simulated equity, $10 position ceiling, 0.5% planned risk per trade, 10% daily loss pause with manual review, and $25 floor.

Python 3.12+, standard library only. In this folder, type python bot.py init, then python bot.py status. On macOS/Linux use python3 if required. The full walkthrough explains each command and expected result.

No guaranteed returns. Synthetic demos are software checks, not trading evidence. Do not enable live trading in another bot based on these checks.
