"""Risk parameters and environment loading for the crypto bot.

This file is the single place the limits live. The risk gate reads them and
does not keep a second copy. A prompt cannot change a number in here.

This process is separate from the equity bot. It trades BTC/USD and ETH/USD
only, around the clock. Overnight and weekend holds are allowed. There is no
session close and no forced flatten.

Two different notions of equity are used on purpose:

- Per-trade risk uses *current* equity. Quantity is chosen so a stop filled
  at the stop price loses no more than 1% of the equity we can see right now.
- The daily loss halt uses equity recorded at the first observation of the
  current UTC date. That baseline stays fixed until the next UTC midnight
  instead of shrinking after every loss. Alpaca's ``last_equity`` follows the
  US equity session, so it is not the crypto day's starting equity.

The £1 figure is not one of those limits. ``format_soft_target_note`` only
builds a sentence for the log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


# V1 trades these two pairs and nothing else. The scanner, the prompt, and
# the gate all share this tuple. If the book ever holds more than the max,
# the earlier symbol in this tuple is the one we keep.
ALLOWED_SYMBOLS: tuple[str, ...] = ("BTC/USD", "ETH/USD")

# Concurrent longs in this universe. A working buy in a symbol we do not
# already hold occupies one of these slots. Locked at 5.
MAX_OPEN_POSITIONS: int = 5

# Daily loss resets on the UTC date, because the crypto market does not close.
UTC_DAY_NAME = "UTC"

# One cent. USD crypto prices, budgets, and the protective limit are
# quantized to this unit. BTC and ETH dollar prices trade in cents on Alpaca.
CENT = Decimal("0.01")

# Smallest quantity step the gate will send. Alpaca accepts finer increments
# on these pairs; flooring to 1e-8 keeps the planned loss from rounding up.
QTY_QUANTUM = Decimal("0.00000001")

# 1% of current equity. The dollar budget is rounded DOWN.
MAX_RISK_FRACTION = Decimal("0.01")

# 3% of UTC-day-start equity. At or beyond this, new entries halt until the
# next UTC date. An open long can still be closed.
DAILY_LOSS_FRACTION = Decimal("0.03")

# No leverage. Notional cannot exceed the smaller of equity and buying power.
MAX_POSITION_FRACTION = Decimal("1")

# A stop closer than this to the ask is rejected rather than sized into a
# huge coin amount. 0.15% is wide enough to avoid a one-tick stop on BTC.
MIN_STOP_FRACTION = Decimal("0.0015")

# Extra distance assumed against us on the entry, so the sized quantity still
# fits the 1% budget if the fill is slightly worse than the ask. The same
# fraction is where the protective stop-limit's limit price sits below the stop.
SLIPPAGE_ALLOWANCE_FRACTION = Decimal("0.0005")

# An entry quote wider than this, or older than this, is not tradable.
# Exits do not use the quote.
MAX_SPREAD_FRACTION = Decimal("0.002")
MAX_QUOTE_AGE_SECONDS = 30
MAX_QUOTE_FUTURE_SKEW_SECONDS = 5

# Alpaca rejects crypto orders below about one dollar of notional.
MIN_NOTIONAL_USD = Decimal("1")

# Awareness only. The gate does not import this name.
SOFT_DAILY_PROFIT_TARGET_GBP = Decimal("1")

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"

DEFAULT_LOOP_INTERVAL_SECONDS = 60
MIN_LOOP_INTERVAL_SECONDS = 15
DEFAULT_DASHBOARD_PORT = 8080
DEFAULT_DASHBOARD_TRADES_WEBHOOK = (
    "https://zyraloft.oph.st/webhook/10410755-f2e7-4ab8-a19c-18190f025b07/call-agent"
)

# Candle context for the model. Thirty 5-minute bars is two and a half hours.
# The fetch window is slightly longer so a bar that is still forming does not
# leave the summary short. The model receives a summary of these bars, not
# the bars themselves.
CANDLE_MINUTES = 5
CANDLE_BARS = 30


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"invalid risk configuration: {message}")


_require(MAX_OPEN_POSITIONS == 5, "V1 allows five open positions")
_require(MAX_RISK_FRACTION == Decimal("0.01"), "per-trade risk is 1% of current equity")
_require(DAILY_LOSS_FRACTION == Decimal("0.03"), "daily loss halt is 3%")
_require(MAX_POSITION_FRACTION == Decimal("1"), "V1 does not use leverage")
_require(Decimal("0") < MIN_STOP_FRACTION < MAX_RISK_FRACTION, "min stop fraction")
_require(Decimal("0") < SLIPPAGE_ALLOWANCE_FRACTION < MIN_STOP_FRACTION, "slippage fraction")
_require(Decimal("0") < MAX_SPREAD_FRACTION < Decimal("0.01"), "spread fraction")
_require(MIN_NOTIONAL_USD == Decimal("1"), "minimum notional is one dollar")
_require(SOFT_DAILY_PROFIT_TARGET_GBP == Decimal("1"), "soft target is £1")
_require(ALLOWED_SYMBOLS == ("BTC/USD", "ETH/USD"), "universe is BTC/USD and ETH/USD")


def normalize_symbol(value: str) -> str:
    """Return the slash form used by the gate.

    Alpaca reports the same pair as ``BTC/USD`` and sometimes ``BTCUSD``.
    Both become ``BTC/USD``. Anything else is returned uppercased so the
    gate can reject it as outside the universe.
    """

    text = value.strip().upper().replace(" ", "").replace("-", "/")
    if "/" not in text and text.endswith("USD") and len(text) > 3:
        text = text[:-3] + "/USD"
    return text


def is_allowed_symbol(value: str) -> bool:
    return normalize_symbol(value) in ALLOWED_SYMBOLS


def require_interval(seconds: int) -> int:
    if seconds < MIN_LOOP_INTERVAL_SECONDS:
        raise RuntimeError(
            f"loop interval must be at least {MIN_LOOP_INTERVAL_SECONDS} seconds, got {seconds}"
        )
    return seconds


def format_soft_target_note(day_pnl_usd: Decimal, pounds_per_usd: Decimal | None) -> str:
    """Describe the £1 figure for a human reading the log.

    The text is not an instruction to the gate. A missing FX rate does not
    block anything.
    """

    base = (
        "Soft daily profit target is £1 and is awareness only. "
        "It does not force a trade, and it does not block a trade the risk rules already allow."
    )
    if pounds_per_usd is None:
        return base + " No pounds-per-USD display rate is set, so progress toward £1 is not shown."
    pnl_gbp = (day_pnl_usd * pounds_per_usd).quantize(CENT, rounding=ROUND_HALF_UP)
    return (
        f"{base} Display-only day P/L is £{pnl_gbp} "
        f"using {pounds_per_usd} pounds per USD. That conversion is not used for sizing or for the daily loss halt."
    )


def data_directory(environ: dict[str, str] | None = None) -> Path:
    """Directory for the decision database and the log file."""

    source = os.environ if environ is None else environ
    raw = source.get("DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "data"


def decisions_db_path(environ: dict[str, str] | None = None) -> Path:
    """SQLite file for the decision audit and the UTC day-start equity."""

    source = os.environ if environ is None else environ
    override = source.get("DECISIONS_DB", "").strip()
    if override:
        return Path(override)
    return data_directory(source) / "decisions.db"


def assert_paper_trading_url(url: str) -> None:
    """Refuse any trading host other than Alpaca paper."""

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "paper-api.alpaca.markets":
        raise RuntimeError(f"trading URL must be the Alpaca paper host, got {url!r}")


def _mask(secret: str) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= 4:
        return "****"
    return f"{secret[:2]}…{secret[-2:]}"


@dataclass(frozen=True, repr=False)
class Settings:
    """Secrets and other values that come from the environment.

    Risk percentages are not fields here. Putting them in the environment
    would make it too easy to loosen a limit without reviewing the gate.
    """

    alpaca_api_key: str
    alpaca_secret_key: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    loop_interval_seconds: int
    pounds_per_usd: Decimal | None
    n8n_webhook_url: str | None
    trade_notify_email: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    dashboard_trades_webhook: str | None
    dashboard_port: int

    def __repr__(self) -> str:
        return (
            "Settings("
            f"alpaca_api_key={_mask(self.alpaca_api_key)}, "
            f"alpaca_secret_key={_mask(self.alpaca_secret_key)}, "
            f"openai_api_key={_mask(self.openai_api_key)}, "
            f"openai_base_url={self.openai_base_url}, "
            f"openai_model={self.openai_model}, "
            f"loop_interval_seconds={self.loop_interval_seconds}, "
            f"pounds_per_usd={self.pounds_per_usd}, "
            f"n8n_webhook_url={'set' if self.n8n_webhook_url else 'missing'}, "
            f"trade_notify_email={'set' if self.trade_notify_email else 'missing'}, "
            f"smtp_host={'set' if self.smtp_host else 'missing'}, "
            f"smtp_password={'set' if self.smtp_password else 'missing'}, "
            f"dashboard_trades_webhook={'set' if self.dashboard_trades_webhook else 'missing'}, "
            f"dashboard_port={self.dashboard_port})"
        )


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Load paper credentials and the model endpoint.

    ``ALPACA_PAPER`` must be true. A live flag aborts before a client exists.
    When ``environ`` is omitted, values are read from the process environment
    after a local ``.env`` file is loaded. An explicit mapping is used as-is
    so tests do not touch the developer's ``.env``.
    """

    if environ is None:
        load_dotenv(Path(__file__).resolve().parent / ".env")
        source: dict[str, str] = dict(os.environ)
    else:
        source = environ

    paper_flag = source.get("ALPACA_PAPER", "").strip().lower()
    if paper_flag not in {"1", "true", "yes"}:
        raise RuntimeError(
            "ALPACA_PAPER must be true. This bot refuses to start when live trading is requested."
        )

    alpaca_key = source.get("ALPACA_API_KEY", "").strip()
    alpaca_secret = source.get("ALPACA_SECRET_KEY", "").strip()
    openai_key = source.get("OPENAI_API_KEY", "").strip()
    if not alpaca_key or not alpaca_secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required. Use paper-trading keys.")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is required. The model suggests trades; it does not size or send them.")

    base_url = source.get("OPENAI_BASE_URL", "https://api.x.ai/v1").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("OPENAI_BASE_URL must be an https URL.")

    model = source.get("OPENAI_MODEL", "grok-4.7").strip()
    if not model:
        raise RuntimeError("OPENAI_MODEL cannot be empty.")

    interval_raw = source.get("LOOP_INTERVAL_SECONDS", str(DEFAULT_LOOP_INTERVAL_SECONDS)).strip()
    try:
        interval = require_interval(int(interval_raw))
    except ValueError as exc:
        raise RuntimeError(f"LOOP_INTERVAL_SECONDS must be an integer, got {interval_raw!r}") from exc

    fx_raw = source.get("POUNDS_PER_USD", "").strip()
    pounds_per_usd: Decimal | None
    if not fx_raw:
        pounds_per_usd = None
    else:
        try:
            pounds_per_usd = Decimal(fx_raw)
        except Exception as exc:
            raise RuntimeError(f"POUNDS_PER_USD must be a decimal number, got {fx_raw!r}") from exc
        if not pounds_per_usd.is_finite() or pounds_per_usd <= 0:
            raise RuntimeError("POUNDS_PER_USD must be a positive number. It is display-only.")

    webhook = source.get("N8N_WEBHOOK_URL", "").strip()
    trade_email = source.get("TRADE_NOTIFY_EMAIL", "").strip()
    smtp_host = source.get("SMTP_HOST", "").strip()
    smtp_user = source.get("SMTP_USER", "").strip()
    smtp_password = source.get("SMTP_PASSWORD", "").strip()
    smtp_from = source.get("SMTP_FROM", "").strip()
    port_raw = source.get("SMTP_PORT", "587").strip() or "587"
    try:
        smtp_port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"SMTP_PORT must be an integer, got {port_raw!r}") from exc
    if smtp_port <= 0:
        raise RuntimeError("SMTP_PORT must be a positive integer.")

    trades_webhook = source.get("DASHBOARD_TRADES_WEBHOOK", DEFAULT_DASHBOARD_TRADES_WEBHOOK).strip()
    port_env = source.get("PORT", source.get("DASHBOARD_PORT", str(DEFAULT_DASHBOARD_PORT))).strip()
    try:
        dashboard_port = int(port_env or str(DEFAULT_DASHBOARD_PORT))
    except ValueError as exc:
        raise RuntimeError(f"PORT must be an integer, got {port_env!r}") from exc
    if dashboard_port <= 0:
        raise RuntimeError("PORT must be a positive integer.")

    return Settings(
        alpaca_api_key=alpaca_key,
        alpaca_secret_key=alpaca_secret,
        openai_api_key=openai_key,
        openai_base_url=base_url,
        openai_model=model,
        loop_interval_seconds=interval,
        pounds_per_usd=pounds_per_usd,
        n8n_webhook_url=webhook or None,
        trade_notify_email=trade_email or None,
        smtp_host=smtp_host or None,
        smtp_port=smtp_port,
        smtp_user=smtp_user or None,
        smtp_password=smtp_password or None,
        smtp_from=smtp_from or None,
        dashboard_trades_webhook=trades_webhook or None,
        dashboard_port=dashboard_port,
    )
