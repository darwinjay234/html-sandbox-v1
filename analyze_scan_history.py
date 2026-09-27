"""Measure how saved scan signals line up with subsequent 4-hour prices.

This is a descriptive review tool, not a trading strategy or backtester. The
early-momentum group is an exploratory cohort defined in ``is_early_momentum``.
"""

import argparse
from bisect import bisect_left
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_HISTORY_DIR = Path("data/history")
DEFAULT_OUTPUT_DIR = Path("data/analysis")
HORIZONS_HOURS = (4, 12, 24)
CANDLE_HOURS = 4
MAX_MATCH_LAG_HOURS = 4


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def load_closed_4h_prices(path: Path) -> Dict[str, List[Tuple[datetime, float]]]:
    """Load CoinGecko 4h bar closes, excluding non-live/mock records."""
    if not path.is_file():
        raise FileNotFoundError(f"Market candle CSV not found: {path}")

    closes: Dict[str, Dict[datetime, float]] = defaultdict(dict)
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            if row.get("timeframe") != "4h" or row.get("data_source") != "coingecko":
                continue
            try:
                bar_start = parse_timestamp(row["timestamp_utc"])
                price = float(row["price"])
                if price > 0:
                    # Resampling labels bars at their start; the recorded
                    # price is the last sample in that completed 4h interval.
                    closes[row["coin_id"]][bar_start + timedelta(hours=CANDLE_HOURS)] = price
            except (KeyError, TypeError, ValueError) as error:
                print(f"Skipping malformed 4h candle row: {error}", file=sys.stderr)

    return {coin: sorted(rows.items()) for coin, rows in closes.items()}


def load_scan_decisions(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Scan decision CSV not found: {path}")

    decisions: List[Dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            if row.get("data_source") != "coingecko" or row.get("status") != "success":
                continue
            try:
                metrics = json.loads(row["metrics_json"])
                entry_price = float(metrics["current_price"])
                if entry_price <= 0:
                    continue
                buy_score = int(row["buy_score"])
                sell_score = int(row["sell_score"])
                buy_alert = parse_bool(row.get("buy_alert_triggered"))
                warning_alert = parse_bool(row.get("warning_alert_triggered"))
                sell_alert = parse_bool(row.get("sell_alert_triggered"))
                early_buy = parse_bool(metrics.get("early_buy_setup_active"))
                early_sell = parse_bool(metrics.get("early_sell_setup_active"))
                early_buy_alert = parse_bool(metrics.get("early_buy_alert_triggered"))
                early_sell_alert = parse_bool(metrics.get("early_sell_alert_triggered"))
                if buy_alert:
                    alert_state = "BUY"
                elif warning_alert:
                    alert_state = "WARNING"
                elif sell_alert:
                    alert_state = "SELL"
                elif early_buy_alert:
                    alert_state = "EARLY_BUY"
                elif early_sell_alert:
                    alert_state = "EARLY_SELL_RISK"
                else:
                    alert_state = "NONE"
                scanned_at = parse_timestamp(row["scanned_at_utc"])
                decisions.append({
                    "scanned_at": scanned_at,
                    "coin_id": row["coin_id"],
                    "entry_price": entry_price,
                    "buy_score": buy_score,
                    "sell_score": sell_score,
                    "alert_state": alert_state,
                    "early_buy_setup": early_buy,
                    "early_sell_setup": early_sell,
                    "onchain_data_source": metrics.get("onchain_data_source", "unknown"),
                })
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(f"Skipping malformed scan decision row: {error}", file=sys.stderr)
    return decisions


def match_forward_price(
    decision: Dict[str, Any],
    horizon_hours: int,
    prices: Dict[str, List[Tuple[datetime, float]]],
) -> Dict[str, Any]:
    target_time = decision["scanned_at"] + timedelta(hours=horizon_hours)
    coin_prices = prices.get(decision["coin_id"], [])
    match_index = bisect_left(coin_prices, (target_time, float("-inf")))
    result = {
        "future_close_utc": "",
        "future_price": "",
        "observed_after_hours": "",
        "price_return_pct": "",
        "outcome_status": "awaiting_forward_candle",
    }
    if match_index >= len(coin_prices):
        return result

    close_time, future_price = coin_prices[match_index]
    elapsed_hours = (close_time - decision["scanned_at"]).total_seconds() / 3600
    result["future_close_utc"] = close_time.isoformat()
    result["future_price"] = future_price
    result["observed_after_hours"] = round(elapsed_hours, 2)
    if elapsed_hours > horizon_hours + MAX_MATCH_LAG_HOURS:
        result["outcome_status"] = "forward_candle_gap_exceeds_tolerance"
        return result

    result["price_return_pct"] = round(
        (future_price / decision["entry_price"] - 1.0) * 100.0, 6
    )
    result["outcome_status"] = "matched"
    return result


def make_forward_rows(
    decisions: Iterable[Dict[str, Any]],
    prices: Dict[str, List[Tuple[datetime, float]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for decision in decisions:
        for horizon in HORIZONS_HOURS:
            outcome = match_forward_price(decision, horizon, prices)
            rows.append({
                "scanned_at_utc": decision["scanned_at"].isoformat(),
                "coin_id": decision["coin_id"],
                "horizon_hours": horizon,
                "buy_score": decision["buy_score"],
                "sell_score": decision["sell_score"],
                "alert_state": decision["alert_state"],
                "early_buy_setup": decision["early_buy_setup"],
                "early_sell_setup": decision["early_sell_setup"],
                "entry_price": decision["entry_price"],
                "onchain_data_source": decision["onchain_data_source"],
                **outcome,
            })
    return rows


def group_memberships(row: Dict[str, Any]) -> List[Tuple[str, str, Optional[str]]]:
    """Return (dimension, value, bias) used for summary aggregation."""
    state = row["alert_state"]
    state_bias = "short" if state in {"SELL", "EARLY_SELL_RISK"} else "long" if state in {"BUY", "WARNING", "EARLY_BUY"} else None
    memberships = [
        ("coin", row["coin_id"], None),
        ("buy_score", str(row["buy_score"]), "long"),
        ("sell_score", str(row["sell_score"]), "short"),
        ("alert_state", state, state_bias),
    ]
    if row["early_buy_setup"]:
        memberships.append(("early_setup", "BUY", "long"))
    if row["early_sell_setup"]:
        memberships.append(("early_setup", "SELL_RISK", "short"))
    return memberships


def make_summary_rows(forward_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    group_bias: Dict[Tuple[str, str, int], Optional[str]] = {}
    for row in forward_rows:
        horizon = int(row["horizon_hours"])
        for dimension, value, bias in group_memberships(row):
            key = (dimension, value, horizon)
            groups[key].append(row)
            group_bias[key] = bias

    summaries: List[Dict[str, Any]] = []
    for (dimension, value, horizon), rows in sorted(groups.items()):
        matched = [row for row in rows if row["outcome_status"] == "matched"]
        returns = [float(row["price_return_pct"]) for row in matched]
        bias = group_bias[(dimension, value, horizon)]
        if bias == "long":
            favorable = [value > 0 for value in returns]
        elif bias == "short":
            favorable = [value < 0 for value in returns]
        else:
            favorable = []
        summaries.append({
            "group_type": dimension,
            "group_value": value,
            "horizon_hours": horizon,
            "matched_observations": len(matched),
            "awaiting_or_unmatched": len(rows) - len(matched),
            "mean_price_return_pct": round(statistics.mean(returns), 6) if returns else "",
            "median_price_return_pct": round(statistics.median(returns), 6) if returns else "",
            "positive_price_return_pct": round(sum(value > 0 for value in returns) / len(returns) * 100, 2) if returns else "",
            "directional_win_rate_pct": round(sum(favorable) / len(favorable) * 100, 2) if favorable else "",
            "directional_observations": len(favorable),
        })
    return summaries


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare saved scanner decisions with subsequent closed 4-hour prices."
    )
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR,
                        help=f"Directory containing the two history CSVs (default: {DEFAULT_HISTORY_DIR})")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help=f"Directory for analysis CSVs (default: {DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    try:
        prices = load_closed_4h_prices(args.history_dir / "market_candles.csv")
        decisions = load_scan_decisions(args.history_dir / "scan_decisions.csv")
    except (OSError, csv.Error) as error:
        parser.error(str(error))

    forward_rows = make_forward_rows(decisions, prices)
    summary_rows = make_summary_rows(forward_rows)
    write_csv(args.output_dir / "forward_returns.csv", forward_rows, [
        "scanned_at_utc", "coin_id", "horizon_hours", "buy_score", "sell_score",
        "alert_state", "early_buy_setup", "early_sell_setup", "entry_price", "future_close_utc",
        "future_price", "observed_after_hours", "price_return_pct", "outcome_status",
        "onchain_data_source",
    ])
    write_csv(args.output_dir / "analysis_summary.csv", summary_rows, [
        "group_type", "group_value", "horizon_hours", "matched_observations",
        "awaiting_or_unmatched", "mean_price_return_pct", "median_price_return_pct",
        "positive_price_return_pct", "directional_win_rate_pct", "directional_observations",
    ])

    matched_count = sum(row["outcome_status"] == "matched" for row in forward_rows)
    pending_count = len(forward_rows) - matched_count
    print(f"Loaded {len(decisions)} live scan decisions and {sum(map(len, prices.values()))} closed 4h candles.")
    print(f"Forward outcomes matched: {matched_count}; awaiting or unmatched: {pending_count}.")
    print(f"Detailed outcomes: {args.output_dir / 'forward_returns.csv'}")
    print(f"Grouped summary:    {args.output_dir / 'analysis_summary.csv'}")
    print("Returns are price changes, not simulated trade profits; fees, slippage, and position sizing are excluded.")
    print("Early setup groups mirror provisional scanner alerts; outcomes are descriptive, not trade results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
