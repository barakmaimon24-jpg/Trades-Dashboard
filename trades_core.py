import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache

import pandas as pd
import yfinance as yf


TRADE_NOTES_PATH = "trade_notes.json"
FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
FLEX_GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
FLEX_QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")
FLEX_TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
FLEX_VERSION = os.environ.get("IBKR_FLEX_VERSION", "3")
FLEX_USER_AGENT = "TradesDashboard/1.0"


def _local_tag_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _iter_xml_nodes(root: ET.Element, local_name: str):
    for node in root.iter():
        if _local_tag_name(node.tag) == local_name:
            yield node


def _get_attr(node: ET.Element, *names: str) -> str | None:
    for name in names:
        value = node.get(name)
        if value not in (None, ""):
            return value
    return None


def _find_first_text(root: ET.Element, *local_names: str) -> str:
    wanted = set(local_names)
    for node in root.iter():
        if _local_tag_name(node.tag) in wanted and node.text:
            return node.text.strip()
    return ""


def _safe_float(value: str | None, default: float | None = 0.0) -> float | None:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    try:
        number = float(text.strip("()").replace(",", "").replace("$", ""))
    except ValueError:
        return default
    return -number if negative else number


def _normalize_trade_side(value: str | None) -> str:
    side = (value or "").strip().upper()
    if side in {"BOT", "BUY"}:
        return "BUY"
    if side in {"SLD", "SELL"}:
        return "SELL"
    return side


def _flex_service_error(root: ET.Element) -> str:
    return " - ".join(
        part
        for part in [
            _find_first_text(root, "Status"),
            _find_first_text(root, "ErrorCode", "code"),
            _find_first_text(root, "ErrorMessage", "message"),
        ]
        if part
    )


def _flex_reference_code(root: ET.Element) -> str:
    return _find_first_text(root, "ReferenceCode")


def _is_flex_service_response(root: ET.Element) -> bool:
    return _local_tag_name(root.tag) in {"FlexStatementResponse", "FlexStatementServiceResponse"}


def _is_flex_generation_pending(root: ET.Element) -> bool:
    return (
        _find_first_text(root, "ErrorCode", "code") == "1019"
        or "generation in progress" in _find_first_text(root, "ErrorMessage", "message").lower()
    )


def _is_flex_send_retryable(message: str) -> bool:
    return "could not be generated at this time" in message.lower()


def _json_ready(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp) or hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def records_for_api(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    normalized = df.copy()
    for column in normalized.columns:
        normalized[column] = normalized[column].map(_json_ready)
    return normalized.to_dict(orient="records")


def summarize_xml_tags(xml_text: str, limit: int = 40) -> dict[str, int | str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"parse_error": str(exc)}
    counts: dict[str, int] = {}
    for node in root.iter():
        tag = _local_tag_name(node.tag)
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit])


def parse_trades(xml_text: str) -> tuple[pd.DataFrame, float | None, pd.Series, pd.DataFrame]:
    root = ET.fromstring(xml_text)
    rows = []
    for trade in _iter_xml_nodes(root, "Trade"):
        rows.append(
            {
                "symbol": _get_attr(trade, "symbol", "underlyingSymbol", "description") or "",
                "trade_date": _get_attr(trade, "tradeDate", "date", "orderDate") or "",
                "trade_datetime": _get_attr(trade, "dateTime", "tradeTime", "dateTimeGMT") or "",
                "buy_sell": _normalize_trade_side(_get_attr(trade, "buySell", "side")),
                "quantity": _safe_float(_get_attr(trade, "quantity"), 0.0) or 0.0,
                "trade_price": _safe_float(_get_attr(trade, "tradePrice"), 0.0) or 0.0,
                "proceeds": _safe_float(_get_attr(trade, "proceeds"), 0.0) or 0.0,
                "ib_commission": _safe_float(_get_attr(trade, "ibCommission"), 0.0) or 0.0,
                "fifo_pnl_realized": _safe_float(_get_attr(trade, "fifoPnlRealized"), 0.0) or 0.0,
                "asset_category": _get_attr(trade, "assetCategory", "assetClass") or "",
                "currency": _get_attr(trade, "currency") or "",
            }
        )
    trades = pd.DataFrame(rows)
    if not trades.empty:
        trade_date = pd.to_datetime(trades["trade_date"], format="%Y%m%d", errors="coerce")
        trade_date_alt = pd.to_datetime(trades["trade_date"], format="%d/%m/%Y", errors="coerce")
        trades["trade_date"] = trade_date.fillna(trade_date_alt).fillna(pd.to_datetime(trades["trade_date"], errors="coerce"))
        trade_datetime = pd.to_datetime(trades["trade_datetime"], format="%Y%m%d;%H%M%S", errors="coerce")
        trade_datetime_alt = pd.to_datetime(trades["trade_datetime"], format="%d/%m/%Y;%H%M%S", errors="coerce")
        trades["trade_datetime"] = trade_datetime.fillna(trade_datetime_alt).fillna(pd.to_datetime(trades["trade_datetime"], errors="coerce"))

    totals = []
    for node in _iter_xml_nodes(root, "EquitySummaryByReportDateInBase"):
        report_date = _get_attr(node, "reportDate", "date")
        total = _safe_float(_get_attr(node, "total"), None)
        if report_date and total is not None:
            parsed = pd.to_datetime(report_date, format="%Y%m%d", errors="coerce")
            if pd.isna(parsed):
                parsed = pd.to_datetime(report_date, format="%d/%m/%Y", errors="coerce")
            if pd.notna(parsed):
                totals.append((parsed, total))
    if totals:
        totals.sort(key=lambda item: item[0])
        latest_total = totals[-1][1]
        equity_series = pd.Series({date: total for date, total in totals}).sort_index()
    else:
        latest_total = None
        equity_series = pd.Series(dtype="float64")

    open_rows = []
    for node in _iter_xml_nodes(root, "OpenPosition"):
        open_rows.append(
            {
                "symbol": node.get("symbol", ""),
                "position": _safe_float(_get_attr(node, "position"), None),
                "mark_price": _safe_float(_get_attr(node, "markPrice"), None),
                "position_value": _safe_float(_get_attr(node, "marketValue", "positionValue"), None),
                "unrealized_pnl": _safe_float(_get_attr(node, "unrealizedPnl", "unrealizedPnL", "fifoPnlUnrealized"), None),
                "cost_basis": _safe_float(_get_attr(node, "costBasisMoney", "costBasis"), None),
                "avg_cost": _safe_float(_get_attr(node, "avgCost", "averageCost", "costBasisPrice", "openPrice"), None),
                "percent_of_nav": _safe_float(_get_attr(node, "percentOfNAV", "percentageOfNAV", "percentOfNetLiq", "percentOfNetLiquidation"), None),
            }
        )
    open_positions = pd.DataFrame(open_rows)
    if not open_positions.empty:
        open_positions["symbol"] = open_positions["symbol"].astype(str).str.strip()
        open_positions = open_positions[
            open_positions["symbol"].ne("")
            & open_positions["position"].notna()
            & (open_positions["position"].abs() > 1e-9)
        ].reset_index(drop=True)
    return trades, latest_total, equity_series, open_positions


def load_trade_notes() -> dict:
    if not os.path.exists(TRADE_NOTES_PATH):
        return {}
    try:
        with open(TRADE_NOTES_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def save_trade_notes(notes: dict) -> None:
    with open(TRADE_NOTES_PATH, "w", encoding="utf-8") as handle:
        json.dump(notes, handle, indent=2, sort_keys=True)


def _fetch_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": FLEX_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def fetch_flex_xml() -> tuple[str | None, dict]:
    debug: dict[str, str] = {}
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        debug["configuration_error"] = "Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID environment variables."
        return None, debug
    try:
        send_url = f"{FLEX_BASE_URL}?{urllib.parse.urlencode({'t': FLEX_TOKEN, 'q': FLEX_QUERY_ID, 'v': FLEX_VERSION})}"
        debug["send_url"] = send_url
        ref_code = ""
        for attempt in range(3):
            response_text = _fetch_url_text(send_url)
            debug["send_response"] = response_text[:4000]
            root = ET.fromstring(response_text)
            status = _find_first_text(root, "Status").upper()
            message = _flex_service_error(root)
            debug["send_status"] = status
            debug["send_error"] = message
            debug["send_attempts"] = str(attempt + 1)
            ref_code = _flex_reference_code(root)
            debug["reference_code"] = ref_code or ""
            if ref_code:
                break
            if _is_flex_service_response(root) and _is_flex_send_retryable(message):
                time.sleep(2)
                continue
            if status and status != "SUCCESS":
                return None, debug
        if not ref_code:
            return None, debug

        get_url = f"{FLEX_GET_URL}?{urllib.parse.urlencode({'t': FLEX_TOKEN, 'q': ref_code, 'v': FLEX_VERSION})}"
        debug["get_url"] = get_url
        last_response = ""
        for attempt in range(10):
            xml_text = _fetch_url_text(get_url)
            last_response = xml_text
            root = ET.fromstring(xml_text)
            status = _find_first_text(root, "Status").upper()
            message = _flex_service_error(root)
            if _is_flex_service_response(root):
                debug["get_status"] = status
                debug["get_error"] = message
                debug["get_attempts"] = str(attempt + 1)
                if _is_flex_generation_pending(root):
                    time.sleep(2)
                    continue
                if status and status != "SUCCESS":
                    debug["get_response"] = xml_text[:4000]
                    return None, debug
            if not any(_iter_xml_nodes(root, "Trade")) and message:
                debug["get_response"] = xml_text[:4000]
                return None, debug
            debug["get_response"] = xml_text[:4000]
            return xml_text, debug
        debug["get_response"] = last_response[:4000]
        debug["get_error"] = "Statement generation did not finish before timeout."
        return None, debug
    except Exception as exc:
        debug["exception"] = str(exc)
        return None, debug


def equity_for_date(equity_series: pd.Series, trade_date: pd.Timestamp | None) -> float | None:
    if equity_series.empty or trade_date is None or pd.isna(trade_date):
        return None
    idx = equity_series.index.searchsorted(pd.to_datetime(trade_date), side="right") - 1
    return float(equity_series.iloc[idx]) if idx >= 0 else None


def add_trade_segments(trades: pd.DataFrame) -> pd.DataFrame:
    trades = trades.sort_values(["symbol", "trade_datetime", "trade_date"]).reset_index(drop=True).copy()
    trades["trade_time"] = trades["trade_datetime"].where(pd.notna(trades["trade_datetime"]), trades["trade_date"])
    trades["signed_qty"] = trades.apply(lambda row: abs(row["quantity"]) * (1 if row["buy_sell"] == "BUY" else -1), axis=1)
    trades["position_running"] = trades.groupby("symbol")["signed_qty"].cumsum()
    trades["segment_id"] = (
        trades.groupby("symbol")["position_running"]
        .apply(lambda s: s.eq(0).cumsum().shift(1, fill_value=0))
        .reset_index(level=0, drop=True)
    )
    trades["segment_max"] = trades.groupby(["symbol", "segment_id"])["position_running"].transform(lambda s: s.abs().max())
    return trades


@lru_cache(maxsize=16)
def _fetch_latest_prices_cached(symbols_key: tuple[str, ...]) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
    symbols = list(symbols_key)
    prices: dict[str, float] = {}
    debug: dict[str, dict[str, str]] = {}
    if not symbols:
        return prices, debug
    try:
        data = yf.download(" ".join(symbols), period="1d", interval="1m", prepost=True, group_by="ticker", progress=False)
    except Exception:
        data = None
    if data is None or data.empty:
        return prices, debug
    for symbol in symbols:
        try:
            series = data[symbol]["Close"].dropna() if isinstance(data.columns, pd.MultiIndex) else data["Close"].dropna()
            if not series.empty:
                prices[symbol] = float(series.iloc[-1])
                debug[symbol] = {"last_timestamp": str(series.index[-1]), "bar_count": str(len(series))}
        except Exception:
            continue
    return prices, debug


def fetch_latest_prices(symbols: list[str], refresh: bool = False) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
    if refresh:
        _fetch_latest_prices_cached.cache_clear()
    return _fetch_latest_prices_cached(tuple(sorted(set(symbols))))


def match_trades(trades: pd.DataFrame, live_prices: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = add_trade_segments(trades)
    open_lots: dict[str, list[dict]] = {}
    closed_rows = []
    for _, row in trades.iterrows():
        symbol = row["symbol"]
        open_lots.setdefault(symbol, [])
        qty = -abs(float(row["quantity"] or 0)) if row["buy_sell"] == "SELL" else abs(float(row["quantity"] or 0))
        price = float(row["trade_price"] or 0)
        trade_time = row["trade_datetime"] if pd.notna(row["trade_datetime"]) else row["trade_date"]
        remaining = qty
        lots = open_lots[symbol]
        while remaining != 0 and lots:
            lot = lots[0]
            if lot["qty"] == 0:
                lots.pop(0)
                continue
            if lot["qty"] * remaining >= 0:
                break
            close_qty = min(abs(remaining), abs(lot["qty"]))
            close_qty = close_qty if remaining > 0 else -close_qty
            closed_rows.append(
                {
                    "symbol": symbol,
                    "quantity": abs(close_qty),
                    "direction": "LONG" if lot["qty"] > 0 else "SHORT",
                    "entry_price": lot["price"],
                    "exit_price": price,
                    "entry_time": lot["time"],
                    "exit_time": trade_time,
                    "pnl": ((price - lot["price"]) if lot["qty"] > 0 else (lot["price"] - price)) * abs(close_qty),
                    "segment_max": float(row["segment_max"]) if pd.notna(row.get("segment_max")) else None,
                    "segment_id": int(row["segment_id"]) if pd.notna(row.get("segment_id")) else None,
                }
            )
            lot["qty"] += close_qty
            remaining -= close_qty
            if lot["qty"] == 0:
                lots.pop(0)
        if remaining != 0:
            lots.append({"qty": remaining, "price": price, "time": trade_time})

    last_prices = trades.dropna(subset=["trade_price"]).sort_values(["symbol", "trade_datetime", "trade_date"]).groupby("symbol")["trade_price"].last().to_dict()
    today = pd.Timestamp.now().normalize()
    open_rows = []
    for symbol, lots in open_lots.items():
        net_qty = sum(lot["qty"] for lot in lots)
        if net_qty == 0:
            continue
        cost_basis = sum(abs(lot["qty"]) * lot["price"] for lot in lots)
        avg_entry = cost_basis / abs(net_qty)
        last_price = float(live_prices.get(symbol, last_prices.get(symbol, avg_entry)))
        unrealized = ((last_price - avg_entry) if net_qty > 0 else (avg_entry - last_price)) * abs(net_qty)
        entry_times = [lot["time"] for lot in lots if pd.notna(lot["time"])]
        entry_time = min(entry_times) if entry_times else pd.NaT
        open_rows.append(
            {
                "entry_date": entry_time.date() if pd.notna(entry_time) else None,
                "symbol": symbol,
                "quantity": abs(net_qty),
                "direction": "LONG" if net_qty > 0 else "SHORT",
                "avg_entry": avg_entry,
                "pnl_pct": unrealized / cost_basis * 100 if cost_basis else 0.0,
                "unrealized_pnl": unrealized,
                "holding_days": (today - pd.to_datetime(entry_time).normalize()).days if pd.notna(entry_time) else None,
                "last_price": last_price,
            }
        )
    open_df = pd.DataFrame(open_rows).sort_values(["symbol"], ignore_index=True) if open_rows else pd.DataFrame()
    closed_df = pd.DataFrame(closed_rows)
    if closed_df.empty:
        return open_df, closed_df
    closed_df["entry_time"] = pd.to_datetime(closed_df["entry_time"])
    closed_df["exit_time"] = pd.to_datetime(closed_df["exit_time"])
    closed_df["exit_hour"] = closed_df["exit_time"].dt.floor("h")
    closed_df = (
        closed_df.groupby(["symbol", "exit_hour"], as_index=False)
        .apply(lambda group: pd.Series({
            "quantity": group["quantity"].sum(),
            "direction": group["direction"].iloc[0],
            "entry_price": (group["entry_price"] * group["quantity"]).sum() / group["quantity"].sum(),
            "exit_price": (group["exit_price"] * group["quantity"]).sum() / group["quantity"].sum(),
            "entry_time": group["entry_time"].min(),
            "exit_time": group["exit_time"].max(),
            "pnl": group["pnl"].sum(),
            "segment_max": group["segment_max"].max(),
            "segment_id": group["segment_id"].max(),
        }))
        .reset_index(drop=True)
    )
    closed_df["entry_date"] = closed_df["entry_time"].dt.date
    closed_df["exit_date"] = closed_df["exit_time"].dt.date
    closed_df["exit_time_str"] = closed_df["exit_time"].dt.strftime("%H:%M")
    closed_df["market_value"] = closed_df["exit_price"] * closed_df["quantity"]
    closed_df["holding_days"] = (closed_df["exit_time"].dt.normalize() - closed_df["entry_time"].dt.normalize()).dt.days
    return open_df, closed_df.drop(columns=["entry_time", "exit_time"])


def build_dashboard_data(xml_text: str, refresh_market: bool = False) -> dict:
    trades, total_portfolio, equity_series, open_positions = parse_trades(xml_text)
    filtered = trades.copy()
    if not filtered.empty:
        filtered = filtered[filtered["asset_category"] != "CASH"]
        filtered = filtered[filtered["symbol"].str.len() > 0]
    if filtered.empty:
        return {"total_portfolio": total_portfolio, "stats": {}, "open_positions": [], "closed_trades": [], "raw_trades": []}

    symbols = sorted(open_positions["symbol"].dropna().unique().tolist()) if not open_positions.empty else []
    live_prices, market_debug = fetch_latest_prices(symbols, refresh=refresh_market)
    if not open_positions.empty:
        for symbol, price in open_positions.dropna(subset=["symbol", "mark_price"]).set_index("symbol")["mark_price"].to_dict().items():
            live_prices.setdefault(symbol, price)

    open_trades, _ = match_trades(filtered, live_prices)
    _, closed_trades = match_trades(filtered, live_prices)

    if not open_trades.empty:
        open_trades["equity_at_entry"] = open_trades["entry_date"].apply(lambda d: equity_for_date(equity_series, pd.to_datetime(d)))
        open_trades["position_value"] = open_trades["quantity"] * open_trades["last_price"]
        open_trades["cost_basis"] = open_trades["quantity"] * open_trades["avg_entry"]
        open_trades["portfolio_pct"] = open_trades.apply(lambda row: row["position_value"] / row["equity_at_entry"] * 100 if row["equity_at_entry"] else 0.0, axis=1)
        open_trades["pnl_portfolio_pct"] = open_trades.apply(lambda row: row["unrealized_pnl"] / row["equity_at_entry"] * 100 if row["equity_at_entry"] else 0.0, axis=1)

    if not closed_trades.empty:
        closed_trades["equity_at_entry"] = closed_trades["entry_date"].apply(lambda d: equity_for_date(equity_series, pd.to_datetime(d)))
        closed_trades["position_value"] = closed_trades["entry_price"] * closed_trades["quantity"]
        closed_trades["pnl_pct"] = closed_trades.apply(lambda row: row["pnl"] / row["position_value"] * 100 if row["position_value"] else 0.0, axis=1)
        closed_trades["pnl_portfolio_pct"] = closed_trades.apply(lambda row: row["pnl"] / row["equity_at_entry"] * 100 if row["equity_at_entry"] else 0.0, axis=1)

    total_trades = len(closed_trades)
    wins = int((closed_trades["pnl"] > 0).sum()) if not closed_trades.empty else 0
    losses = int((closed_trades["pnl"] < 0).sum()) if not closed_trades.empty else 0
    return {
        "total_portfolio": total_portfolio,
        "stats": {
            "total_trades": total_trades,
            "total_pnl": float(closed_trades["pnl"].sum()) if not closed_trades.empty else 0.0,
            "total_unrealized": float(open_positions["unrealized_pnl"].sum()) if not open_positions.empty else 0.0,
            "wins": wins,
            "losses": losses,
            "flats": int((closed_trades["pnl"] == 0).sum()) if not closed_trades.empty else 0,
            "win_rate": wins / (wins + losses) * 100 if wins + losses else 0.0,
            "total_commission": float(filtered["ib_commission"].sum()),
            "avg_commission_per_trade": float(filtered["ib_commission"].sum()) / total_trades if total_trades else 0.0,
        },
        "open_positions": records_for_api(open_trades),
        "closed_trades": records_for_api(closed_trades),
        "raw_trades": records_for_api(filtered),
        "market_debug": market_debug,
    }
