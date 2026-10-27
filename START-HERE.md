# Start here: MEXC bot (paper first, live only after the checks)

This guide contains no programming code. Commands in ordinary text are things to type into your terminal.

**Delivered now:** a Python paper-only command-line core, private local webhook receiver, two built-in strategies, a ranked scanner, cost-aware simulated positions, persistent risk controls, historical CSV backtests, and local tests. No Python packages need to be installed.

**Added in the second installment:** a news veto (it blocks entries; blocks you record by hand, plus automatic blocks when MEXC marks a coin as not online), a copy-trading slot with both providers disconnected, and a CSV export with Australian dollar columns. See step 19.

**Added in the third installment:** fixes to the market-data code so it matches MEXC's published API (hourly candles are `60m`, exchange size rules, and the scanner's price-change units), and an automatic live-trading module (steps 15, 17 and 20). The live module was written from MEXC's published documentation and tested only against a simulated exchange. It has never been run against MEXC, and it cannot start until every check in step 20 passes, including at least 30 days and 50 closed trades of paper trading.

**Not delivered or enabled yet:** dashboard, an automatic news feed, any connected FOMO or Astral AI feed, Tradovate connection, or portal imports. Tradovate and portal settings cannot be enabled. The configuration file always says paper; live trading starts only through the separate live-start command.

1. **Understand the money limits — safe now.**

   Goal: set a defensible expectation before spending anything. A systematic strategy cannot reliably turn $50 into $10,000 in one week. An isolated lucky outcome is possible, but pursuing 200 times your money is gambling. Your acknowledged live limits of $50 total and $10 per position remain binding. The target is unattainable as a defensible plan within those limits.

   Expect cents or a few dollars of movement in an ordinary week of small spot positions, rather than thousands; losing money is a prudent baseline assumption for an unproven system after costs. There is no validated strategy evidence here from which to estimate an expected return or a percentage probability of total loss. The entire $50 can still be lost through extreme moves, custody failure, compromised keys, or execution failure. A $25 software floor is a halt threshold, not a guarantee that $25 remains recoverable.

   Copy trading does not establish an edge: you inherit the source's strategy risk and fees, and may buy after its price has moved. A TradingView subscription and hosting or news fees can exceed a small account's profits. Those bills are outside the trading ledger and must be included when deciding whether the project is worthwhile.

   A more aggressive bounded choice later is Option B, two positions, and 1% planned risk per trade, retaining all hard limits. At $50, that is at most $0.50 planned loss per trade before unforeseen gaps. Even a 5% favorable move on a $9.50 position is only about $0.48 before costs. This is arithmetic, not a return forecast. If your expectation still depends on reaching $10,000 next week, do not fund live trading.

2. **Choose the sources — safe now.**

   Goal: start with a setup that can be evaluated. You chose Option C: the TradingView technical strategy, the breakout scanner, and a news veto that can only block entries. Option A (TradingView only) and Option B (adds the scanner) remain available by editing option in config.json; news must then be false.

   Copy trading is switched on in settings, but both providers are disconnected, so nothing is copied automatically. Tradovate and portal strategies stay off. Your country is Australia and your account type is individual. Before any later connection, supply the exact FOMO and Astral AI URLs and permitted feed/API documentation, your exact country and account type, the intended Tradovate use, and your portal's entry, exit, and stop rules or source material. No portal recommendations are assumed.

   Expected result: config.json says mode paper, option C, news true, copy_trading true, and false for tradovate and portal_strategies. If it does not, restore those values before proceeding.

3. **Check regional and provider eligibility — safe now, before subscriptions or keys.**

   Goal: confirm that each service permits this use. Open each provider's official website yourself. For MEXC, read its current User Agreement and API rules and ask support whether your exact residence, legal entity, verification status, and account type permit automated spot API trading. EU, UK, and Asia-Pacific are not single eligibility categories. Country exclusions, identity verification, account permissions, and product restrictions can prevent access even if a login works. Never use a VPN or a different country selection to evade restrictions.

   For TradingView, open Pricing and compare plans for the explicit words webhook alerts. Essential has historically been the entry paid tier; its current name, price, and webhook entitlement were not verified here. Buy only a plan that currently lists the feature. Confirm its alert quota, two-factor authentication requirement, webhook ports, and delivery timeout in official Help.

   For Tradovate, obtain confirmation of resident eligibility, API entitlement and charges, automation permission, market-data licensing, and brokerage funding/margin requirements. It is a futures brokerage: it cannot act as this initial spot module. Whole futures contracts generally exceed $10 in notional exposure. Measuring only margin would evade the cap. A future separate module would need explicit futures acceptance, leverage at most 2 times, and the same shared exposure and loss checks; if no contract meets the limits, it must reject all orders.

   For FOMO and Astral AI, independently confirm their terms permit exporting and mirroring activity into MEXC. A connector displaying trade history is not proof that unattended external mirroring is permitted. No provider terms or current regional eligibility were successfully verified in this session. Keep these items unresolved and the connections disabled until verified. If official permitted access is unavailable, leave a disconnected adapter; no scraping or account-password sharing.

   For Australia: [AUSTRAC](https://www.austrac.gov.au) keeps a register of digital currency exchanges. Check it and ASIC's warnings list for MEXC yourself. This session could not confirm either way, and reports conflict. Ask MEXC support in writing whether an Australian individual may use automated spot API trading. If the answer is no or unclear, stay in paper mode. Buying USDT with AUD and moving it to MEXC has its own fees, which can take a real share of $50. The ATO treats each coin-to-coin trade as a capital gains event, so record keeping matters (step 19).

   Expected result: a saved record of eligibility and terms for each enabled service. If you cannot obtain it, continue offline paper testing only.

4. **Install Python — safe now.**

   Goal: install the program that runs the paper core. On Windows, visit python.org, select Downloads, download Python 3.12 or newer, run the installer, select Add Python to PATH, then Install Now. Open Start, type Command Prompt, and open it. Type python --version and press Enter. On macOS or Linux, open Terminal and type python3 --version; use python3 wherever this guide says python. Install Python from python.org on macOS or your Linux distribution's package manager if needed.

   Expected result: Python 3.12 or newer is displayed. If the command is not found, reopen the terminal after installation and repair PATH or use the Python launcher's py command on Windows. This project uses Python's included libraries; there is no dependency-install step.

5. **Open the first-stage project — safe now.**

   Goal: keep the bot's files together. In a web browser, open https://github.com/sjbrenchley89/-mexc-paper-core. and sign in. Use the branch menu to pick the branch you were told to use, click the green Code button, then Download ZIP. Choose Extract All on Windows or open the ZIP on macOS. Place the extracted folder somewhere private, such as Documents. Open that folder. On Windows, click File Explorer's address bar, type cmd, and press Enter. On macOS, open Terminal and type cd followed by a space, drag the extracted folder into Terminal, then press Enter.

   Expected result: the folder contains bot.py, config.json, risk.py, market.py, strategies.py, exchange.py, live.py, examples, tests, and this guide. If bot.py is absent, open the inner extracted folder instead.

6. **Initialize paper mode and inspect status — safe now.**

   Goal: create a pretend $50 account. Paper mode means simulated orders and balances; it does not spend exchange money. The command-line interface, or CLI, is the text window where you type commands instead of clicking dashboard buttons.

   Type python bot.py init. Then type python bot.py status. Look for mode paper, equity 50, empty positions, paused false, killed false, and review_required false. This creates paper.sqlite, the saved ledger. It never reads your real MEXC account.

   Type python bot.py demo to try a synthetic entry and exit in a separate temporary ledger. Expected result: synthetic_demo true and not_market_evidence true. The deliberately favorable demo is not market performance. Run status again: your main paper account should still be $50. If you get an error, retain paper mode and check that you are in the correct folder.

7. **Learn the safety controls — safe now.**

   Goal: know how to halt the bot before connecting alerts. Type python bot.py pause, then python bot.py status. Expect paused true. Type python bot.py resume; an ordinary pause should clear.

   A kill switch is the emergency control that blocks entries and requires review before restarting. Type python bot.py kill. Expect killed true and review_required true. Resume must now fail. To complete a review after this practice kill, type python bot.py review-rearm --note "Practiced emergency stop; still paper only". The quoted words are your recorded review note, not programming code.

   After an actual daily loss-limit breach, re-arm must also wait until the next UTC day. UTC is the worldwide reference clock used for daily accounting. Midnight alone never clears the pause. Below the $25 floor, re-arm is refused even with a review note; manual intervention is required. Restarting the process preserves all these flags.

   Protective sales of bot-owned paper positions remain permitted during safety halts. With the receiver running, its monitor attempts these exits at fresh prices. A stopped process or unavailable market data cannot execute a stop. If an expected safety flag does not appear, stop and do not proceed to live development.

8. **Check the settings — safe now.**

   Goal: understand the few values you may adjust. Open config.json with Notepad or another plain-text editor. The following are defaults; keep them for the initial trial.

   | Setting | Default | Meaning |
   |---|---|---|
   | mode | paper | Must stay paper. Real trading starts only through the separate live-start command. Changing this makes startup fail. |
   | option | C | A is one technical strategy; B adds the scanner; C adds the news veto. |
   | paper_cash | 50 | Starting simulated funds, used only for a new ledger. |
   | symbols | BTCUSDT | Explicit allowed spot pairs. Scanner ranks only this approved list; add verified liquid pairs later. |
   | total_cap | 50 | Maximum allocated purchase cost across positions, including entry fees. Values above $50 cannot raise the code limit. |
   | position_cap | 10 | Per-position ceiling. Entries reserve 5% headroom, so initial allocation is at most $9.50 by default. Marked exposure above $10 triggers an attempted paper exit. |
   | risk_per_trade | 0.005 | Planned stop loss, including estimated costs: 0.5% of marked equity, about $0.25 initially. May be adjusted up to 1%. |
   | daily_loss | 0.10 | Loss threshold: 10% of the day's starting marked equity, initially $5. Includes fees and unrealized losses. May be tightened, not raised above 10%. |
   | manual review | always required | Persistent safety latch. Not switchable off through configuration. |
   | floor | 25 | Whole simulated-ledger equity floor. Code always enforces at least $25. Real whole-account valuation is a later deliverable. |
   | max_positions | 1 | Maximum simultaneous positions. Later bounded aggressive setting: 2. |
   | fee | 0.001 | Assumed 0.1% per side; replace with independently verified actual fees. This is not a current MEXC fee claim. |
   | slippage | 0.001 | Assumed 0.1% worse price per side. Slippage means the actual fill differs from the quoted price. Stress-test larger values. |
   | min_paper_order | 1 | Additional simulation minimum. Current exchange minimums and quantity increments also have to pass. The bot skips small orders instead of increasing them above risk limits. |
   | copy_trading | true | Turns on the copy slot. Both providers stay disconnected; nothing is copied automatically. |
   | tradovate | false | No futures connection or orders. |
   | portal_strategies | false | No rules imported. |
   | news | true | News veto. Must be true for option C and false for A or B. No news provider is connected; blocks are yours plus automatic ones when MEXC marks a coin as not online. |
   | live | both false | Two confirmations you set to true yourself: MEXC eligibility confirmed in writing, and a trade-only key created. Needed before live-preflight can pass. |

   ATR means Average True Range: a measure of recent price movement that accounts for gaps. The core computes 14-period ATR from closed one-hour candles and sizes positions so a stop two ATR below entry fits the planned loss budget. It takes the smaller of that size, available paper cash, and the hard caps. Fees and slippage count toward sizing. Actual gaps can exceed the planned loss.

   Expected result: status reflects the same paper ledger after a restart. Changing paper_cash does not overwrite an existing ledger. Do not delete the ledger to clear safety pauses.

9. **Understand the two strategies — safe now.**

   Goal: know exactly what can trigger a simulated trade. Both strategies buy spot only and sell only positions recorded by this bot; they never short, borrow, or use leverage. Only closed one-hour candles are used.

   Strategy 1, technical: buy when the 9-period exponential moving average crosses above the 21-period average, RSI is between 50 and 65, and the MACD histogram is positive. Moving averages smooth price trends; RSI measures recent strength on a 0–100 scale; MACD compares averages to measure momentum. Sell on a downward 9/21 crossover, RSI below 45, the fixed two-ATR stop, or the four-ATR profit target. TradingView triggers an alert; the bot independently recomputes indicators from MEXC candles instead of trusting supplied indicator numbers.

   Strategy 2, breakout: buy when the close exceeds the highs of the previous 20 candles and volume is at least twice the previous 20-candle average. Sell below the previous 10-candle low or at the same stop and target. Scanner candidates must have at least one million USDT of 24-hour volume, a 1–20% positive daily change, ATR between 0.3% and 3% of price, spread at most 0.2%, and at least $1,000 of depth on each side within 0.5% of the midpoint. Spread is the difference between the best buy and sell quotes; depth measures available liquidity. Candidates are ranked by volume spike and daily change, penalized for wider spread.

   Strategy 2 is disabled in Option A. Strategy 3, the news veto, never buys. While a block is active for a symbol (or for all symbols), new entries from every source are rejected, including TradingView, scanner and copy signals. It does not sell anything you already hold; protective exits still work as before. Blocks expire after 1 to 168 hours. You record a block yourself after reading an official notice, such as a delisting or maintenance announcement on MEXC. The running program also adds a one-hour block by itself whenever MEXC's exchange information shows the coin is paused or offline, and renews it while that lasts. Nothing reads announcements or news automatically, so beyond that the veto is only as good as your blocks. No portal recommendation is included. Imported strategies later require explicit entry, exit, stop, eligible instruments, timeframe, version, and approval evidence.

10. **Create the webhook secret — safe now.**

    Goal: allow only your own alerts into the receiver. A webhook is an automatic web message sent when an alert fires. A shared secret is a random token both your alert and receiver know; it is not an exchange API key.

    Type python bot.py setup. Expect a message that .env was created. This private file supplies WEBHOOK_SECRET to the process as an environment variable, a named setting available to a running program. The receiver also accepts a WEBHOOK_SECRET set directly in your operating system or host's secrets manager.

    Open .env locally, copy only the value after WEBHOOK_SECRET= for your own TradingView indicator input, and close the file. Do not paste it into chat, publish it, or include it in screenshots. On shared Windows computers, restrict the folder's Security permissions to your user. On a host, use the host's Secrets/Environment Variables screen: add WEBHOOK_SECRET as the name and the random token as the value, save, and restart the service. Never place an exchange key in an alert.

    If setup says the file exists, use your existing secret; it will not be overwritten. If it has been exposed, rotate it locally and update the TradingView alert before restarting.

11. **Start the local receiver — safe now.**

    Goal: receive alerts and monitor paper exits. Type python bot.py start. Leave that terminal open. Expect PAPER ONLY and the local webhook address http://127.0.0.1:8787/webhook. Open another terminal in the same folder for status and safety commands. The process must stay running to check simulated stops.

    Visit http://127.0.0.1:8787/health in your browser. Expect mode paper and live_enabled false. If the page fails, check that the terminal is still running; if the port is occupied, stop the other program or use the documented start --port option and change your tunnel target accordingly.

    The receiver acknowledges accepted messages as queued, not filled. It checks their secret, payload fields, unique ID, permitted symbol, timeframe, timestamps, and latest closed candle, then confirms the strategy against official MEXC public data. Alerts older than two minutes are rejected. Duplicate IDs cannot create a second paper position. Invalid requests cannot access live orders because no live order implementation exists.

12. **Expose only the webhook to TradingView — safe paper testing, subscription may cost money.**

    Goal: let TradingView reach your local receiver over an encrypted HTTPS address. TradingView cannot reach your private 127.0.0.1 address directly. One simple option is an HTTPS tunnel, a service that forwards a public URL to your computer.

    For an ngrok tunnel, visit its official site, create your own account, select its installation instructions for your operating system, download and install its client, and follow the account's displayed authentication-token command locally. Do not send that token to anyone. In a second terminal, type ngrok http 8787. Expect a Forwarding address beginning with https. Copy that address and append /webhook for TradingView. Leave both the bot and tunnel running. If there is no Forwarding address, follow ngrok's displayed account error before creating alerts. Free tunnel addresses may change after restart; update the alert URL when they do.

    Tailscale can later protect a private dashboard, but an ordinary private Tailscale address cannot receive TradingView webhooks. A public ingress feature needs its own configuration and terms checks; installing Tailscale alone is insufficient.

    A hosted alternative needs a long-running Python service, HTTPS ingress forwarding only /webhook, persistent storage for paper.sqlite, your webhook secret in its secret store, and a tested restart procedure. Hosting price and sleep limits must be checked independently. This installment has not been deployed. Do not expose a future control dashboard through the public webhook tunnel.

    Expected result: visiting the tunnel's /health address reports paper-only. If it fails, test local /health first, then confirm the tunnel targets port 8787. No public remote control endpoint exists in this stage.

13. **Create the TradingView alert — safe paper testing.**

    Goal: send technical signals in the receiver's agreed format. Upgrade only after confirming current webhook entitlement. Enable two-factor authentication in TradingView's account security settings if required. Open a chart for MEXC BTCUSDT and choose the 1-hour timeframe. Open Pine Editor, open the supplied examples/TradingView.pine file on your computer, copy its contents into that editor, select Save, then Add to chart. This is copying a supplied file, not writing a program yourself. The Pine template has not been compiled in TradingView here; stop and report any compiler error.

    Open the indicator's Settings, set Webhook secret to your locally generated token, and confirm. Click Create alert, select the indicator, and choose Any alert() function call. In Notifications, enable Webhook URL and enter your HTTPS tunnel address ending /webhook. Select Create. If the webhook field is missing, revisit your plan and two-factor settings.

    The supplied alert template sends the webhook-only secret, a unique candle/action ID, BTCUSDT, buy or sell, strategy technical, timeframe 1h, the send time, and the candle's opening time. Both times use Unix seconds, a numerical timestamp format. A readable example is in examples/alert.json; its zero timestamps and placeholder secret must not be sent unchanged. Indicator values and account credentials are never included.

    Expected result: the alert appears active. A natural signal may take days; no trade is forced simply to test the connection. When one arrives, watch TradingView's alert log and the bot terminal, then run status. A technical confirmation or exchange-filter rejection is a valid protective outcome. If official market data is unavailable, no entry should occur. Symbols with missing minimum-size rules are rejected rather than guessed. Pine and local indicator history can differ slightly, so an alert can fail independent confirmation.

14. **Backtest and paper-trade before considering live — safe now.**

    Goal: measure costs and losses on data not selected to look good. A backtest replays historical prices; it is not proof future trades will succeed.

    Type python bot.py download-history --output history.csv. If successful, expect saved history.csv and a count of closed bars from official public MEXC data. The initial downloader fetches up to 1,000 bars, not months of paginated history. It is an exploratory sample. If the network or API refuses the request, do not bypass restrictions. This task's environment does not permit the MEXC destination, so this connection was not verified here.

    Type python bot.py backtest --csv history.csv --strategy technical. For the second strategy, type python bot.py backtest --csv history.csv --strategy breakout. Expect equity, return percentage, maximum drawdown, fees, and closed-trade count. Drawdown is the decline from a previous equity peak. The backtester enters at the next candle's opening price and treats intrabar stop behavior conservatively. It lacks historical order-book liquidity, exchange-rule history, and tick-level fills, so these results alone cannot qualify live use.

    Before live development, collect several months spanning rising, falling, and flat markets; reserve a later period that was not used to tune rules; use at least 30 days of continuous real-market paper operation and enough completed trades to assess costs. A practical minimum is 50 completed paper trades, but no count guarantees statistical confidence. Require positive net performance on untouched data under larger fee/slippage assumptions, tolerable drawdowns, working safety halts, and no unresolved duplicate, stale-data, or reconciliation failures. If a safety breach occurs, keep the pause and review it; never clear the ledger to improve the report.

    For copied trades later, record provider, original signal time, receipt time, fill time, and source strategy/version, and compare paper returns with the provider's reported returns after latency and costs. Provider screenshots alone are insufficient. Portal strategies and any news strategy require their own historical and paper evidence.

15. **Create the MEXC key and store it safely — real-account security, wait.**

    Goal: give the bot the minimum permission it needs. An API key is a machine credential that lets software act on an account without using your login password. Do this only after steps 1 to 14 are done and MEXC has confirmed in writing that you may use automated trading from Australia.

    Sign in to MEXC yourself, open your profile's API Management page, and choose Create API. Name it for this bot. Enable only account read, spot trade read and spot trade write (MEXC's permission names are SPOT_ACCOUNT_READ, SPOT_DEAL_READ and SPOT_DEAL_WRITE). Do not enable withdrawals, transfers, margin, or futures. Bind the key to the fixed internet address of the computer that runs the bot, if MEXC offers it. Complete your own two-factor checks. The program reads the key's permission list and refuses to start if it finds any withdrawal, transfer or futures permission.

    Store the key and secret as two environment variables named MEXC_API_KEY and MEXC_API_SECRET. An environment variable is a named setting your computer hands to programs. On Windows, open Start, type "Edit environment variables for your account", open it, choose New, and add each name with its value, then close and reopen the terminal. On macOS or Linux, add an export line for each to your shell's startup file, then open a new terminal. Never put them in a file in this folder, and never paste them into chat, TradingView, FOMO, Astral AI, or anyone else's tool. If a key is exposed, delete it on MEXC and make a new one.

    Use a MEXC account that holds only the bot's budget, no more than 50 USD. An API key does not limit spending by itself, so the program checks the whole account: it refuses to start, or halts if already running, when it sees other coins or more than about 55 USD.

    Expected result: step 20's preflight shows the key as valid, with the right permissions. If MEXC will not let you leave out the unneeded permissions, stop.

16. **Add the dashboard and optional modules after the CLI works — deferred.**

    Goal: make controls easier without introducing a second risk engine. The future dashboard must call the same control layer as the CLI and show status, open positions, equity, daily profit/loss versus the 10% threshold, distance to $25, data age, and the kill switch. The daily breach review note and explicit re-arm must be available there. Keep it authenticated and private.

    All future signal adapters must use one shared ledger across venues, reserve outstanding orders, log source and latency, and obey $50 total exposure, $10 per position, the account floor, and the manual-review latch. They may not submit directly to a broker. Futures exposure must count notional, not just margin; dollars committed to separate accounts still count toward the same total budget. Until an official feed and its permitted unattended use are confirmed, a FOMO/Astral adapter remains disconnected. Until you supply portal rules, its import path remains empty. The news veto stays manual until a provider is chosen, its coverage and recurring costs are known, and its terms permit this use.

    Expected result: optional modules cannot trade simply because an account is connected. Each requires explicit selection, qualification evidence, and the same risk checks. The copy slot and manual news veto exist (step 19) and obey those checks; no feed, Tradovate or portal interface is implemented.

17. **Go-live checklist — real money, wait.**

    Goal: start automatic real trading only when every item is true. The program checks the ones marked (program). The rest are yours.

    - (program) You have set both live confirmations in config.json to true, which you may do only after MEXC support confirmed in writing that an Australian individual may use automated API trading and after you made a trade-only key.
    - (program) At least 30 days since the first paper trade, at least 50 closed paper trades, positive net result after costs, and no active paper safety halt.
    - (program) Key valid, with only the needed permissions, no withdrawal, transfer or futures permission, and a computer clock within 3 seconds of MEXC. A missing IP binding only warns.
    - (program) The account holds only USDT and the allowed coin, with a value between 25 and 55 USD. Each allowed coin is online. A signed test order that MEXC checks without trading is accepted.
    - (program) Always in force: 50 USD total, 10 USD per position, no leverage, spot only, 10% daily loss pause with a written review and next-day wait, 25 USD floor, news veto, duplicate-order protection, one live process at a time. These cannot be raised from the configuration file.
    - (you) TradingView plan, webhook entitlement and cost checked; terms checked for any copy provider you use.
    - (you) You understand that local stop-losses only work while the program, computer and internet are running. There are no stop orders at the exchange.
    - (you) You understand the AUD record requirements in step 19 and have a tax agent's guidance.
    - (you) You review costs and results weekly. A weekly review does not raise the 50 and 10 USD ceilings; raising them needs a separate explicit code change.

    Expected result: the live-preflight command in step 20 lists every check and says ready or not. If anything fails, stay in paper mode.

18. **Know what each file does — safe now.**

    Goal: know where to look without changing program internals. bot.py is the launcher and webhook receiver. risk.py owns the ledger, sizing, caps, pauses, exits, order intents and the news veto. exchange.py is the signed MEXC client. live.py turns order intents into real orders, settles them from MEXC's own records, and runs the go-live checks. copytrade.py holds the disconnected copy slot and its report. records.py writes the Australian-dollar CSV. market.py reads only official public MEXC spot data. strategies.py contains the two defined indicator rules. config.json contains ordinary settings. paper.sqlite saves simulated balances, unique alert IDs, review notes, and events. .env stores the private webhook token locally; it is never uploaded to GitHub. examples contains supplied alert files. tests contains the local safety checks. docs contains the staged implementation scope.

    Type python -m unittest discover -s tests -v to run the local checks. Expected result: 163 tests pass. These checks, a local HTTP authentication check, and synthetic backtest runs passed in the task environment. Current MEXC connectivity, TradingView compilation and delivery, provider terms, and profitability were not verified.

19. **Use the news veto, copy slot, and AUD records — safe now.**

    Goal: try the three added features in paper mode. Type each command in the project folder, using python3 if needed.

    News veto. After reading an official notice yourself, type python bot.py news-block --symbol BTCUSDT --hours 6 --reason "Delisting notice on MEXC announcements". Use * instead of BTCUSDT to block every symbol, for example during an exchange incident. Expected result: a confirmation with an expiry time. Type python bot.py news-list to see active blocks. Remove one early with python bot.py news-clear --symbol BTCUSDT --note "Checked the notice; not applicable". A written note is required because removing a veto lowers protection. Blocks last 1 to 168 hours and survive restarts. If you see "News veto is off", config.json is not set to option C.

    Copy slot. Type python bot.py copy-status. Expected result: both fomo and astral show connected false and automatic_copying false. To measure how late a copied signal arrives, you may pass one signal you saw yourself through the same risk checks: python bot.py copy-signal --provider fomo --id any-unique-name --symbol BTCUSDT --action buy --seconds-ago 20. The seconds-ago value is how long ago the source made the trade. Signals older than 120 seconds are rejected, and the rejection is recorded. This command needs access to MEXC public data. Type python bot.py copy-report to see entries, closed trades, paper profit or loss, average delay, and rejections for each provider. Compare these with the provider's own reported results yourself. Do not give either provider your MEXC keys or your own passwords.

    AUD records. Type python bot.py export-records --output records.csv --flat-rate 1.55, replacing 1.55 with a rate you have sourced. For a rate that changes by day, make a file with two columns, date and aud_per_usdt, one row per UTC date on which you traded, and use --rates rates.csv instead. The command refuses if a trading date has no rate; it never guesses. Without either option the AUD columns are blank. The file lists each buy and sell with fees, cost base, proceeds, gain, days held, and the Australian tax year, which runs 1 July to 30 June. It never overwrites an existing file. It covers only the coin legs recorded by this bot. Keep your AUD to USDT purchases and exchange statements separately. Paper trades are simulated and are not tax records. Confirm treatment with a registered tax agent.

    Expected result: each command prints a result or a plain error and changes nothing else. If the copy-signal command reports that MEXC public data is unavailable, no trade was made.

20. **Automatic trading — real money, only after step 17.**

    Goal: let the bot trade by itself within its limits, and know exactly how to stop it.

    See what is left, any time. Type python bot.py doctor. It lists every requirement with done or not done, shows your paper-trading progress (days and trades so far), and prints the one next step to take. It checks only your own computer and settings, contacts nothing, and trades nothing. Run it as often as you like.

    Check readiness. When doctor says you are ready, type python bot.py live-preflight. Expected result: a list of checks, each passed or failed with a reason, and ready true or false. Nothing is traded; the only order sent is a test order that MEXC validates without executing. Failing is normal until the 30-day, 50-trade paper trial is complete. Fix each failed line as described, then run it again.

    Start. When ready is true, type python bot.py live-start. The program checks everything again, shows a real-money warning, and asks you to type an exact sentence. Type it exactly. It then starts the webhook receiver, scanner, news veto, and the live trader. Keep that terminal open. It always asks for the sentence again after any restart; it never resumes real trading by itself. This is deliberate.

    What it does by itself. Every 5 seconds it reads your MEXC account and syncs its records to the real balance. Signals from TradingView, the scanner and any copy relay go through the same checks as in paper mode, then become market orders for at most 10 USD. It confirms each fill with MEXC and records the real price and fee. Stops and profit targets sell at market while the program runs. It checks every 5 minutes whether MEXC still lists the coin as online and blocks entries if not. It cancels an entry if trading was halted, a news block began, the signal is more than 2 minutes old, or the price rose more than 0.5% before the order was sent. If an order's outcome is unclear after a network failure, it asks MEXC for the order by its own ID before doing anything else, and never sends it twice.

    What makes it halt for your review. A daily loss of 10%, equity below 25 USD, a balance that does not match its records, other coins in the account (including any it cannot put a price on), more than about 55 USD in the account, an order it cannot confirm after 2 minutes, or five failed attempts to exit. Halted means no new entries and an attempt to sell what it holds. To resume, use the review-rearm step from step 7 with --live in front, for example python bot.py --live review-rearm --note "Reviewed; reason explained".

    Controls from a second terminal. python bot.py --live status shows the real account state. python bot.py --live pause and resume work as in paper mode. python bot.py --live kill stops entries and sells open positions within about 15 seconds, while the program is running. If the program is not running, sell on the MEXC website yourself.

    What can still go wrong. This module was written from MEXC's published documentation and tested only against a simulated exchange. It has never been run against MEXC, so unexpected behavior is possible, and the first real orders are the first real test. Watch the first trades by hand and compare the bot's record with your MEXC order history. There are no stop orders at the exchange, so a stopped program, a lost internet connection or a fast price gap can lose more than planned. Fees paid in other coins are estimated. Treat the 50 USD as money you can lose entirely.

    Safety against mix-ups. Only one live program can run on a computer at a time: it holds an operating-system lock for as long as it is alive, and a second live-start refuses to run. If a sale is refused or only partly fills, the program remembers the decision to sell and retries after a short wait. A sell signal that arrives while a buy is still being confirmed is held and applied as soon as the buy is confirmed. A ledger file remembers whether it is paper or live. The program refuses to open a live file in paper mode, or a paper file with --live, so a mistyped command cannot change your real records. The status page on port 8787 also says which mode is running.

    Records. python bot.py --live export-records --output live-records.csv --rates rates.csv writes the Australian record file from real fills, as in step 19.

    Expected result: live-preflight prints ready true only when every line passes, and live-start refuses to run otherwise. If you ever feel unsure, type python bot.py --live kill.

