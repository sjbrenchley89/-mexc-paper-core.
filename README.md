# MEXC paper core

Start with [the numbered, no-code walkthrough](START-HERE.md).

Paper trading is the default and needs no exchange credentials. Automatic live trading exists but is locked: it cannot start until a preflight passes, including 30 days and 50 closed trades of paper trading, and then only after a typed confirmation. It was written from MEXC's published API pages and tested only against a simulated exchange; it has never run against MEXC. Default: Option C (TradingView, scanner, news veto), copy slot on but disconnected, BTCUSDT, $50 equity, $10 position ceiling, 0.5% planned risk per trade, 10% daily loss pause with manual review, and $25 floor. Tradovate and portal imports are refused.

Python 3.12+, standard library only. In this folder, type python bot.py init, then python bot.py status. On macOS/Linux use python3 if required. The full walkthrough explains each command and expected result.

No guaranteed returns. Synthetic demos are software checks, not trading evidence. Do not enable live trading in another bot based on these checks.
