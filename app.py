import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from fractions import Fraction

import pandas as pd
import streamlit as st
import yfinance as yf
import altair as alt


def _load_local_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip().lstrip("\ufeff")
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_local_env()

HISTORY_EXPORT_BASE_DIRS = {
    "winning": r"G:\My Drive\SetupsMaster\Trades\history_trades\winning",
    "losing": r"G:\My Drive\SetupsMaster\Trades\history_trades\losing",
}


def _local_tag_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _iter_xml_nodes(root: ET.Element, local_name: str):
    # IB Flex XML can include namespaces; compare by local tag name.
    for node in root.iter():
        if _local_tag_name(node.tag) == local_name:
            yield node


def _get_attr(node: ET.Element, *names: str) -> str | None:
    for name in names:
        value = node.get(name)
        if value is not None and value != "":
            return value
    return None


def _find_first_text(root: ET.Element, *local_names: str) -> str:
    wanted = set(local_names)
    for node in root.iter():
        if _local_tag_name(node.tag) in wanted and node.text:
            return node.text.strip()
    return ""


def _is_flex_service_response(root: ET.Element) -> bool:
    tag_name = _local_tag_name(root.tag)
    return tag_name in {"FlexStatementResponse", "FlexStatementServiceResponse"}


def _flex_service_error(root: ET.Element) -> str:
    status = _find_first_text(root, "Status")
    error_code = _find_first_text(root, "ErrorCode", "code")
    error_message = _find_first_text(root, "ErrorMessage", "message")
    parts = [part for part in [status, error_code, error_message] if part]
    return " - ".join(parts)


def _flex_reference_code(root: ET.Element) -> str:
    return _find_first_text(root, "ReferenceCode")


def _is_flex_generation_pending(root: ET.Element) -> bool:
    error_code = _find_first_text(root, "ErrorCode", "code")
    error_message = _find_first_text(root, "ErrorMessage", "message").lower()
    return error_code == "1019" or "generation in progress" in error_message


def _is_flex_send_retryable(message: str) -> bool:
    return "could not be generated at this time" in message.lower()


def _safe_float(value: str | None, default: float | None = 0.0) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    is_parenthesized_negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "").replace("$", "")
    try:
        number = float(cleaned)
    except ValueError:
        return default
    return -number if is_parenthesized_negative else number


def _normalize_trade_side(value: str | None) -> str:
    side = (value or "").strip().upper()
    if side in {"BOT", "BUY"}:
        return "BUY"
    if side in {"SLD", "SELL"}:
        return "SELL"
    return side


def summarize_xml_tags(xml_text: str, limit: int = 40) -> dict[str, int | str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"parse_error": str(exc)}
    counts: dict[str, int] = {}
    for node in root.iter():
        tag = _local_tag_name(node.tag)
        if not tag:
            continue
        counts[tag] = counts.get(tag, 0) + 1
    return dict(
        sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    )


def parse_trades(xml_text: str) -> tuple[pd.DataFrame, float | None, pd.Series, pd.DataFrame]:
    root = ET.fromstring(xml_text)
    rows = []
    for trade in _iter_xml_nodes(root, "Trade"):
        rows.append(
            {
                "symbol": _get_attr(
                    trade, "symbol", "underlyingSymbol", "description"
                )
                or "",
                "trade_date": _get_attr(trade, "tradeDate", "date", "orderDate")
                or "",
                "trade_datetime": _get_attr(
                    trade, "dateTime", "tradeTime", "dateTimeGMT"
                )
                or "",
                "buy_sell": _normalize_trade_side(
                    _get_attr(trade, "buySell", "side")
                ),
                "quantity": _safe_float(_get_attr(trade, "quantity"), 0.0) or 0.0,
                "trade_price": _safe_float(_get_attr(trade, "tradePrice"), 0.0) or 0.0,
                "proceeds": _safe_float(_get_attr(trade, "proceeds"), 0.0) or 0.0,
                "ib_commission": _safe_float(
                    _get_attr(trade, "ibCommission"), 0.0
                )
                or 0.0,
                "fifo_pnl_realized": _safe_float(
                    _get_attr(trade, "fifoPnlRealized"), 0.0
                )
                or 0.0,
                "asset_category": _get_attr(trade, "assetCategory", "assetClass")
                or "",
                "currency": _get_attr(trade, "currency") or "",
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        trade_date = pd.to_datetime(
            df["trade_date"], format="%Y%m%d", errors="coerce")
        trade_date_alt = pd.to_datetime(
            df["trade_date"], format="%d/%m/%Y", errors="coerce")
        trade_date_generic = pd.to_datetime(df["trade_date"], errors="coerce")
        df["trade_date"] = trade_date.fillna(trade_date_alt).fillna(
            trade_date_generic
        )
        trade_datetime = pd.to_datetime(
            df["trade_datetime"], format="%Y%m%d;%H%M%S", errors="coerce")
        trade_datetime_alt = pd.to_datetime(
            df["trade_datetime"], format="%d/%m/%Y;%H%M%S", errors="coerce")
        trade_datetime_generic = pd.to_datetime(
            df["trade_datetime"], errors="coerce")
        df["trade_datetime"] = trade_datetime.fillna(trade_datetime_alt).fillna(
            trade_datetime_generic
        )
    totals = []
    for node in _iter_xml_nodes(root, "EquitySummaryByReportDateInBase"):
        report_date = _get_attr(node, "reportDate", "date")
        total = _safe_float(_get_attr(node, "total"), None)
        if report_date and total is not None:
            parsed_date = pd.to_datetime(
                report_date, format="%Y%m%d", errors="coerce")
            if pd.isna(parsed_date):
                parsed_date = pd.to_datetime(
                    report_date, format="%d/%m/%Y", errors="coerce")
            if pd.notna(parsed_date):
                totals.append((parsed_date, total))
    if totals:
        totals.sort(key=lambda x: x[0])
        latest_total = totals[-1][1]
        equity_series = pd.Series(
            {date: total for date, total in totals}).sort_index()
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
                "position_value": _safe_float(
                    _get_attr(node, "marketValue", "positionValue"), None
                ),
                "unrealized_pnl": _safe_float(
                    _get_attr(node, "unrealizedPnl",
                              "unrealizedPnL", "fifoPnlUnrealized"),
                    None,
                ),
                "cost_basis": _safe_float(
                    _get_attr(node, "costBasisMoney", "costBasis"), None
                ),
                "avg_cost": _safe_float(
                    _get_attr(node, "avgCost", "averageCost",
                              "costBasisPrice", "openPrice"),
                    None,
                ),
                "unrealized_pnl_pct": _safe_float(
                    _get_attr(
                        node,
                        "unrealizedPnlPercent",
                        "unrealizedPnlPct",
                        "unrealizedPnlPercentOfNAV",
                    ),
                    None,
                ),
                "percent_of_nav": _safe_float(
                    _get_attr(
                        node,
                        "percentOfNAV",
                        "percentageOfNAV",
                        "percentOfNetLiq",
                        "percentOfNetLiquidation",
                    ),
                    None,
                ),
                "level_of_detail": node.get("levelOfDetail", ""),
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
    return df, latest_total, equity_series, open_positions


def load_default_xml() -> tuple[str | None, dict]:
    return fetch_flex_xml()


TRADE_NOTES_PATH = "trade_notes.json"
POSITION_SETTINGS_KEY = "__position__"


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


def get_symbol_position_settings(notes: dict, symbol: str) -> dict:
    symbol_notes = notes.get(symbol, {})
    settings = symbol_notes.get(POSITION_SETTINGS_KEY, {})
    return settings if isinstance(settings, dict) else {}


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
FLEX_GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
FLEX_QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")
FLEX_TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
FLEX_VERSION = os.environ.get("IBKR_FLEX_VERSION", "3")
FLEX_USER_AGENT = "TradesDashboard/1.0"
FLEX_SEND_RETRY_ATTEMPTS = 3
FLEX_SEND_RETRY_DELAY_SECONDS = 2
FLEX_GET_POLL_ATTEMPTS = 10
FLEX_GET_POLL_DELAY_SECONDS = 2


def _fetch_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": FLEX_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def fetch_flex_xml() -> tuple[str | None, dict]:
    debug: dict[str, str] = {}
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        debug["configuration_error"] = (
            "Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID environment variables."
        )
        return None, debug
    try:
        params = urllib.parse.urlencode(
            {"t": FLEX_TOKEN, "q": FLEX_QUERY_ID, "v": FLEX_VERSION}
        )
        send_url = f"{FLEX_BASE_URL}?{params}"
        debug["send_url"] = send_url
        for attempt in range(FLEX_SEND_RETRY_ATTEMPTS):
            response_text = _fetch_url_text(send_url)
            debug["send_response"] = response_text[:4000]
            root = ET.fromstring(response_text)
            status = _find_first_text(root, "Status").upper()
            service_message = _flex_service_error(root)
            debug["send_status"] = status
            debug["send_error"] = service_message
            debug["send_attempts"] = str(attempt + 1)
            ref_code = _flex_reference_code(root)
            debug["reference_code"] = ref_code or ""
            if ref_code:
                break
            if _is_flex_service_response(root) and _is_flex_send_retryable(service_message):
                time.sleep(FLEX_SEND_RETRY_DELAY_SECONDS)
                continue
            if _is_flex_service_response(root) and service_message and status != "SUCCESS":
                return None, debug
            if status and status != "SUCCESS":
                return None, debug
        if not ref_code:
            return None, debug
        get_params = urllib.parse.urlencode(
            {"t": FLEX_TOKEN, "q": ref_code, "v": FLEX_VERSION}
        )
        get_url = f"{FLEX_GET_URL}?{get_params}"
        debug["get_url"] = get_url
        last_response = ""
        for attempt in range(FLEX_GET_POLL_ATTEMPTS):
            xml_text = _fetch_url_text(get_url)
            last_response = xml_text
            root = ET.fromstring(xml_text)
            status = _find_first_text(root, "Status").upper()
            service_message = _flex_service_error(root)
            if _is_flex_service_response(root):
                debug["get_status"] = status
                debug["get_error"] = service_message
                debug["get_attempts"] = str(attempt + 1)
                if _is_flex_generation_pending(root):
                    time.sleep(FLEX_GET_POLL_DELAY_SECONDS)
                    continue
                if status and status != "SUCCESS":
                    debug["get_response"] = xml_text[:4000]
                    return None, debug

            if not any(_iter_xml_nodes(root, "Trade")) and service_message:
                debug["get_status"] = status
                debug["get_error"] = service_message
                debug["get_response"] = xml_text[:4000]
                return None, debug

            debug["get_response"] = xml_text[:4000]
            debug["get_attempts"] = str(attempt + 1)
            return xml_text, debug
        debug["get_response"] = last_response[:4000]
        total_wait_seconds = FLEX_GET_POLL_ATTEMPTS * FLEX_GET_POLL_DELAY_SECONDS
        debug["get_error"] = (
            f"Statement generation did not finish before timeout "
            f"({total_wait_seconds} seconds)."
        )
        return None, debug
    except Exception as exc:
        debug["exception"] = str(exc)
        return None, debug


def format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def format_money_rounded(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def format_price(value: float) -> str:
    return f"{value:.1f}"


def format_pnl(value: float) -> str:
    return f"{value:,.0f}$"


def format_pnl_signed(value: float) -> str:
    if value is None or pd.isna(value):
        return ""
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}{abs(value):,.0f}$"


def format_percent(value: float) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{value:.1f}%"


def format_of_position(value: float | None) -> str:
    if value is None or pd.isna(value) or value == 0:
        return ""
    ratio = abs(float(value))
    fraction = Fraction(ratio).limit_denominator(10)
    return f"{fraction.numerator}/{fraction.denominator}"


def format_date(value: pd.Timestamp | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.to_datetime(value).strftime("%d/%m/%Y")


def format_export_datetime(value: pd.Timestamp | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M")


def _export_quarter_for_date(value: pd.Timestamp) -> tuple[int, str]:
    date_value = pd.to_datetime(value)
    month = date_value.month
    if month in (12, 1, 2):
        year = date_value.year + 1 if month == 12 else date_value.year
        return year, "Q1"
    if month in (3, 4, 5):
        return date_value.year, "Q2"
    if month in (6, 7, 8):
        return date_value.year, "Q3"
    return date_value.year, "Q4"


def _safe_export_number(value: object, decimals: int | None = None):
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if decimals is None:
        return number
    return round(number, decimals)


def _safe_folder_name(value: object) -> str:
    text = str(value or "").strip().upper() or "UNKNOWN"
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in text)
    return cleaned.rstrip(" .") or "UNKNOWN"


def _next_trade_folder(parent_dir: str, symbol: object) -> str:
    base_name = _safe_folder_name(symbol)
    candidate = os.path.join(parent_dir, base_name)
    if not os.path.exists(candidate):
        return candidate
    counter = 1
    while True:
        candidate = os.path.join(parent_dir, f"{base_name}#{counter}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _trade_key_for_group(group: pd.DataFrame) -> str:
    trade_date_key = group["trade_date"].min()
    buy_sell = group["buy_sell"].iloc[0]
    quantity = group["quantity"].sum()
    trade_price = (
        (group["trade_price"] * group["quantity"]).sum() / quantity
        if quantity
        else 0.0
    )
    proceeds = group["proceeds"].sum()
    return f"{trade_date_key}|{buy_sell}|{quantity}|{trade_price}|{proceeds}"


def stop_percent_for_closed_trade(
    closed_row: pd.Series,
    segmented_trades: pd.DataFrame,
    notes_store: dict,
):
    symbol = closed_row.get("symbol")
    segment_id = closed_row.get("segment_id")
    if symbol is None or segment_id is None or pd.isna(segment_id):
        return None
    symbol_notes = notes_store.get(symbol, {})
    if not isinstance(symbol_notes, dict):
        return None
    segment_trades = segmented_trades[
        (segmented_trades["symbol"] == symbol)
        & (segmented_trades["segment_id"] == segment_id)
    ].copy()
    if segment_trades.empty:
        return None
    segment_trades["trade_time_bucket"] = pd.to_datetime(
        segment_trades["trade_time"]
    ).dt.floor("3min")
    grouped = segment_trades.groupby(["trade_time_bucket", "buy_sell"], as_index=False)
    for _, group in grouped:
        if group["buy_sell"].iloc[0] != "BUY":
            continue
        note = symbol_notes.get(_trade_key_for_group(group))
        if isinstance(note, dict):
            stop_percent = note.get("Stop %")
            if stop_percent is not None and not pd.isna(stop_percent):
                return float(stop_percent)
    return None


def export_closed_trades(
    trades: pd.DataFrame,
    segmented_trades: pd.DataFrame,
    notes_store: dict,
) -> tuple[int, list[str], list[str]]:
    exported_paths = []
    skipped = []
    for _, row in trades.iterrows():
        pnl = row.get("pnl")
        if pnl is None or pd.isna(pnl) or pnl == 0:
            skipped.append(f"{row.get('symbol', 'UNKNOWN')}: breakeven trade")
            continue
        side = "winning" if pnl > 0 else "losing"
        exit_date = pd.to_datetime(
            row.get("exit_time_dt", row.get("exit_date")), errors="coerce"
        )
        if pd.isna(exit_date):
            skipped.append(f"{row.get('symbol', 'UNKNOWN')}: missing exit date")
            continue
        entry_date_value = row.get("entry_time_dt", row.get("entry_date"))
        exit_date_value = row.get("exit_time_dt", row.get("exit_date"))
        if side == "losing":
            entry_date_export = format_export_datetime(entry_date_value)
            exit_date_export = format_export_datetime(exit_date_value)
        else:
            entry_date_export = format_date(entry_date_value)
            exit_date_export = format_date(exit_date_value)
        year, quarter = _export_quarter_for_date(exit_date)
        parent_dir = os.path.join(HISTORY_EXPORT_BASE_DIRS[side], str(year), quarter)
        os.makedirs(parent_dir, exist_ok=True)
        trade_dir = _next_trade_folder(parent_dir, row.get("symbol"))
        os.makedirs(trade_dir, exist_ok=False)
        payload = {
            "entryDate": entry_date_export,
            "exitDate": exit_date_export,
            "entryPrice": _safe_export_number(row.get("entry_price")),
            "exitPrice": _safe_export_number(row.get("exit_price")),
            "stopPercent": _safe_export_number(
                stop_percent_for_closed_trade(row, segmented_trades, notes_store)
            ),
            "portfolioPercent": _safe_export_number(
                row.get("pnl_portfolio_pct"), decimals=2
            ),
            "gainPercent": _safe_export_number(row.get("pnl_pct"), decimals=2),
            "setup": "",
            "entryTactic": "",
        }
        trade_path = os.path.join(trade_dir, "trade.json")
        with open(trade_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, allow_nan=False)
        exported_paths.append(trade_dir)
    return len(exported_paths), exported_paths, skipped


def center_table(styler):
    return (
        styler.set_properties(**{"text-align": "center"})
        .set_table_styles(
            [
                {"selector": "th", "props": [("text-align", "center")]},
                {"selector": "td", "props": [("text-align", "center")]},
            ]
        )
        .hide(axis="index")
    )


def equity_for_date(equity_series: pd.Series, trade_date: pd.Timestamp | None) -> float | None:
    if equity_series.empty or trade_date is None or pd.isna(trade_date):
        return None
    idx = equity_series.index.searchsorted(
        pd.to_datetime(trade_date), side="right") - 1
    if idx < 0:
        return None
    return float(equity_series.iloc[idx])


def add_trade_segments(trades: pd.DataFrame) -> pd.DataFrame:
    trades = trades.sort_values(
        ["symbol", "trade_datetime", "trade_date"]
    ).reset_index(drop=True)
    trades = trades.copy()
    trades["trade_time"] = trades["trade_datetime"].where(
        pd.notna(trades["trade_datetime"]),
        trades["trade_date"],
    )
    trades["signed_qty"] = trades.apply(
        lambda row: abs(row["quantity"])
        * (1 if row["buy_sell"] == "BUY" else -1),
        axis=1,
    )
    trades["position_running"] = trades.groupby(
        "symbol")["signed_qty"].cumsum()
    trades["segment_id"] = (
        trades.groupby("symbol")["position_running"]
        .apply(lambda s: s.eq(0).cumsum().shift(1, fill_value=0))
        .reset_index(level=0, drop=True)
    )
    trades["segment_max"] = (
        trades.groupby(["symbol", "segment_id"])["position_running"]
        .transform(lambda s: s.abs().max())
    )
    return trades


def format_datetime_short(value: pd.Timestamp | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.to_datetime(value).strftime("%d/%m/%Y %H:%M")


def build_backtest_rows(closed_range: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for idx, row in closed_range.reset_index(drop=True).iterrows():
        exit_time = row.get("exit_time_dt", row.get("exit_date"))
        exit_key = (
            pd.to_datetime(exit_time).isoformat()
            if exit_time is not None and pd.notna(exit_time)
            else str(idx)
        )
        row_id = (
            f"{row.get('symbol')}|{row.get('segment_id')}|{exit_key}|"
            f"{row.get('quantity')}|{row.get('exit_price')}"
        )
        rows.append(
            {
                "row_id": row_id,
                "Included": True,
                "State": "Included",
                "Ticker": row.get("symbol"),
                "Direction": row.get("direction", "LONG"),
                "Entry Date": row.get("entry_time_dt", row.get("entry_date")),
                "Entry Price": row.get("entry_price"),
                "Exit Price": row.get("exit_price"),
                "Result %": row.get("pnl_pct", 0.0),
            }
        )
    return pd.DataFrame(rows)


def refresh_backtest_calcs(table: pd.DataFrame) -> pd.DataFrame:
    refreshed = table.copy()
    if refreshed.empty:
        return refreshed
    refreshed["State"] = refreshed["Included"].map(
        lambda included: "Included" if included else "Excluded"
    )
    return refreshed


def backtest_stats(table: pd.DataFrame) -> dict[str, float | int]:
    if table.empty:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
        }
    included = table[table["Included"]].copy()
    if included.empty:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
        }
    result_pct = pd.to_numeric(included["Result %"], errors="coerce").fillna(0.0)
    winners = result_pct[result_pct > 0]
    losers = result_pct[result_pct < 0]
    total_decided = len(winners) + len(losers)
    return {
        "total": len(included),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": (len(winners) / total_decided * 100) if total_decided else 0.0,
        "total_return": result_pct.sum(),
        "avg_winner": winners.mean() if not winners.empty else 0.0,
        "avg_loser": losers.mean() if not losers.empty else 0.0,
    }


def build_mock_open_position(
    trades: pd.DataFrame,
    segmented: pd.DataFrame,
    symbol: str,
    days: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty or segmented.empty:
        return trades, pd.DataFrame()
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
    recent = segmented[
        (segmented["symbol"] == symbol) & (segmented["trade_time"] >= cutoff)
    ].copy()
    if recent.empty:
        return trades, pd.DataFrame()
    latest_idx = recent["trade_time"].idxmax()
    segment_id = int(recent.loc[latest_idx, "segment_id"])
    segment_trades = segmented[
        (segmented["symbol"] == symbol) & (
            segmented["segment_id"] == segment_id)
    ].copy()
    if segment_trades.empty:
        return trades, pd.DataFrame()

    sells = segment_trades[segment_trades["buy_sell"] == "SELL"]
    last_sell_time = None
    if not sells.empty:
        last_sell_time = sells["trade_time"].max()
        segment_trades = segment_trades[
            ~(
                (segment_trades["buy_sell"] == "SELL")
                & (segment_trades["trade_time"] == last_sell_time)
            )
        ]

    signed_qty = segment_trades.apply(
        lambda row: abs(row["quantity"])
        * (1 if row["buy_sell"] == "BUY" else -1),
        axis=1,
    )
    net_qty = signed_qty.sum()
    if net_qty == 0:
        return trades, pd.DataFrame()

    buys = segment_trades[segment_trades["buy_sell"] == "BUY"]
    avg_cost = None
    if not buys.empty:
        buy_qty = buys["quantity"].abs().sum()
        if buy_qty:
            avg_cost = (buys["quantity"].abs() *
                        buys["trade_price"]).sum() / buy_qty
    last_price = None
    if not segment_trades["trade_price"].dropna().empty:
        last_row = segment_trades.sort_values("trade_time").iloc[-1]
        last_price = float(last_row["trade_price"])

    cost_basis = abs(net_qty) * avg_cost if avg_cost else None
    mark_price = last_price if last_price is not None else avg_cost
    position_value = abs(net_qty) * \
        mark_price if mark_price is not None else None
    unrealized_pnl = (
        (mark_price - avg_cost) * net_qty
        if (mark_price is not None and avg_cost is not None)
        else None
    )

    mock_row = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "position": float(net_qty),
                "mark_price": mark_price,
                "position_value": position_value,
                "unrealized_pnl": unrealized_pnl,
                "cost_basis": cost_basis,
                "avg_cost": avg_cost,
                "unrealized_pnl_pct": None,
                "percent_of_nav": None,
                "level_of_detail": "MOCK",
            }
        ]
    )

    updated_segmented = segmented.copy()
    if last_sell_time is not None:
        updated_segmented = updated_segmented[
            ~(
                (updated_segmented["symbol"] == symbol)
                & (updated_segmented["segment_id"] == segment_id)
                & (updated_segmented["buy_sell"] == "SELL")
                & (updated_segmented["trade_time"] == last_sell_time)
            )
        ]
    open_filtered = updated_segmented[trades.columns].copy()
    return open_filtered, mock_row


@st.cache_data(show_spinner=False, ttl=60)
def fetch_latest_prices(symbols: list[str]) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
    prices: dict[str, float] = {}
    debug: dict[str, dict[str, str]] = {}
    if not symbols:
        return prices, debug
    try:
        data = yf.download(
            tickers=" ".join(symbols),
            period="1d",
            interval="1m",
            prepost=True,
            group_by="ticker",
            progress=False,
        )
    except Exception:
        data = None
    if data is None or data.empty:
        return prices, debug
    for symbol in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                series = data[symbol]["Close"].dropna()
            else:
                series = data["Close"].dropna()
            if not series.empty:
                last_value = float(series.iloc[-1])
                prices[symbol] = last_value
                debug[symbol] = {
                    "last_timestamp": str(series.index[-1]),
                    "last_close": f"{last_value}",
                    "bar_count": f"{len(series)}",
                }
        except Exception:
            continue
    return prices, debug


def match_trades(trades: pd.DataFrame, live_prices: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = add_trade_segments(trades)
    open_lots: dict[str, list[dict]] = {}
    closed_rows = []

    for _, row in trades.iterrows():
        symbol = row["symbol"]
        if symbol not in open_lots:
            open_lots[symbol] = []

        qty = float(row["quantity"] or 0)
        price = float(row["trade_price"] or 0)
        side = row["buy_sell"]
        trade_time = row["trade_datetime"] if pd.notna(
            row["trade_datetime"]) else row["trade_date"]
        segment_max = row.get("segment_max")
        segment_id = row.get("segment_id")

        if side == "SELL":
            qty = -abs(qty)
        else:
            qty = abs(qty)

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

            if lot["qty"] > 0:
                pnl = (price - lot["price"]) * abs(close_qty)
                side_label = "LONG"
            else:
                pnl = (lot["price"] - price) * abs(close_qty)
                side_label = "SHORT"

            closed_rows.append(
                {
                    "symbol": symbol,
                    "quantity": abs(close_qty),
                    "direction": side_label,
                    "entry_price": lot["price"],
                    "exit_price": price,
                    "entry_time": lot["time"],
                    "exit_time": trade_time,
                    "pnl": pnl,
                    "segment_max": float(segment_max)
                    if segment_max is not None and pd.notna(segment_max)
                    else None,
                    "segment_id": int(segment_id)
                    if segment_id is not None and pd.notna(segment_id)
                    else None,
                }
            )

            lot["qty"] += close_qty
            remaining -= close_qty

            if lot["qty"] == 0:
                lots.pop(0)

        if remaining != 0:
            lots.append({"qty": remaining, "price": price, "time": trade_time})

    open_rows = []
    last_prices = (
        trades.dropna(subset=["trade_price"])
        .sort_values(["symbol", "trade_datetime", "trade_date"])
        .groupby("symbol")["trade_price"]
        .last()
        .to_dict()
    )
    today = pd.Timestamp.now().normalize()
    for symbol, lots in open_lots.items():
        net_qty = 0.0
        cost_basis = 0.0
        entry_times = []
        for lot in lots:
            if lot["qty"] == 0:
                continue
            net_qty += lot["qty"]
            cost_basis += abs(lot["qty"]) * lot["price"]
            if pd.notna(lot["time"]):
                entry_times.append(lot["time"])
        if net_qty == 0:
            continue
        avg_entry = cost_basis / abs(net_qty)
        last_price = float(live_prices.get(
            symbol, last_prices.get(symbol, avg_entry)))
        if net_qty > 0:
            unrealized = (last_price - avg_entry) * abs(net_qty)
            pnl_pct = (last_price - avg_entry) / \
                avg_entry * 100 if avg_entry else 0.0
        else:
            unrealized = (avg_entry - last_price) * abs(net_qty)
            pnl_pct = (avg_entry - last_price) / \
                avg_entry * 100 if avg_entry else 0.0
        entry_time = min(entry_times) if entry_times else pd.NaT
        holding_days = None
        if pd.notna(entry_time):
            holding_days = (
                today - pd.to_datetime(entry_time).normalize()).days
        direction = "LONG" if net_qty > 0 else "SHORT"
        open_rows.append(
            {
                "entry_date": entry_time.date() if pd.notna(entry_time) else None,
                "symbol": symbol,
                "quantity": abs(net_qty),
                "direction": direction,
                "avg_entry": avg_entry,
                "pnl_pct": pnl_pct,
                "unrealized_pnl": unrealized,
                "holding_days": holding_days,
                "last_price": last_price,
            }
        )

    open_df = pd.DataFrame(open_rows)
    if not open_df.empty:
        open_df = open_df.sort_values(["symbol"], ignore_index=True)
    else:
        open_df = pd.DataFrame(
            columns=[
                "entry_date",
                "symbol",
                "quantity",
                "direction",
                "avg_entry",
                "pnl_pct",
                "unrealized_pnl",
                "holding_days",
                "last_price",
            ]
        )

    closed_df = pd.DataFrame(closed_rows)
    if not closed_df.empty:
        closed_df = closed_df.sort_values(
            ["exit_time", "symbol"], ignore_index=True
        )
    else:
        closed_df = pd.DataFrame(
            columns=[
                "symbol",
                "quantity",
                "direction",
                "entry_price",
                "exit_price",
                "entry_time",
                "exit_time",
                "pnl",
                "segment_max",
                "segment_id",
            ]
        )
    if not closed_df.empty:
        closed_df["exit_hour"] = pd.to_datetime(
            closed_df["exit_time"]).dt.floor("H")
        closed_df["entry_time"] = pd.to_datetime(closed_df["entry_time"])
        closed_df["exit_time"] = pd.to_datetime(closed_df["exit_time"])
        closed_df = (
            closed_df.groupby(["symbol", "exit_hour"], as_index=False)
            .apply(
                lambda group: pd.Series(
                    {
                        "quantity": group["quantity"].sum(),
                        "direction": group["direction"].iloc[0],
                        "entry_price": (group["entry_price"] * group["quantity"]).sum()
                        / group["quantity"].sum(),
                        "exit_price": (group["exit_price"] * group["quantity"]).sum()
                        / group["quantity"].sum(),
                        "entry_time": group["entry_time"].min(),
                        "exit_time": group["exit_time"].max(),
                        "pnl": group["pnl"].sum(),
                        "segment_max": group["segment_max"].max(),
                        "segment_id": group["segment_id"].max(),
                    }
                )
            )
            .reset_index(drop=True)
        )
        closed_df["exit_time_dt"] = closed_df["exit_time"]
        closed_df["entry_time_dt"] = closed_df["entry_time"]
        closed_df["of_position"] = closed_df.apply(
            lambda row: (
                row["quantity"] / row["segment_max"]
                if row["segment_max"] not in (None, 0)
                else None
            ),
            axis=1,
        )
        closed_df["entry_date"] = pd.to_datetime(
            closed_df["entry_time"]).dt.date
        closed_df["exit_date"] = pd.to_datetime(closed_df["exit_time"]).dt.date
        closed_df["exit_time_str"] = pd.to_datetime(
            closed_df["exit_time"]
        ).dt.strftime("%H:%M")
        closed_df["market_value"] = closed_df["exit_price"] * \
            closed_df["quantity"]
        closed_df["holding_days"] = (
            pd.to_datetime(closed_df["exit_time"]).dt.normalize()
            - pd.to_datetime(closed_df["entry_time"]).dt.normalize()
        ).dt.days
        closed_df = closed_df.drop(columns=["entry_time", "exit_time"])
    return open_df, closed_df


st.set_page_config(page_title="Trades Dashboard", layout="wide")

st.title("Trades Dashboard")

if "xml_text" not in st.session_state:
    xml_text, flex_debug = load_default_xml()
    st.session_state["xml_text"] = xml_text
    st.session_state["flex_debug"] = flex_debug
if "xml_source" not in st.session_state:
    st.session_state["xml_source"] = "ib_flex"
if "trade_notes" not in st.session_state:
    st.session_state["trade_notes"] = load_trade_notes()

controls = st.columns(3)
with controls[0]:
    if st.button("Refresh IB Data"):
        xml_text, flex_debug = load_default_xml()
        st.session_state["xml_text"] = xml_text
        st.session_state["flex_debug"] = flex_debug
        st.session_state["xml_source"] = "ib_flex"
        st.rerun()
with controls[1]:
    if st.button("Refresh Market Data"):
        fetch_latest_prices.clear()
        st.rerun()
with controls[2]:
    uploaded_xml = st.file_uploader(
        "Upload XML fallback",
        type=["xml"],
        key="uploaded_xml_file",
        help="Use your own IB Flex XML file when live fetch is unavailable.",
    )
source_col1, source_col2 = st.columns([1, 1])
with source_col1:
    if uploaded_xml is not None and st.button("Use Uploaded XML"):
        try:
            st.session_state["xml_text"] = uploaded_xml.getvalue().decode("utf-8")
            st.session_state["flex_debug"] = {
                "source": "uploaded_xml",
                "file_name": uploaded_xml.name,
            }
            st.session_state["xml_source"] = "uploaded_xml"
            st.rerun()
        except UnicodeDecodeError:
            st.error("The uploaded XML file must be UTF-8 encoded.")
with source_col2:
    if st.session_state.get("xml_source") == "uploaded_xml":
        if st.button("Switch Back To IB"):
            xml_text, flex_debug = load_default_xml()
            st.session_state["xml_text"] = xml_text
            st.session_state["flex_debug"] = flex_debug
            st.session_state["xml_source"] = "ib_flex"
            st.rerun()

if st.session_state.get("xml_source") == "uploaded_xml":
    uploaded_name = (st.session_state.get("flex_debug") or {}).get("file_name", "")
    st.caption(f"Source: Uploaded XML{f' ({uploaded_name})' if uploaded_name else ''}")
else:
    st.caption("Source: Live IB Flex")

xml_text = st.session_state.get("xml_text")

st.markdown(
    """
    <style>
      .stDataFrame td, .stDataFrame th {
        text-align: center !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

if not xml_text:
    flex_debug = st.session_state.get("flex_debug") or {}
    flex_error = (
        flex_debug.get("get_error")
        or flex_debug.get("send_error")
        or flex_debug.get("exception")
    )
    st.error(
        "Unable to fetch data from IB Flex. Check your Flex Web Service "
        "configuration, query ID, and token."
    )
    if flex_error:
        st.warning(f"IB Flex detail: {flex_error}")
    if flex_debug:
        with st.expander("Flex Debug"):
            st.json(flex_debug)
    st.stop()

try:
    df, total_portfolio, equity_series, open_positions = parse_trades(xml_text)
except ET.ParseError as exc:
    st.error("IB Flex returned XML that could not be parsed.")
    st.warning(f"XML parse detail: {exc}")
    flex_debug = st.session_state.get("flex_debug") or {}
    if flex_debug:
        with st.expander("Flex Debug"):
            st.json(flex_debug)
    st.stop()

if df.empty:
    st.warning("No trades found in the IB Flex response.")
    flex_debug = st.session_state.get("flex_debug") or {}
    if flex_debug:
        with st.expander("Flex Debug"):
            st.json(flex_debug)
    with st.expander("Detected XML Tags"):
        st.json(summarize_xml_tags(xml_text))
    st.stop()

filtered = df.copy()
filtered = filtered[filtered["asset_category"] != "CASH"]
filtered = filtered[filtered["symbol"].str.len() > 0]
segmented_trades = add_trade_segments(filtered)
open_filtered = filtered

if filtered.empty:
    st.warning("No trades match the current filters.")
    st.stop()

trade_dates = filtered["trade_date"].dropna()
date_min = trade_dates.min()
date_max = trade_dates.max()

if not open_positions.empty:
    symbols = sorted(open_positions["symbol"].dropna().unique().tolist())
else:
    symbols = []
open_positions_data = open_positions
open_price_map = {}
if not open_positions_data.empty:
    open_price_map = (
        open_positions_data.dropna(subset=["symbol", "mark_price"])
        .set_index("symbol")["mark_price"]
        .to_dict()
    )
live_prices, market_debug = fetch_latest_prices(symbols)
for symbol, price in open_price_map.items():
    live_prices.setdefault(symbol, price)
open_trades, _ = match_trades(open_filtered, live_prices)
_, closed_trades = match_trades(filtered, live_prices)
st.session_state["market_debug"] = market_debug

ib_open_map: dict[str, dict[str, float]] = {}
if not open_positions_data.empty:
    for _, row in open_positions_data.iterrows():
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        position = row.get("position")
        cost_basis = row.get("cost_basis")
        avg_cost = row.get("avg_cost")
        position = 0.0 if position is None or pd.isna(
            position) else float(position)
        cost_basis = None if cost_basis is None or pd.isna(
            cost_basis) else float(cost_basis)
        avg_cost = None if avg_cost is None or pd.isna(
            avg_cost) else float(avg_cost)
        if (cost_basis is None or cost_basis == 0) and avg_cost and position:
            cost_basis = abs(position) * avg_cost
        unrealized_pnl = row.get("unrealized_pnl")
        position_value = row.get("position_value")
        unrealized_pnl = None if unrealized_pnl is None or pd.isna(
            unrealized_pnl) else float(unrealized_pnl)
        position_value = None if position_value is None or pd.isna(
            position_value) else float(position_value)
        ib_open_map[symbol] = {
            "unrealized_pnl": unrealized_pnl,
            "position_value": position_value,
            "cost_basis": cost_basis,
        }

if not open_trades.empty:
    open_trades["equity_at_entry"] = open_trades["entry_date"].apply(
        lambda d: equity_for_date(equity_series, pd.to_datetime(d))
    )
    open_trades["position_value"] = open_trades["quantity"] * \
        open_trades["last_price"]
    open_trades["cost_basis"] = open_trades["quantity"] * \
        open_trades["avg_entry"]
    if ib_open_map:
        def _apply_ib_open(row: pd.Series) -> pd.Series:
            ib_row = ib_open_map.get(row["symbol"])
            if not ib_row:
                return row
            if ib_row["position_value"] is not None:
                row["position_value"] = ib_row["position_value"]
            if ib_row["cost_basis"] is not None:
                row["cost_basis"] = ib_row["cost_basis"]
                if row["quantity"]:
                    row["avg_entry"] = ib_row["cost_basis"] / row["quantity"]
            if ib_row["unrealized_pnl"] is not None:
                row["unrealized_pnl"] = ib_row["unrealized_pnl"]
            return row

        open_trades = open_trades.apply(_apply_ib_open, axis=1)
    open_trades["portfolio_pct"] = open_trades.apply(
        lambda row: (row["position_value"] / row["equity_at_entry"] * 100)
        if row["equity_at_entry"]
        else 0.0,
        axis=1,
    )
    open_trades["pnl_pct"] = open_trades.apply(
        lambda row: (row["unrealized_pnl"] / row["cost_basis"] * 100)
        if row["cost_basis"]
        else 0.0,
        axis=1,
    )
    open_trades["pnl_portfolio_pct"] = open_trades.apply(
        lambda row: (row["unrealized_pnl"] / row["equity_at_entry"] * 100)
        if row["equity_at_entry"]
        else 0.0,
        axis=1,
    )

if not closed_trades.empty:
    closed_trades["equity_at_entry"] = closed_trades["entry_date"].apply(
        lambda d: equity_for_date(equity_series, pd.to_datetime(d))
    )
    closed_trades["position_value"] = closed_trades["entry_price"] * \
        closed_trades["quantity"]
    closed_trades["pnl_pct"] = closed_trades.apply(
        lambda row: (row["pnl"] / row["position_value"] * 100)
        if row["position_value"]
        else 0.0,
        axis=1,
    )
    closed_trades["pnl_portfolio_pct"] = closed_trades.apply(
        lambda row: (row["pnl"] / row["equity_at_entry"] * 100)
        if row["equity_at_entry"]
        else 0.0,
        axis=1,
    )

total_trades = len(closed_trades)
total_pnl = closed_trades["pnl"].sum() if not closed_trades.empty else 0.0
total_unrealized = open_trades["unrealized_pnl"].sum(
) if not open_trades.empty else 0.0
wins = (closed_trades["pnl"] > 0).sum() if not closed_trades.empty else 0
losses = (closed_trades["pnl"] < 0).sum() if not closed_trades.empty else 0
flats = (closed_trades["pnl"] == 0).sum() if not closed_trades.empty else 0
win_rate = (wins / (wins + losses) * 100) if (wins + losses) else 0.0

total_win = closed_trades.loc[closed_trades["pnl"] >
                              0, "pnl"].sum() if not closed_trades.empty else 0.0
total_loss = closed_trades.loc[closed_trades["pnl"] <
                               0, "pnl"].sum() if not closed_trades.empty else 0.0
avg_win = (total_win / wins) if wins else 0.0
avg_loss = (total_loss / losses) if losses else 0.0
largest_win = closed_trades["pnl"].max() if not closed_trades.empty else 0.0
largest_loss = closed_trades["pnl"].min() if not closed_trades.empty else 0.0

total_commission = filtered["ib_commission"].sum()
total_unrealized = open_positions_data["unrealized_pnl"].sum(
) if not open_positions_data.empty else 0.0
avg_commission_per_trade = (
    total_commission / total_trades) if total_trades else 0.0
avg_trades_per_month = 0.0
if not closed_trades.empty:
    monthly_counts = (
        closed_trades.dropna(subset=["exit_date"])
        .groupby(pd.to_datetime(closed_trades["exit_date"]).dt.to_period("M"))
        .size()
    )
    if not monthly_counts.empty:
        avg_trades_per_month = monthly_counts.mean()
avg_loss_pct = 0.0
avg_loss_portfolio_pct = 0.0
avg_win_portfolio_pct = 0.0
avg_win_pct = 0.0
if not closed_trades.empty:
    loss_rows = closed_trades[closed_trades["pnl"] < 0]
    if not loss_rows.empty:
        loss_position_value = loss_rows["entry_price"] * loss_rows["quantity"]
        avg_loss_pct = (loss_rows["pnl"] / loss_position_value).mean() * 100
        avg_loss_portfolio_pct = loss_rows["pnl_portfolio_pct"].mean()
    win_rows = closed_trades[closed_trades["pnl"] > 0]
    if not win_rows.empty:
        win_position_value = win_rows["entry_price"] * win_rows["quantity"]
        avg_win_pct = (win_rows["pnl"] / win_position_value).mean() * 100
        avg_win_portfolio_pct = win_rows["pnl_portfolio_pct"].mean()

equity_change_pct = 0.0
if not equity_series.empty and equity_series.iloc[0] > 0:
    equity_change_pct = (
        equity_series.iloc[-1] - equity_series.iloc[0]) / equity_series.iloc[0] * 100

expectancy_pct = 0.0
profit_factor = 0.0
total_closed = wins + losses
if total_closed > 0:
    win_rate_frac = wins / total_closed
    loss_rate_frac = losses / total_closed
    expectancy_pct = win_rate_frac * avg_win_pct + loss_rate_frac * avg_loss_pct
if total_loss < 0:
    profit_factor = total_win / abs(total_loss)


open_tab, history_tab, backtesting_tab, analysis_tab = st.tabs(
    ["Open Positions", "Trade History", "Backtesting", "Analysis"])

with open_tab:
    header_text = "Open Positions (0.0% of Portfolio)"
    if open_positions_data.empty:
        st.subheader(header_text)
        st.caption("No open trades.")
    else:
        open_positions_display = open_positions_data
        if "level_of_detail" in open_positions_display.columns:
            summary_rows = open_positions_display[
                open_positions_display["level_of_detail"].str.upper(
                ) == "SUMMARY"
            ]
            if not summary_rows.empty:
                open_positions_display = summary_rows
        open_display = open_positions_display[
            [
                "symbol",
                "position",
                "cost_basis",
                "avg_cost",
            ]
        ].copy()
        open_display["symbol"] = open_display["symbol"].astype(str).str.strip()
        holding_days = (
            open_trades[["symbol", "holding_days", "entry_date"]]
            if not open_trades.empty
            else pd.DataFrame(columns=["symbol", "holding_days", "entry_date"])
        )
        open_display = open_display.merge(
            holding_days, on="symbol", how="left")
        open_display["last_price"] = open_display["symbol"].map(live_prices)
        open_display["last_price"] = open_display["last_price"].where(
            pd.notna(open_display["last_price"]),
            None,
        )
        open_display["cost_basis"] = open_display.apply(
            lambda row: row["cost_basis"]
            if row["cost_basis"]
            else (
                abs(row["position"]) * row["avg_cost"]
                if row["avg_cost"] and row["position"]
                else None
            ),
            axis=1,
        )
        open_display["market_value"] = open_display.apply(
            lambda row: row["last_price"] * row["position"]
            if row["last_price"] is not None and row["position"] is not None
            else None,
            axis=1,
        )
        open_display["unrealized_pnl"] = open_display.apply(
            lambda row: (row["last_price"] - row["avg_cost"]) * row["position"]
            if row["last_price"] is not None and row["avg_cost"] is not None and row["position"] is not None
            else (
                row["market_value"] - row["cost_basis"]
                if row["market_value"] is not None and row["cost_basis"] is not None
                else None
            ),
            axis=1,
        )
        open_display["Trade Return %"] = open_display.apply(
            lambda row: (row["unrealized_pnl"] / row["cost_basis"] * 100)
            if row["cost_basis"]
            else None,
            axis=1,
        )
        notes_store = st.session_state.get("trade_notes", {})
        open_display["Entry Price"] = open_display["avg_cost"]
        open_display["Stop Price"] = open_display["symbol"].apply(
            lambda symbol: get_symbol_position_settings(notes_store, symbol).get("stop_price")
        )
        open_display["Entry Price"] = open_display.apply(
            lambda row: get_symbol_position_settings(notes_store, row["symbol"]).get("entry_price")
            if get_symbol_position_settings(notes_store, row["symbol"]).get("entry_price") is not None
            else row["Entry Price"],
            axis=1,
        )
        open_display["Risk / Share"] = open_display.apply(
            lambda row: (
                row["Entry Price"] - row["Stop Price"]
                if pd.notna(row["Entry Price"]) and pd.notna(row["Stop Price"])
                else None
            ),
            axis=1,
        )
        open_display["Stop Loss %"] = open_display.apply(
            lambda row: (
                abs(row["Entry Price"] - row["Stop Price"])
                / abs(row["Entry Price"])
                * 100
                if pd.notna(row["Entry Price"])
                and pd.notna(row["Stop Price"])
                and row["Entry Price"] != 0
                else None
            ),
            axis=1,
        )
        open_display["Risk $"] = open_display.apply(
            lambda row: (
                abs(row["position"]) * row["Risk / Share"]
                if pd.notna(row["position"])
                and pd.notna(row["Risk / Share"])
                and row["Risk / Share"] > 0
                else None
            ),
            axis=1,
        )
        open_display["Risk %"] = open_display.apply(
            lambda row: (
                row["Risk $"] / total_portfolio * 100
                if total_portfolio and pd.notna(row["Risk $"])
                else None
            ),
            axis=1,
        )
        open_display["R Multiple"] = open_display.apply(
            lambda row: (
                row["unrealized_pnl"] / row["Risk $"]
                if pd.notna(row["unrealized_pnl"])
                and pd.notna(row["Risk $"])
                and row["Risk $"] not in (0, None)
                else None
            ),
            axis=1,
        )
        if total_portfolio:
            open_display["Portfolio %"] = (
                open_display["market_value"] / total_portfolio * 100
            )
        else:
            open_display["Portfolio %"] = None
        open_display = open_display.sort_values(
            "Trade Return %", ascending=False, ignore_index=True)
        open_display = open_display.rename(
            columns={
                "symbol": "Symbol",
                "position": "Position",
                "market_value": "Market Value",
                "unrealized_pnl": "Unrealized PnL $",
                "holding_days": "Holding Days",
                "Entry Price": "Entry Price",
                "Stop Price": "Stop Price",
                "Stop Loss %": "Stop Loss %",
                "Risk $": "Risk $",
                "Risk %": "Risk %",
                "R Multiple": "R",
            }
        )
        if st.session_state.get("market_debug") is not None:
            open_debug = {}
            for _, row in open_display.iterrows():
                symbol = row.get("Symbol", "")
                if not symbol:
                    continue
                open_debug[symbol] = {
                    "last_price": f"{row.get('last_price')}",
                    "position": f"{row.get('Position')}",
                    "cost_basis": f"{row.get('cost_basis')}",
                    "unrealized_pnl": f"{row.get('Unrealized PnL $')}",
                    "trade_return_pct": f"{row.get('Trade Return %')}",
                }
            st.session_state["open_trade_debug"] = open_debug
        if total_portfolio:
            open_display["Portfolio %"] = (
                open_display["Market Value"] / total_portfolio * 100
            )
        else:
            open_display["Portfolio %"] = None
        open_display = open_display[
            [
                "Symbol",
                "Entry Price",
                "Stop Price",
                "Stop Loss %",
                "Risk $",
                "Risk %",
                "R",
                "Trade Return %",
                "Position",
                "Market Value",
                "Portfolio %",
                "Unrealized PnL $",
                "Holding Days",
            ]
        ]
        if "Portfolio %" in open_display.columns:
            open_header_pct = open_display["Portfolio %"].sum(skipna=True)
            header_text = f"Open Positions ({open_header_pct:.1f}% of Portfolio)"
        st.subheader(header_text)
        open_styled = (
            open_display.style.format(
                {
                    "Entry Price": format_price,
                    "Stop Price": format_price,
                    "Stop Loss %": format_percent,
                    "Position": lambda v: f"{v:,.0f}" if pd.notna(v) else "",
                    "Market Value": format_money_rounded,
                    "Portfolio %": format_percent,
                    "Risk $": format_money_rounded,
                    "Risk %": format_percent,
                    "R": lambda v: f"{v:.2f}R" if pd.notna(v) else "",
                    "Trade Return %": format_percent,
                    "Unrealized PnL $": format_pnl_signed,
                },
                na_rep="",
            )
        )
        open_styled = center_table(open_styled)
        selection = st.dataframe(
            open_styled,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )
        selected_symbol = None
        if selection and selection.selection and selection.selection.get("rows"):
            selected_idx = selection.selection["rows"][0]
            if 0 <= selected_idx < len(open_display):
                selected_symbol = open_display.iloc[selected_idx]["Symbol"]
        if selected_symbol:
            selected_open_row = open_positions_display[
                open_positions_display["symbol"] == selected_symbol
            ]
            default_entry_price = None
            if not selected_open_row.empty:
                default_entry_price = selected_open_row.iloc[0].get("avg_cost")
            notes_store = st.session_state.get("trade_notes", {})
            position_settings = get_symbol_position_settings(
                notes_store, selected_symbol
            )
            saved_entry_price = position_settings.get("entry_price")
            saved_stop_price = position_settings.get("stop_price")
            entry_input_default = (
                float(saved_entry_price)
                if saved_entry_price is not None
                else (
                    float(default_entry_price)
                    if default_entry_price is not None and pd.notna(default_entry_price)
                    else 0.0
                )
            )
            stop_input_default = (
                float(saved_stop_price)
                if saved_stop_price is not None
                else 0.0
            )
            risk_col1, risk_col2, risk_col3 = st.columns([1, 1, 0.8])
            with risk_col1:
                manual_entry_price = st.number_input(
                    f"{selected_symbol} entry price",
                    min_value=0.0,
                    value=entry_input_default,
                    step=0.01,
                    key=f"entry_price_{selected_symbol}",
                )
            with risk_col2:
                manual_stop_price = st.number_input(
                    f"{selected_symbol} stop price",
                    min_value=0.0,
                    value=stop_input_default,
                    step=0.01,
                    key=f"stop_price_{selected_symbol}",
                )
            with risk_col3:
                st.caption(" ")
                if st.button("Save Risk Setup", key=f"save_position_setup_{selected_symbol}"):
                    updated = st.session_state.get("trade_notes", {})
                    updated.setdefault(selected_symbol, {})
                    updated[selected_symbol][POSITION_SETTINGS_KEY] = {
                        "entry_price": manual_entry_price or None,
                        "stop_price": manual_stop_price or None,
                    }
                    st.session_state["trade_notes"] = updated
                    save_trade_notes(updated)
                    st.rerun()
            symbol_trades = open_filtered[open_filtered["symbol"]
                                          == selected_symbol].copy()
            if symbol_trades.empty:
                st.caption("No trade details available.")
            else:
                symbol_trades["sort_time"] = symbol_trades["trade_datetime"].where(
                    pd.notna(symbol_trades["trade_datetime"]),
                    symbol_trades["trade_date"],
                )
                symbol_trades = symbol_trades.sort_values(
                    ["sort_time", "trade_date"], ascending=True
                )
                symbol_trades["signed_qty"] = symbol_trades.apply(
                    lambda row: abs(row["quantity"])
                    * (1 if row["buy_sell"] == "BUY" else -1),
                    axis=1,
                )
                symbol_trades["position_running"] = symbol_trades["signed_qty"].cumsum(
                )
                last_flat_idx = symbol_trades.index[symbol_trades["position_running"] == 0]
                if not last_flat_idx.empty:
                    symbol_trades = symbol_trades.loc[last_flat_idx[-1] + 1:]
                if symbol_trades.empty:
                    st.caption("No current-position trades available.")
                    st.stop()
                buy_rows = symbol_trades[symbol_trades["buy_sell"] == "BUY"]
                total_buy_qty = buy_rows["quantity"].abs().sum()
                total_buy_cost = (buy_rows["quantity"].abs()
                                  * buy_rows["trade_price"]).sum()
                avg_cost_for_return = (
                    total_buy_cost / total_buy_qty if total_buy_qty else None
                )
                symbol_trades["trade_time_bucket"] = pd.to_datetime(
                    symbol_trades["sort_time"]
                ).dt.floor("3min")
                grouped = symbol_trades.groupby(
                    ["trade_time_bucket", "buy_sell"], as_index=False
                )
                details = grouped.apply(
                    lambda group: pd.Series(
                        {
                            "trade_date_key": group["trade_date"].min(),
                            "trade_time": group["trade_time_bucket"].min(),
                            "buy_sell": group["buy_sell"].iloc[0],
                            "quantity": group["quantity"].sum(),
                            "trade_price": (
                                (group["trade_price"] *
                                 group["quantity"]).sum()
                                / group["quantity"].sum()
                                if group["quantity"].sum()
                                else 0.0
                            ),
                            "proceeds": group["proceeds"].sum(),
                        }
                    )
                ).reset_index(drop=True)
                details["trade_key"] = details.apply(
                    lambda row: f"{row['trade_date_key']}|{row['buy_sell']}|"
                    f"{row['quantity']}|{row['trade_price']}|{row['proceeds']}",
                    axis=1,
                )
                details = details.drop(
                    columns=["trade_time_bucket", "trade_date_key"], errors="ignore")
                details = details.rename(
                    columns={
                        "trade_time": "Trade Date",
                        "buy_sell": "Side",
                        "quantity": "Quantity",
                        "trade_price": "Price",
                        "proceeds": "Market Value",
                    }
                )
                original_position = symbol_trades["position_running"].abs(
                ).max()
                position_total = None
                avg_cost = None
                open_row = open_positions_display[
                    open_positions_display["symbol"] == selected_symbol
                ]
                if not open_row.empty:
                    position_total = open_row.iloc[0].get("position")
                    avg_cost = open_row.iloc[0].get("avg_cost")

                def _fraction_of_position(numerator: float, denominator: float) -> str:
                    if denominator in (None, 0) or numerator is None:
                        return ""
                    ratio = abs(numerator) / \
                        abs(denominator) if denominator else 0
                    if ratio <= 0:
                        return ""
                    denom = max(1, int(round(1 / ratio)))
                    return f"1/{denom}"
                details["Of Position"] = details.apply(
                    lambda row: "1/1"
                    if row["Side"] == "BUY"
                    else _fraction_of_position(row["Quantity"], original_position),
                    axis=1,
                )
                details["Trade Return %"] = details.apply(
                    lambda row: (
                        (row["Price"] / avg_cost_for_return - 1) * 100
                        if row["Side"] == "SELL" and avg_cost_for_return
                        else None
                    ),
                    axis=1,
                )
                details["Trade Date"] = details["Trade Date"].map(
                    format_datetime_short
                )
                details["Market Value"] = details["Market Value"].abs()
                details["Portfolio %"] = (
                    details["Market Value"] / total_portfolio * 100
                    if total_portfolio
                    else None
                )
                symbol_risk_per_share = (
                    manual_entry_price - manual_stop_price
                    if manual_entry_price and manual_stop_price and manual_entry_price > manual_stop_price
                    else None
                )
                details["Stop Price"] = (
                    manual_stop_price if manual_stop_price else None
                )
                details["R"] = details.apply(
                    lambda row: (
                        (row["Price"] - manual_entry_price) / symbol_risk_per_share
                        if row["Side"] == "SELL"
                        and symbol_risk_per_share
                        else None
                    ),
                    axis=1,
                )
                display_details = details[
                    [
                        "Trade Date",
                        "Side",
                        "Of Position",
                        "Quantity",
                        "Price",
                        "Trade Return %",
                        "R",
                        "Stop Price",
                        "Market Value",
                        "Portfolio %",
                    ]
                ]
                st.dataframe(
                    display_details,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Quantity": st.column_config.NumberColumn(format="%.0f"),
                        "Price": st.column_config.NumberColumn(format="%.2f"),
                        "Trade Return %": st.column_config.NumberColumn(format="%.1f%%"),
                        "R": st.column_config.NumberColumn(format="%.2fR"),
                        "Stop Price": st.column_config.NumberColumn(format="%.2f"),
                        "Market Value": st.column_config.NumberColumn(format="$%.0f"),
                        "Portfolio %": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )

with history_tab:
    st.subheader("Trade History")
    if closed_trades.empty:
        st.caption("No trade history.")
    else:
        closed_display_raw = closed_trades.copy()
        history_dates = pd.to_datetime(
            closed_display_raw.get("exit_time_dt", closed_display_raw["exit_date"]),
            errors="coerce",
        ).dropna()
        if not history_dates.empty:
            default_start = history_dates.min().date()
            default_end = history_dates.max().date()
            filter_col1, filter_col2, filter_col3 = st.columns([1.1, 1, 1])
            with filter_col1:
                result_filter = st.selectbox(
                    "Result",
                    ["All trades", "Winning trades", "Losing trades"],
                    key="history_result_filter",
                )
            with filter_col2:
                history_start = st.date_input(
                    "Start date",
                    value=default_start,
                    min_value=default_start,
                    max_value=default_end,
                    key="history_start_date",
                )
            with filter_col3:
                history_end = st.date_input(
                    "End date",
                    value=default_end,
                    min_value=default_start,
                    max_value=default_end,
                    key="history_end_date",
                )

            if history_start > history_end:
                st.warning("Start date must be on or before end date.")
                closed_display_raw = closed_display_raw.iloc[0:0]
            else:
                history_exit_dates = pd.to_datetime(
                    closed_display_raw.get(
                        "exit_time_dt", closed_display_raw["exit_date"]
                    ),
                    errors="coerce",
                ).dt.date
                closed_display_raw = closed_display_raw[
                    (history_exit_dates >= history_start)
                    & (history_exit_dates <= history_end)
                ].copy()
                if result_filter == "Winning trades":
                    closed_display_raw = closed_display_raw[
                        closed_display_raw["pnl"] > 0
                    ].copy()
                elif result_filter == "Losing trades":
                    closed_display_raw = closed_display_raw[
                        closed_display_raw["pnl"] < 0
                    ].copy()

        if closed_display_raw.empty:
            st.caption("No trade history matches the selected filters.")
        else:
            export_col1, export_col2 = st.columns([1, 4])
            with export_col1:
                if st.button(
                    "Export trades",
                    key="export_history_trades",
                    use_container_width=True,
                ):
                    try:
                        exported_count, exported_paths, skipped_exports = export_closed_trades(
                            closed_display_raw,
                            segmented_trades,
                            st.session_state.get("trade_notes", {}),
                        )
                    except OSError as exc:
                        st.error(f"Export failed: {exc}")
                    except ValueError as exc:
                        st.error(f"Export failed: {exc}")
                    else:
                        if exported_count:
                            st.success(f"Exported {exported_count} trades.")
                            with st.expander("Exported folders"):
                                for path in exported_paths:
                                    st.code(path)
                        if skipped_exports:
                            st.warning(
                                "Skipped: " + "; ".join(skipped_exports)
                            )

        if "exit_time_dt" in closed_display_raw.columns:
            closed_display_raw = closed_display_raw.sort_values(
                "exit_time_dt", ascending=False, ignore_index=True
            )
        else:
            closed_display_raw = closed_display_raw.sort_values(
                "exit_date", ascending=False, ignore_index=True
            )
        closed_display = closed_display_raw.copy()
        if "exit_time_dt" in closed_display_raw.columns:
            exit_dt = closed_display_raw["exit_time_dt"]
        else:
            exit_dt = pd.to_datetime(
                closed_display_raw["exit_date"], errors="coerce"
            )
        closed_display["status"] = closed_display["pnl"].apply(
            lambda v: "🟢 Winner" if v > 0 else (
                "🔴 Loser" if v < 0 else "🟡 Breakeven")
        )
        closed_display["Of Position"] = closed_display["of_position"].map(
            format_of_position
        )
        closed_display = closed_display.rename(
            columns={
                "symbol": "Symbol",
                "entry_date": "Entry Date",
                "exit_date": "Exit Date",
                "quantity": "Quantity",
                "entry_price": "Entry Price",
                "exit_price": "Exit Price",
                "market_value": "Market Value",
                "pnl_pct": "Trade Return %",
                "pnl": "PnL $",
                "pnl_portfolio_pct": "Portfolio Impact %",
                "holding_days": "Holding Days",
                "status": "Status",
            }
        )
        closed_display["Entry Date"] = closed_display["Entry Date"].map(
            format_date)
        closed_display["Exit Date"] = exit_dt
        closed_display = closed_display[
            [
                "Exit Date",
                "Symbol",
                "Quantity",
                "Entry Price",
                "Exit Price",
                "Market Value",
                "Of Position",
                "Trade Return %",
                "Portfolio Impact %",
                "PnL $",
                # "Entry Date",

                "Holding Days",
                "Status",
            ]
        ]
        closed_styled = (
            closed_display.style.format(
                {
                    "Quantity": lambda v: f"{v:,.0f}",
                    "Entry Price": format_price,
                    "Exit Price": format_price,
                    "Market Value": format_money_rounded,
                    "Trade Return %": format_percent,
                    "PnL $": format_pnl,
                    "Portfolio Impact %": lambda v: f"{v:.2f}%" if pd.notna(v) else "",
                }
            )
        )
        closed_styled = center_table(closed_styled)
        selection = st.dataframe(
            closed_styled,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Exit Date": st.column_config.DatetimeColumn(
                    format="DD/MM/YYYY HH:mm"
                )
            },
            on_select="rerun",
            selection_mode="single-row",
        )
        selected_history_row = None
        if selection and selection.selection and selection.selection.get("rows"):
            selected_idx = selection.selection["rows"][0]
            if 0 <= selected_idx < len(closed_display_raw):
                selected_history_row = closed_display_raw.iloc[selected_idx]
        if selected_history_row is not None:
            selected_symbol = selected_history_row.get("symbol")
            selected_segment = selected_history_row.get("segment_id")
            if selected_symbol is not None and selected_segment is not None:
                segment_trades = segmented_trades[
                    (segmented_trades["symbol"] == selected_symbol)
                    & (segmented_trades["segment_id"] == selected_segment)
                ].copy()
                if not segment_trades.empty:
                    segment_trades["trade_time_bucket"] = pd.to_datetime(
                        segment_trades["trade_time"]
                    ).dt.floor("3min")
                    grouped = segment_trades.groupby(
                        ["trade_time_bucket", "buy_sell"], as_index=False
                    )
                    details = grouped.apply(
                        lambda group: pd.Series(
                            {
                                "trade_date_key": group["trade_date"].min(),
                                "trade_time": group["trade_time_bucket"].min(),
                                "buy_sell": group["buy_sell"].iloc[0],
                                "quantity": group["quantity"].sum(),
                                "trade_price": (
                                    (group["trade_price"] *
                                     group["quantity"]).sum()
                                    / group["quantity"].sum()
                                    if group["quantity"].sum()
                                    else 0.0
                                ),
                                "proceeds": group["proceeds"].sum(),
                            }
                        )
                    ).reset_index(drop=True)
                    details["trade_key"] = details.apply(
                        lambda row: f"{row['trade_date_key']}|{row['buy_sell']}|"
                        f"{row['quantity']}|{row['trade_price']}|{row['proceeds']}",
                        axis=1,
                    )
                    details = details.drop(
                        columns=["trade_time_bucket", "trade_date_key"], errors="ignore")
                    details = details.rename(
                        columns={
                            "trade_time": "Trade Date",
                            "buy_sell": "Side",
                            "quantity": "Quantity",
                            "trade_price": "Price",
                            "proceeds": "Market Value",
                        }
                    )
                    buy_rows = segment_trades[segment_trades["buy_sell"] == "BUY"]
                    total_buy_qty = buy_rows["quantity"].abs().sum()
                    total_buy_cost = (buy_rows["quantity"].abs()
                                      * buy_rows["trade_price"]).sum()
                    avg_cost_for_return = (
                        total_buy_cost / total_buy_qty if total_buy_qty else None
                    )
                    original_position = total_buy_qty if total_buy_qty else 0

                    def _fraction_of_position(numerator: float, denominator: float) -> str:
                        if denominator in (None, 0) or numerator is None:
                            return ""
                        ratio = abs(numerator) / \
                            abs(denominator) if denominator else 0
                        if ratio <= 0:
                            return ""
                        fraction = Fraction(ratio).limit_denominator(10)
                        return f"{fraction.numerator}/{fraction.denominator}"

                    details["Of Position"] = details.apply(
                        lambda row: _fraction_of_position(
                            row["Quantity"], original_position
                        ),
                        axis=1,
                    )
                    details["Trade Return %"] = details.apply(
                        lambda row: (
                            (row["Price"] / avg_cost_for_return - 1) * 100
                            if row["Side"] == "SELL" and avg_cost_for_return
                            else None
                        ),
                        axis=1,
                    )
                    details["Trade Date"] = details["Trade Date"].map(
                        format_datetime_short)
                    details["Market Value"] = details["Market Value"].abs()
                    details["Portfolio %"] = (
                        details["Market Value"] / total_portfolio * 100
                        if total_portfolio
                        else None
                    )
                    notes_store = st.session_state.get("trade_notes", {})
                    symbol_notes = notes_store.get(selected_symbol, {})

                    def _get_note_value(key: str, field: str):
                        value = symbol_notes.get(key)
                        if isinstance(value, dict):
                            return value.get(field)
                        if field == "R":
                            return value
                        return None

                    details["R"] = details["trade_key"].apply(
                        lambda key: _get_note_value(key, "R")
                    )
                    details["Stop %"] = details["trade_key"].apply(
                        lambda key: _get_note_value(key, "Stop %")
                    )
                    stop_candidates = details.loc[
                        details["Side"] == "BUY", "Stop %"
                    ].dropna()
                    default_stop_pct = stop_candidates.iloc[0] if not stop_candidates.empty else None

                    def _calc_sell_r(row: pd.Series) -> float | None:
                        if row["Side"] != "SELL":
                            return row["R"]
                        stop_pct = row["Stop %"] if pd.notna(
                            row["Stop %"]) else default_stop_pct
                        if not stop_pct or not avg_cost_for_return:
                            return row["R"]
                        stop_fraction = float(stop_pct) / 100.0
                        if stop_fraction == 0:
                            return row["R"]
                        return (row["Price"] - avg_cost_for_return) / (
                            avg_cost_for_return * stop_fraction
                        )

                    details["R"] = details.apply(_calc_sell_r, axis=1)
                    display_details = details[
                        [
                            "Trade Date",
                            "Side",
                            "Of Position",
                            "Quantity",
                            "Price",
                            "Trade Return %",
                            "R",
                            "Stop %",
                            "Market Value",
                            "Portfolio %",
                        ]
                    ]
                    editor_key = f"history_r_editor_{selected_symbol}_{selected_segment}"
                    editor_data_key = f"{editor_key}_data"

                    def _recalculate_editor_r(table: pd.DataFrame) -> pd.DataFrame:
                        recalculated = table.copy()
                        stop_candidates = recalculated.loc[
                            recalculated["Side"] == "BUY", "Stop %"
                        ].dropna()
                        default_stop = (
                            stop_candidates.iloc[0]
                            if not stop_candidates.empty
                            else None
                        )

                        def _calc_row_r(row: pd.Series) -> float | None:
                            if row["Side"] != "SELL":
                                return row["R"]
                            stop_pct = (
                                row["Stop %"]
                                if pd.notna(row["Stop %"])
                                else default_stop
                            )
                            if not stop_pct or not avg_cost_for_return:
                                return row["R"]
                            stop_fraction = float(stop_pct) / 100.0
                            if stop_fraction == 0:
                                return row["R"]
                            return (row["Price"] - avg_cost_for_return) / (
                                avg_cost_for_return * stop_fraction
                            )

                        recalculated["R"] = recalculated.apply(
                            _calc_row_r, axis=1)
                        return recalculated

                    editor_source = st.session_state.get(editor_data_key)
                    if not isinstance(editor_source, pd.DataFrame) or set(editor_source.columns) != set(display_details.columns) or len(editor_source) != len(display_details):
                        editor_source = display_details.copy()
                    else:
                        # Keep non-editable context columns aligned with the selected trade.
                        for col in [
                            "Trade Date",
                            "Side",
                            "Of Position",
                            "Quantity",
                            "Price",
                            "Trade Return %",
                            "Market Value",
                            "Portfolio %",
                        ]:
                            editor_source[col] = display_details[col].values

                    editor_source = _recalculate_editor_r(editor_source)
                    st.session_state[editor_data_key] = editor_source

                    edited = st.data_editor(
                        editor_source,
                        use_container_width=True,
                        hide_index=True,
                        key=editor_key,
                        column_config={
                            "Quantity": st.column_config.NumberColumn(format="%.0f"),
                            "Price": st.column_config.NumberColumn(format="%.1f"),
                            "Trade Return %": st.column_config.NumberColumn(format="%.1f%%"),
                            "Market Value": st.column_config.NumberColumn(format="$%.0f"),
                            "Portfolio %": st.column_config.NumberColumn(format="%.1f%%"),
                            "R": st.column_config.NumberColumn(format="%.2f"),
                            "Stop %": st.column_config.NumberColumn(format="%.1f%%"),
                        },
                        disabled=[
                            "Trade Date",
                            "Side",
                            "Of Position",
                            "Quantity",
                            "Price",
                            "Trade Return %",
                            "R",
                            "Market Value",
                            "Portfolio %",
                        ],
                    )
                    edited = _recalculate_editor_r(edited)
                    if not edited.equals(st.session_state[editor_data_key]):
                        st.session_state[editor_data_key] = edited
                        st.rerun()
                    if st.button(
                        "Save",
                        key=f"save_history_r_{selected_symbol}_{selected_segment}",
                    ):
                        updated = st.session_state.get("trade_notes", {})
                        updated.setdefault(selected_symbol, {})
                        edited_rows = edited.to_dict(orient="records")
                        for idx, row in enumerate(edited_rows):
                            if idx >= len(details):
                                continue
                            trade_key = details.iloc[idx]["trade_key"]
                            updated[selected_symbol][trade_key] = {
                                "R": row.get("R"),
                                "Stop %": row.get("Stop %"),
                            }
                        st.session_state["trade_notes"] = updated
                        save_trade_notes(updated)
                        st.success("Saved.")

with backtesting_tab:
    st.subheader("Backtesting")
    if closed_trades.empty:
        st.caption("No closed trades available for backtesting.")
    else:
        closed_dates = pd.to_datetime(
            closed_trades["exit_date"], errors="coerce"
        ).dropna()
        default_start = closed_dates.min().date()
        default_end = closed_dates.max().date()

        range_cols = st.columns([1, 1, 1])
        with range_cols[0]:
            backtest_start = st.date_input(
                "Start date",
                value=default_start,
                min_value=default_start,
                max_value=default_end,
                key="backtest_start_date",
            )
        with range_cols[1]:
            backtest_end = st.date_input(
                "End date",
                value=default_end,
                min_value=default_start,
                max_value=default_end,
                key="backtest_end_date",
            )

        if backtest_start > backtest_end:
            st.warning("Start date must be on or before end date.")
        else:
            range_mask = (
                pd.to_datetime(closed_trades["exit_date"], errors="coerce").dt.date
                >= backtest_start
            ) & (
                pd.to_datetime(closed_trades["exit_date"], errors="coerce").dt.date
                <= backtest_end
            )
            closed_range = closed_trades[range_mask].copy()
            backtest_sort_date = (
                "entry_time_dt"
                if "entry_time_dt" in closed_range.columns
                else "entry_date"
            )
            closed_range = closed_range.sort_values(
                [backtest_sort_date, "symbol"],
                ascending=[True, True],
                ignore_index=True,
            )
            data_signature = "backtest_v3|" + "|".join(
                closed_range.apply(
                    lambda row: (
                        f"{row.get('symbol')}:{row.get('segment_id')}:"
                        f"{row.get('entry_time_dt', row.get('entry_date'))}:"
                        f"{row.get('exit_time_dt', row.get('exit_date'))}:"
                        f"{row.get('quantity')}:{row.get('entry_price')}:"
                        f"{row.get('exit_price')}"
                    ),
                    axis=1,
                ).tolist()
            )
            backtest_state_key = (
                f"backtest_rows_v3_{backtest_start.isoformat()}_"
                f"{backtest_end.isoformat()}"
            )
            backtest_signature_key = f"{backtest_state_key}_signature"

            if (
                st.session_state.get(backtest_signature_key) != data_signature
                or backtest_state_key not in st.session_state
            ):
                st.session_state[backtest_state_key] = build_backtest_rows(closed_range)
                st.session_state[backtest_signature_key] = data_signature

            with range_cols[2]:
                if st.button("Reset", key="reset_backtest"):
                    st.session_state[backtest_state_key] = build_backtest_rows(
                        closed_range
                    )
                    st.rerun()

            backtest_table = refresh_backtest_calcs(
                st.session_state[backtest_state_key]
            )
            stats = backtest_stats(backtest_table)

            stat_cols = st.columns(4)
            stat_cols[0].metric("Total Trades", f"{stats['total']}")
            stat_cols[1].metric("Winning Trades", f"{stats['wins']}")
            stat_cols[2].metric("Losing Trades", f"{stats['losses']}")
            stat_cols[3].metric("Win Rate", format_percent(stats["win_rate"]))

            stat_cols = st.columns(3)
            stat_cols[0].metric(
                "Total Return %", format_percent(stats["total_return"])
            )
            stat_cols[1].metric(
                "Average Winner", format_percent(stats["avg_winner"])
            )
            stat_cols[2].metric(
                "Average Loser", format_percent(stats["avg_loser"])
            )

            if backtest_table.empty:
                st.caption("No closed trades in the selected range.")
            else:
                editor_columns = [
                    "Included",
                    "State",
                    "Ticker",
                    "Entry Date",
                    "Entry Price",
                    "Exit Price",
                    "Result %",
                ]
                editor_table = backtest_table[editor_columns].copy()
                edited = st.data_editor(
                    editor_table,
                    use_container_width=True,
                    hide_index=True,
                    key=f"backtest_editor_v3_{backtest_start}_{backtest_end}",
                    column_config={
                        "Included": st.column_config.CheckboxColumn(
                            "Included",
                            help="Exclude a trade from the backtest without hiding it.",
                        ),
                        "Entry Date": st.column_config.DatetimeColumn(
                            format="DD/MM/YYYY HH:mm"
                        ),
                        "Entry Price": st.column_config.NumberColumn(format="%.2f"),
                        "Exit Price": st.column_config.NumberColumn(format="%.2f"),
                        "Result %": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                    disabled=[
                        "State",
                        "Ticker",
                        "Entry Date",
                        "Entry Price",
                        "Exit Price",
                        "Result %",
                    ],
                )
                updated_table = backtest_table.copy()
                updated_table["Included"] = edited["Included"].values
                updated_table = refresh_backtest_calcs(updated_table)
                if not updated_table.equals(st.session_state[backtest_state_key]):
                    st.session_state[backtest_state_key] = updated_table
                    st.rerun()
                excluded_count = int((~updated_table["Included"]).sum())
                if excluded_count:
                    st.caption(
                        f"{excluded_count} excluded trade"
                        f"{'s' if excluded_count != 1 else ''} remain visible "
                        "with an unchecked Included box and Excluded state."
                    )

with analysis_tab:
    avg_win_hold_days = 0.0
    avg_loss_hold_days = 0.0
    if not closed_trades.empty:
        win_hold = closed_trades.loc[closed_trades["pnl"] > 0, "holding_days"]
        loss_hold = closed_trades.loc[closed_trades["pnl"] < 0, "holding_days"]
        if not win_hold.empty:
            avg_win_hold_days = win_hold.mean()
        if not loss_hold.empty:
            avg_loss_hold_days = loss_hold.mean()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Balance", format_money_rounded(total_portfolio or 0.0))
    col2.metric("Unrealized PnL", format_money_rounded(total_unrealized))
    col3.metric("Equity Change %", format_percent(equity_change_pct))
    col4.metric("Win Rate", format_percent(win_rate))

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Avg Win % (Position)", format_percent(avg_win_pct))
    col6.metric("Avg Loss % (Position)", format_percent(avg_loss_pct))
    col7.metric("Expectancy %", format_percent(expectancy_pct))
    col8.metric("Profit Factor", f"{profit_factor:.2f}")

    col9, col10, col11, col12 = st.columns(4)
    col9.metric("Total Trades", f"{total_trades}")
    col10.metric("Trades Per Month Avg", f"{avg_trades_per_month:.1f}")
    col11.metric("Avg Win Holding Days", f"{avg_win_hold_days:.1f}")
    col12.metric("Avg Loss Holding Days", f"{avg_loss_hold_days:.1f}")

    comm_col1, comm_col2 = st.columns(2)
    comm_col1.metric("Total Commissions",
                     format_money_rounded(total_commission))
    comm_col2.metric("Avg Commission Per Trade",
                     format_money_rounded(avg_commission_per_trade))

st.markdown("")
