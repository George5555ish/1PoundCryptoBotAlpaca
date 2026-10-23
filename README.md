# Crypto paper bot

Separate from the ETF bot in `trading-bot/`. This process suggests a BTC/USD or ETH/USD trade, then a Python risk gate decides whether that suggestion is allowed. The model does not choose a quantity and it does not place orders. Orders go to an Alpaca paper account only, and only if you start the process with `--send-orders`.

The market is treated as open 24 hours a day, including weekends. An open long may be held overnight. The daily loss clock is the UTC date, not the US equity session.

## Safety rules

| Rule | What the code does |
| --- | --- |
| £1 a day | Logged for awareness. It never forces or blocks a trade. |
| One open position | A second long in this universe is closed down to one. A working entry while flat occupies the slot. |
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
├── decision_log.py
├── notify.py
├── main.py
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

`OPENAI_API_KEY` is the Grok key from the xAI console. It starts with `xai-`. Leave the base URL as `https://api.x.ai/v1` and the model as `grok-4.7`.

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

Every suggestion, repair, rejection, dry run, and send is stored in `data/decisions.db` before any broker call. The only call to `execution.submit` is inside `_commit`, after an `approved_awaiting_send` row is inserted. If that insert fails, the order is not sent.

The same database stores `utc_day_equity`, the equity frozen for the current UTC date. That is the baseline for the 3% halt.

Text logs append to `data/bot.log`.

## Send paper orders

```powershell
python main.py --once --send-orders
python main.py --send-orders
```

Startup then says `MODE paper-send`. An order is sent only when the gate approves it, a second read still approves the same symbol, side, quantity, stop, and target, and the client is still on the paper host.

## Tests

```powershell
python -m unittest discover -s tests -q
python risk_gate.py
```

The unit tests do not call Alpaca or a model.

## Deploy on Railway

The image is a long-running worker. It does not listen on a port, so leave the public domain off. Keep the service at one replica. Two copies would both read the same paper account.

1. Push this directory. If the git root is the parent folder, set the Railway service root to `crypto-bot`.
2. Railway reads `railway.toml` and builds the `Dockerfile`. The start command is `python main.py`, which is a dry run.
3. Add the variables from `.env.example`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`, `OPENAI_API_KEY`, `OPENAI_BASE_URL=https://api.x.ai/v1`, `OPENAI_MODEL=grok-4.7`, and optionally `LOOP_INTERVAL_SECONDS`, `POUNDS_PER_USD`, `N8N_WEBHOOK_URL`.
4. Add a volume mounted at `/data` and set `DATA_DIR=/data`. The database, `bot.log`, and chart PNGs are written there. Without the volume, a redeploy deletes `decisions.db`, including the UTC day-start equity used for the 3% halt.
5. Confirm the logs show `MODE dry-run` and `DECISION_RECORD` rows. When those look right, change the start command to `python main.py --send-orders`. Startup then says `MODE paper-send`. The client still uses the paper host only.

Do not paste `.env` into the image. Railway variables are the secrets. `N8N_WEBHOOK_URL` can stay unset; the cycle logs a warning and continues.
