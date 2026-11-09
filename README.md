# Crypto paper bot

Separate from the ETF bot in `trading-bot/`. This process suggests a BTC/USD or ETH/USD trade, then a Python risk gate decides whether that suggestion is allowed. The model does not choose a quantity and it does not place orders. Orders go to an Alpaca paper account only, and only if you start the process with `--send-orders`.

The market is treated as open 24 hours a day, including weekends. An open long may be held overnight. The daily loss clock is the UTC date, not the US equity session.

## Safety rules

| Rule | What the code does |
| --- | --- |
| £1 a day | Logged for awareness. It never forces or blocks a trade. |
| Five open positions | A new entry is rejected when five longs are already open. The same symbol is not entered again while it is open. A working buy occupies one slot for that symbol. |
| 24/7 | No session close and no end-of-day flatten. |
| 1% risk | Quantity is sized from the stop so a fill at the stop loses at most 1% of current equity. |
| 3% daily loss | Measured from equity frozen at the first look of the UTC date. New entries stop until the next UTC date. Closes still run. |
| Overnight holds | Allowed, as long as a protective sell is working. |
| Symbols | BTC/USD and ETH/USD only. |
| Long only | A short suggestion is rejected. Alpaca crypto orders cannot open a short. |

Alpaca crypto orders are simple orders. There is no bracket. An approved entry is a market buy (IOC) followed by a good-till-cancelled stop-limit sell. If that stop cannot be placed, the process closes the position. A long found later with no working sell is closed on the next cycle. A gap through the stop-limit can still lose more than 1%.

This bot does not close equity positions. If the same paper account already holds a stock or another coin, new crypto entries are blocked.

## Layout

```
crypto-bot/
├── .env.example
├── Dockerfile            # image used by Railway
├── railway.toml          # start command and restart policy
├── config.py
├── risk_gate.py
├── ai_decision.py
├── scanner.py
├── strategy.py
├── execution.py
├── positions.py
├── decision_log.py
├── notify.py
├── dashboard.py
├── main.py
├── multi_runner.py
├── equity_scan.py
├── strategies/
├── requirements.txt
└── tests/
```

## Setup

From this directory, with Python 3.10 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Put the paper key id in `ALPACA_API_KEY` and the paper secret in `ALPACA_SECRET_KEY`. `ALPACA_PAPER` must stay `true`.

`OPENAI_API_KEY` is an OpenAI key from https://platform.openai.com/api-keys. It starts with `sk-`. Leave the base URL as `https://api.openai.com/v1` and the model as `gpt-4o`. `--multi-strategy` does not call the model; the enabled rules in `MULTI_STRATEGY_IDS` decide the trade.

## Dry run

One cycle. Reads the paper account and crypto quotes, asks the model, and logs the gate's decision. Sends nothing:

```powershell
python main.py --once
```

Leave it looping, still sending nothing:

```powershell
python main.py
```

## Decision audit

Every suggestion, position review, repair, rejection, dry run, and send is stored in `data/decisions.db` before any broker call. The only call to `execution.submit` is inside `_commit`, after an `approved_awaiting_send` row is inserted. If that insert fails, the order is not sent.

Each row has `action_type`: `entry_evaluation` for a new trade, or `position_review` for an open long. A review stores the symbol, current price, unrealized P&L, the decision (`hold` or `close`), and the reason.

The same database stores `utc_day_equity`, the equity frozen for the current UTC date. That is the baseline for the 3% halt.

Text logs append to `data/bot.log`.

## Send paper orders

```powershell
python main.py --once --send-orders
python main.py --send-orders
```

Startup says `MODE paper-send`, and every cycle logs that same mode line. Without the flag the line is `MODE dry-run` and no order is sent. An order is sent only when the gate approves it, a second read still approves the same symbol, side, quantity, stop, and target, and the client is still on the paper host.

The flag cannot be turned on from an environment variable. Railway stays a dry run until the start command is `python main.py --send-orders`.

## Open positions

Each cycle reads the open positions from Alpaca. For each long the process logs a hold or a close:

- Close when the candle trend for that symbol is `lower lows`.
- Close when the last price is at or below the stop stored on the original entry, or at or above that entry's target.
- Otherwise hold.

The entry checks are higher highs, a pullback, and a last close more than 0.5% below the 30-bar high. A prominent swing-high rejection (a long upper wick, or the next candles closing back below that high) needs a clearer pullback before a long is allowed. The filter never opens a short. A close still goes through the risk gate. In a dry run the close is logged as `dry_run` and is not sent. A model suggestion to exit a position the review just held is logged as `no_trade`.

## Trade notifications

A submitted entry posts `event_type=trade_opened` on that symbol's webhook row. A submitted close posts `event_type=trade_closed`. The row includes symbol, side, quantity, price, stop, target, reason, and `trade_description`, a short sentence that explains the order. An approved dry run gets the same sentence and notes that the paper order was not sent. A failed webhook does not stop the cycle.

Set `TRADE_NOTIFY_EMAIL` and `SMTP_HOST` to also send that event by email. If the address is set and SMTP is not, the email is skipped and the webhook row is unchanged.

## Multi-strategy paper tester

`--multi-strategy` is an extra mode on this process. The default crypto loop is unchanged.

It scans a configurable list in `MULTI_STRATEGY_SYMBOLS` (currently `IWM`, `QQQ`, `DIA`). Add `GLD`, `SLV`, or `USO` from `COMMODITY_SYMBOLS` when commodities should be included.

Each cycle runs the enabled rule-based strategies in `strategies/`. The default enabled set is `MULTI_STRATEGY_IDS` (`anchored_vwap_mean_reversion`, `ema_stack_pullback`). Other catalogued rules stay on disk and can be re-enabled with `--strategies` or by editing `MULTI_STRATEGY_IDS`. No model is used for the decision. The first non-hold per symbol is sent through the same Risk Gate (1% risk, 3% daily loss, max three positions, daily profit lock at +1.5%). One open position per symbol. Dry-run is still the default.

When UTC-day P/L reaches +1.5% of `day_start_equity`, new entries stop for the rest of that UTC day (`daily profit lock: +1.5% reached`). If P/L later falls below +0.8% after that lock, every open stock/ETF is flattened and the day stays halted.

Each open stock/ETF is also checked against average entry. If unrealized P/L reaches `-POSITION_LOSS_CLOSE_FRACTION` (default **-2% from entry**), that name is closed immediately through the Risk Gate. Change the fraction in `config.py`. This does not replace the 1% equity stop or the 3% daily loss halt.

US cash hours are 09:30–16:00 America/New_York, weekdays, and the Alpaca clock must not be closed (holidays and early closes count). Stock brackets are DAY orders, so they expire at the bell:

- During the session: scan, enter, and review as usual, every `LOOP_INTERVAL_SECONDS`.
- Last 20 minutes before the close: no new entries. Every stock/ETF position is flattened, including names left over from an older universe. The loop retries every 15 seconds until the book is flat. An early broker close, or leftover names after the bell, also flatten instead of idling. Crypto longs are left alone.
- Nights, weekends, and holidays with no leftover equity: no scan and no orders. A heartbeat webhook still posts every `LOOP_INTERVAL_SECONDS` (default 5 minutes) with one `session_idle` sheet row per symbol (`strategy_trades` is never empty). Sleep is capped so the process wakes before the next 09:30 America/New_York open.

```powershell
python main.py --once --multi-strategy
```

A subset of strategies (ids must be in `MULTI_STRATEGY_CATALOG`):

```powershell
python main.py --once --multi-strategy --strategies anchored_vwap_mean_reversion,ema_stack_pullback
```

Paper orders still need `--send-orders`. Stock entries use a day bracket on whole shares; Alpaca rejects a fractional bracket. Crypto entries stay on the original path.

Each decision row and webhook payload includes `strategy_id`, `symbol`, `action`, `reason`, `stop_price`, `target_price`, `approved`, `risk_gate_verdict`, and `final_action`. The cycle prints `MULTI_SUMMARY scanned=… signals=… approved=… rejected=… session=…`. Charts are drawn for symbols that produce a non-hold signal. A closed-session cycle still writes one `session_idle` hold row per symbol so n8n can Split Out `strategy_trades` onto the sheet.

## Tests

```powershell
python -m unittest discover -s tests -q
python risk_gate.py
```

The unit tests do not call Alpaca or a model.

## Deploy on Railway

The image is a long-running worker that also serves a small page on `PORT`. Generate a Railway public domain for that page. Keep the service at one replica. Two copies would both read the same paper account.

1. Push this directory. If the git root is the parent folder, set the Railway service root to `crypto-bot`.
2. Railway reads `railway.toml` and builds the `Dockerfile`. The start command is `python main.py --send-orders`.
3. Add the variables from `.env.example`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`, `OPENAI_API_KEY`, `OPENAI_BASE_URL=https://api.openai.com/v1`, `OPENAI_MODEL=gpt-4o`, and optionally `LOOP_INTERVAL_SECONDS`, `POUNDS_PER_USD`, `N8N_WEBHOOK_URL`.
4. Add a volume mounted at `/data` and set `DATA_DIR=/data`. The database, `bot.log`, and chart PNGs are written there. Without the volume, a redeploy deletes `decisions.db`, including the UTC day-start equity used for the 3% halt.
5. Confirm the logs show `MODE paper-send`. The sheet `mode` column should then say `live`. Orders still go only to the Alpaca paper host. A dry run uses the start command `python main.py`.
6. Open the public URL. `/` is the dashboard (equity, decision counts, last trades). `/api` is the same data as JSON. The last-five trades come from `DASHBOARD_TRADES_WEBHOOK` (default is the n8n call-agent URL). A failed fetch does not stop trading.

Do not paste `.env` into the image. Railway variables are the secrets. `N8N_WEBHOOK_URL` can stay unset; the cycle logs a warning and continues.
