"""Portfolio calculation engine: ledger -> positions, PnL, TWR, CAGR, drawdown, drift.

Conventions
- All internal values are in USD. `fx(currency, ts)` returns USD per 1 unit of `currency`.
- Cost method: weighted-average (DCA). Fees on BUY are capitalised into cost basis;
  fees on SELL reduce proceeds; standalone FEE rows reduce realized PnL.
- STAKING_REWARD: units added; cost basis = FMV at receipt (or 0 if reward_basis="zero").
- DIVIDEND: cash income (amount * price_per_unit); no quantity change.
- TRANSFER: internal move between wallets/accounts -> no effect on portfolio totals.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal as D
from typing import Callable, Iterable, Literal
import pandas as pd

FxFn = Callable[[str, datetime], D]
USD_ONLY: FxFn = lambda cur, ts: D(1)


@dataclass
class Tx:
    asset_id: int
    type: Literal["BUY", "SELL", "TRANSFER", "STAKING_REWARD", "DIVIDEND", "FEE"]
    amount: D
    price_per_unit: D
    fee: D
    currency: str
    timestamp: datetime
    notes: str = ""


@dataclass
class Position:
    asset_id: int
    qty: D = D(0)
    cost_basis: D = D(0)          # total USD cost of the *currently held* units
    realized_pnl: D = D(0)
    realized_cost: D = D(0)       # cost of units sold (denominator for realized %)
    income: D = D(0)              # dividends + staking rewards (USD, at receipt)
    fees: D = D(0)
    total_invested: D = D(0)      # gross USD ever put into BUYs (incl. fees)

    @property
    def avg_cost(self) -> D:
        return self.cost_basis / self.qty if self.qty > 0 else D(0)

    def unrealized(self, price: D) -> tuple[D, D]:
        pnl = self.qty * price - self.cost_basis
        pct = pnl / self.cost_basis * 100 if self.cost_basis > 0 else D(0)
        return pnl, pct

    @property
    def realized_pct(self) -> D:
        return self.realized_pnl / self.realized_cost * 100 if self.realized_cost > 0 else D(0)


def build_positions(txs: Iterable[Tx], fx: FxFn = USD_ONLY,
                    reward_basis: Literal["fmv", "zero"] = "fmv") -> dict[int, Position]:
    pos: dict[int, Position] = {}
    for t in sorted(txs, key=lambda x: x.timestamp):
        p = pos.setdefault(t.asset_id, Position(t.asset_id))
        r = fx(t.currency, t.timestamp)
        gross, fee = t.amount * t.price_per_unit * r, t.fee * r
        p.fees += fee

        if t.type == "BUY":
            p.qty += t.amount
            p.cost_basis += gross + fee
            p.total_invested += gross + fee
        elif t.type == "SELL":
            if t.amount > p.qty:
                raise ValueError(f"Oversell asset {t.asset_id} at {t.timestamp}: {t.amount} > {p.qty}")
            sold_cost = p.avg_cost * t.amount
            p.realized_pnl += (gross - fee) - sold_cost
            p.realized_cost += sold_cost
            p.cost_basis -= sold_cost
            p.qty -= t.amount
        elif t.type == "STAKING_REWARD":
            p.qty += t.amount
            p.income += gross
            if reward_basis == "fmv":
                p.cost_basis += gross
        elif t.type == "DIVIDEND":
            p.income += gross
        elif t.type == "FEE":
            p.realized_pnl -= fee
        # TRANSFER: intentionally a no-op at portfolio level
    return pos


def external_flows(txs: Iterable[Tx], fx: FxFn = USD_ONLY) -> pd.Series:
    """Daily net external cash flow into the portfolio (USD), for TWR.
    BUY=+cost, SELL=-proceeds, DIVIDEND=-cash paid out. Rewards/transfers = 0."""
    rows = []
    for t in txs:
        r = fx(t.currency, t.timestamp)
        g, f = float(t.amount * t.price_per_unit * r), float(t.fee * r)
        flow = {"BUY": g + f, "SELL": -(g - f), "DIVIDEND": -g}.get(t.type, 0.0)
        if flow:
            rows.append((pd.Timestamp(t.timestamp).normalize().tz_localize(None), flow))
    if not rows:
        return pd.Series(dtype=float)
    return pd.DataFrame(rows, columns=["d", "f"]).groupby("d")["f"].sum()


# ---------- time-series metrics (floats / pandas) ----------

def twr_index(values: pd.Series, flows: pd.Series) -> pd.Series:
    """Chain-linked TWR index (base 1.0). End-of-day flow convention:
    r_t = (V_t - CF_t) / V_{t-1} - 1. `values` is daily net worth indexed by date."""
    values = values.sort_index().astype(float)
    cf = flows.reindex(values.index).fillna(0.0)
    prev = values.shift(1)
    r = ((values - cf) / prev - 1).where(prev > 0, 0.0).fillna(0.0)
    return (1 + r).cumprod()


def cagr(index: pd.Series) -> float:
    days = (index.index[-1] - index.index[0]).days
    return float(index.iloc[-1] / index.iloc[0]) ** (365 / days) - 1 if days > 0 else 0.0


def max_drawdown(series: pd.Series) -> float:
    """Max peak-to-trough drop as a negative fraction (use TWR index or price series)."""
    s = series.dropna().astype(float)
    return float((s / s.cummax() - 1).min()) if len(s) else 0.0


def distance_from_ath(prices: pd.Series) -> float:
    s = prices.dropna().astype(float)
    return float(s.iloc[-1] / s.max() - 1) if len(s) else 0.0


def slice_range(s: pd.Series, rng: Literal["1M", "6M", "1Y", "YTD", "ALL"]) -> pd.Series:
    end = s.index[-1]
    start = {"1M": end - pd.DateOffset(months=1), "6M": end - pd.DateOffset(months=6),
             "1Y": end - pd.DateOffset(years=1), "YTD": pd.Timestamp(end.year, 1, 1),
             "ALL": s.index[0]}[rng]
    return s[s.index >= start]


def rebased(s: pd.Series) -> pd.Series:
    """Rebase to 100 for benchmark overlay (portfolio TWR vs SPY vs BTC)."""
    return s / s.iloc[0] * 100


# ---------- allocation / rebalancing ----------

@dataclass
class Summary:
    net_worth: D = D(0)
    invested: D = D(0)
    unrealized: D = D(0)
    unrealized_pct: D = D(0)
    realized: D = D(0)
    income: D = D(0)
    by_asset: dict[int, dict] = field(default_factory=dict)


def summarize(positions: dict[int, Position], prices: dict[int, D]) -> Summary:
    s = Summary()
    for aid, p in positions.items():
        price = prices.get(aid, D(0))
        u, u_pct = p.unrealized(price)
        value = p.qty * price
        s.by_asset[aid] = dict(qty=p.qty, avg_cost=p.avg_cost, price=price, value=value,
                               unrealized=u, unrealized_pct=u_pct,
                               realized=p.realized_pnl, realized_pct=p.realized_pct, income=p.income)
        s.net_worth += value
        s.invested += p.cost_basis
        s.unrealized += u
        s.realized += p.realized_pnl
        s.income += p.income
    s.unrealized_pct = s.unrealized / s.invested * 100 if s.invested > 0 else D(0)
    return s


def allocation(values_by_class: dict[str, D]) -> dict[str, float]:
    total = sum(values_by_class.values())
    return {k: float(v / total * 100) if total else 0.0 for k, v in values_by_class.items()}


def rebalancing_drift(current_pct: dict[str, float], target_pct: dict[str, float],
                      net_worth: float) -> pd.DataFrame:
    """Drift in percentage points and the USD to buy(+)/sell(-) to hit target."""
    classes = sorted(set(current_pct) | set(target_pct))
    df = pd.DataFrame({"current_pct": [current_pct.get(c, 0.0) for c in classes],
                       "target_pct": [target_pct.get(c, 0.0) for c in classes]}, index=classes)
    df["drift_pp"] = df.current_pct - df.target_pct
    df["rebalance_usd"] = -df.drift_pp / 100 * net_worth
    return df
