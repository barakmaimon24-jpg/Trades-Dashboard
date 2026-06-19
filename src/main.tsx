import React from "react";
import { createRoot } from "react-dom/client";
import { BarChart3, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import "./styles.css";

type Stats = {
  total_trades?: number;
  total_pnl?: number;
  total_unrealized?: number;
  wins?: number;
  losses?: number;
  flats?: number;
  win_rate?: number;
  total_commission?: number;
  avg_commission_per_trade?: number;
};

type OpenPosition = {
  entry_date: string | null;
  entry_time: string | null;
  symbol: string;
  direction: string;
  quantity: number;
  avg_entry: number;
  last_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
  portfolio_pct: number;
  pnl_portfolio_pct: number;
  holding_days: number | null;
};

type ClosedTrade = {
  entry_date: string | null;
  entry_time: string | null;
  entry_time_str: string | null;
  exit_date: string | null;
  exit_time: string | null;
  exit_time_str: string | null;
  symbol: string;
  direction: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  pnl_portfolio_pct: number;
};

type RawTrade = {
  symbol: string;
  trade_date: string | null;
  trade_datetime: string | null;
  buy_sell: string;
  trade_price: number;
};

type DashboardData = {
  total_portfolio: number | null;
  stats: Stats;
  open_positions: OpenPosition[];
  closed_trades: ClosedTrade[];
  raw_trades?: RawTrade[];
};

const money = (value?: number | null) =>
  value == null
    ? "-"
    : value.toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 0
      });

const numeric = (value?: number | null, digits = 1) =>
  value == null ? "-" : value.toLocaleString("en-US", { maximumFractionDigits: digits });

const percent = (value?: number | null) => (value == null ? "-" : `${numeric(value)}%`);
const daysValue = (value?: number | null) => (value == null ? "-" : `${numeric(value, 1)}D`);
const pageSize = 7;
const stopStorageKey = "trades-dashboard-stop-prices";

type TabKey = "open" | "history" | "analysis";
type AnalysisFrame = "ytd" | "all" | "quarter" | "month";
type PositionSortKey = "symbol" | "entry" | "entryPrice" | "stopPrice" | "stopPct" | "return" | "r" | "heat" | "size" | "holding";
type SortDirection = "asc" | "desc";

const analysisFrames: Array<{ key: AnalysisFrame; label: string }> = [
  { key: "ytd", label: "YTD" },
  { key: "all", label: "All Time" },
  { key: "quarter", label: "Current Quarter" },
  { key: "month", label: "Current Month" }
];

const positionColumns: Array<{ key: PositionSortKey; label: string }> = [
  { key: "symbol", label: "Symbol" },
  { key: "entry", label: "Entry" },
  { key: "entryPrice", label: "Entry Price" },
  { key: "stopPrice", label: "Stop Price" },
  { key: "stopPct", label: "Stop %" },
  { key: "return", label: "Return" },
  { key: "r", label: "R" },
  { key: "heat", label: "Heat" },
  { key: "size", label: "Size" },
  { key: "holding", label: "Holding" }
];

const formatEntry = (value?: string | null, fallbackTime?: string | null) => {
  if (!value) {
    return { date: "-", time: fallbackTime ?? "" };
  }
  const [datePart, timePart] = value.includes("T") ? value.split("T") : value.split(" ");
  const parts = datePart.split("-");
  if (parts.length !== 3) {
    return { date: value, time: fallbackTime ?? "" };
  }
  const [year, month, day] = parts;
  const time = timePart ? timePart.slice(0, 5) : fallbackTime ?? "";
  return { date: `${day}/${month}/${year}`, time };
};

const timestampValue = (date?: string | null, time?: string | null) => {
  if (!date) {
    return null;
  }
  return time ? `${date}T${time}` : date;
};

const dateKey = (value?: string | null) => {
  if (!value) {
    return "";
  }
  const [datePart] = value.includes("T") ? value.split("T") : value.split(" ");
  return datePart;
};

const inputDateValue = (date: Date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};

const defaultHistoryRange = () => {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - 14);
  return {
    start: inputDateValue(start),
    end: inputDateValue(end)
  };
};

const analysisRange = (frame: AnalysisFrame) => {
  if (frame === "all") {
    return { start: "", end: "" };
  }

  const end = new Date();
  const start = new Date(end);

  if (frame === "ytd") {
    start.setMonth(0, 1);
  }

  if (frame === "quarter") {
    const quarterStartMonth = Math.floor(start.getMonth() / 3) * 3;
    start.setMonth(quarterStartMonth, 1);
  }

  if (frame === "month") {
    start.setDate(1);
  }

  start.setHours(0, 0, 0, 0);
  return {
    start: inputDateValue(start),
    end: inputDateValue(end)
  };
};

const displayDateValue = (value: string) => {
  const [year, month, day] = value.split("-");
  return year && month && day ? `${day}/${month}/${year}` : value;
};

const parseDisplayDate = (value: string) => {
  const match = value.trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!match) {
    return null;
  }
  const [, dayText, monthText, yearText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return null;
  }
  return inputDateValue(date);
};

const inferEntryTimestamp = (row: ClosedTrade, rawTrades: RawTrade[] = []) => {
  const targetDate = dateKey(row.entry_date);
  const entrySide = row.direction === "SHORT" ? "SELL" : "BUY";
  const candidates = rawTrades
    .filter((trade) => trade.symbol === row.symbol)
    .filter((trade) => trade.buy_sell === entrySide)
    .filter((trade) => dateKey(trade.trade_datetime ?? trade.trade_date) === targetDate)
    .sort((a, b) => (timestampMs(a.trade_datetime ?? a.trade_date) ?? 0) - (timestampMs(b.trade_datetime ?? b.trade_date) ?? 0));

  if (!candidates.length) {
    return null;
  }

  const priceMatch = candidates.find((trade) => Math.abs((trade.trade_price ?? 0) - row.entry_price) < 0.02);
  return (priceMatch ?? candidates[0]).trade_datetime ?? (priceMatch ?? candidates[0]).trade_date;
};

const timestampMs = (value?: string | null) => {
  if (!value) {
    return null;
  }
  const time = new Date(value).getTime();
  return Number.isFinite(time) ? time : null;
};

const holdingDuration = (entry?: string | null, exit?: string | null) => {
  const entryMs = timestampMs(entry);
  const exitMs = timestampMs(exit);
  if (entryMs == null || exitMs == null || exitMs < entryMs) {
    return { label: "-", days: null };
  }
  const minutes = Math.max(1, Math.round((exitMs - entryMs) / 60000));
  const days = minutes / 1440;
  if (minutes < 60) {
    return { label: `${minutes}m`, days };
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return { label: `${hours}h`, days };
  }
  return { label: `${Math.round(hours / 24)}D`, days };
};

const stopPercent = (row: OpenPosition, stopPrice: number | null) => {
  if (stopPrice == null || !row.avg_entry) {
    return null;
  }
  if (row.direction === "SHORT") {
    return ((row.avg_entry - stopPrice) / row.avg_entry) * 100;
  }
  return ((stopPrice - row.avg_entry) / row.avg_entry) * 100;
};

const riskReward = (row: OpenPosition, stopPrice: number | null) => {
  const riskPct = stopPercent(row, stopPrice);
  if (riskPct == null || riskPct === 0) {
    return null;
  }
  return row.pnl_pct / Math.abs(riskPct);
};

const gainClassName = (value: number | null | undefined) => {
  if (value == null) {
    return "";
  }
  if (value < 0) {
    return "gain-negative";
  }
  if (value >= 60) {
    return "gain-hot";
  }
  if (value >= 40) {
    return "gain-strong";
  }
  if (value >= 20) {
    return "gain-medium";
  }
  return "gain-low";
};

const gainBadgeClassName = (value: number | null | undefined) => {
  const tier = gainClassName(value);
  return tier ? `gain-label ${tier}` : "gain-label";
};

const stopClassName = (value: number | null) => {
  if (value == null) {
    return "stop-label";
  }
  if (value >= 0) {
    return "stop-label stop-positive";
  }
  return "stop-label stop-negative";
};

const rClassName = (value: number | null) => {
  if (value == null) {
    return "r-label";
  }
  if (value < 0) {
    return "r-label r-negative";
  }
  if (value >= 30) {
    return "r-label r-strong";
  }
  if (value >= 12) {
    return "r-label r-great";
  }
  if (value >= 3) {
    return "r-label r-good";
  }
  return "r-label r-neutral";
};

const holdingClassName = (value: number | null) => {
  if (value == null) {
    return "holding-label";
  }
  if (value >= 16) {
    return "holding-label holding-extended";
  }
  if (value > 8) {
    return "holding-label holding-long";
  }
  if (value >= 4) {
    return "holding-label holding-medium";
  }
  return "holding-label holding-short";
};

const sizeClassName = (value: number | null | undefined) => {
  if (value == null) {
    return "holding-label";
  }
  if (value >= 18) {
    return "holding-label holding-extended";
  }
  if (value >= 12) {
    return "holding-label holding-long";
  }
  if (value >= 9) {
    return "holding-label holding-medium";
  }
  return "holding-label holding-short";
};

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <section className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </section>
  );
}

function App() {
  const [data, setData] = React.useState<DashboardData | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [activeTab, setActiveTab] = React.useState<TabKey>("open");
  const [historyPage, setHistoryPage] = React.useState(1);
  const defaultRange = React.useMemo(defaultHistoryRange, []);
  const [historyRange, setHistoryRange] = React.useState(defaultRange);
  const [historyRangeText, setHistoryRangeText] = React.useState(() => ({
    start: displayDateValue(defaultRange.start),
    end: displayDateValue(defaultRange.end)
  }));
  const [analysisFrame, setAnalysisFrame] = React.useState<AnalysisFrame>("ytd");
  const [positionSort, setPositionSort] = React.useState<{ key: PositionSortKey; direction: SortDirection }>({
    key: "size",
    direction: "desc"
  });
  const [stopPrices, setStopPrices] = React.useState<Record<string, string>>(() => {
    try {
      const stored = window.localStorage.getItem(stopStorageKey);
      return stored ? JSON.parse(stored) : {};
    } catch {
      return {};
    }
  });

  const fetchJson = async (url: string) => {
    const response = await fetch(url);
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new Error(payload?.detail?.message || payload?.message || text || `Request failed with ${response.status}`);
    }
    return payload;
  };

  const loadDashboard = React.useCallback(async (refreshMarket = false) => {
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson(`/api/dashboard${refreshMarket ? "?refresh_market=true" : ""}`);
      setData(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    loadDashboard();
  }, [loadDashboard]);

  const stopPriceForSymbol = React.useCallback((symbol: string) => {
    const stopText = stopPrices[symbol] ?? "";
    const stopValue = stopText.trim() === "" ? null : Number(stopText);
    return stopValue != null && Number.isFinite(stopValue) ? stopValue : null;
  }, [stopPrices]);

  const sortedOpenPositions = React.useMemo(() => {
    const valueForSort = (row: OpenPosition) => {
      const stopPrice = stopPriceForSymbol(row.symbol);
      if (positionSort.key === "symbol") {
        return row.symbol;
      }
      if (positionSort.key === "entry") {
        return row.entry_time ?? row.entry_date ?? null;
      }
      if (positionSort.key === "entryPrice") {
        return row.avg_entry;
      }
      if (positionSort.key === "stopPrice") {
        return stopPrice;
      }
      if (positionSort.key === "stopPct") {
        return stopPercent(row, stopPrice);
      }
      if (positionSort.key === "return") {
        return row.pnl_pct;
      }
      if (positionSort.key === "r") {
        return riskReward(row, stopPrice);
      }
      if (positionSort.key === "heat") {
        return row.pnl_portfolio_pct;
      }
      if (positionSort.key === "holding") {
        return row.holding_days;
      }
      return row.portfolio_pct;
    };

    return [...(data?.open_positions ?? [])].sort((a, b) => {
      const aValue = valueForSort(a);
      const bValue = valueForSort(b);
      if (aValue == null && bValue == null) {
        return a.symbol.localeCompare(b.symbol);
      }
      if (aValue == null) {
        return 1;
      }
      if (bValue == null) {
        return -1;
      }

      const baseComparison =
        typeof aValue === "string" || typeof bValue === "string"
          ? String(aValue).localeCompare(String(bValue))
          : aValue - bValue;

      if (baseComparison === 0) {
        return a.symbol.localeCompare(b.symbol);
      }
      return positionSort.direction === "asc" ? baseComparison : -baseComparison;
    });
  }, [data?.open_positions, positionSort, stopPriceForSymbol]);

  const closedTrades = React.useMemo(() => {
    return [...(data?.closed_trades ?? [])].sort((a, b) => {
      const aDate = timestampMs(a.exit_time ?? a.exit_date) ?? 0;
      const bDate = timestampMs(b.exit_time ?? b.exit_date) ?? 0;
      return bDate - aDate;
    });
  }, [data?.closed_trades]);

  const filteredClosedTrades = React.useMemo(() => {
    return closedTrades.filter((trade) => {
      const exitDate = dateKey(trade.exit_time ?? trade.exit_date);
      if (!exitDate) {
        return false;
      }
      return exitDate >= historyRange.start && exitDate <= historyRange.end;
    });
  }, [closedTrades, historyRange.end, historyRange.start]);

  const analysisStats = React.useMemo(() => {
    const range = analysisRange(analysisFrame);
    const trades = closedTrades.filter((trade) => {
      const exitDate = dateKey(trade.exit_time ?? trade.exit_date);
      if (!exitDate) {
        return false;
      }
      if (range.start && exitDate < range.start) {
        return false;
      }
      if (range.end && exitDate > range.end) {
        return false;
      }
      return true;
    });

    const average = (values: number[]) => {
      if (!values.length) {
        return null;
      }
      return values.reduce((sum, value) => sum + value, 0) / values.length;
    };

    const tradeHoldingDays = (trade: ClosedTrade) => {
      const entryTimestamp =
        trade.entry_time ??
        (trade.entry_time_str ? timestampValue(trade.entry_date, trade.entry_time_str) : null) ??
        inferEntryTimestamp(trade, data?.raw_trades) ??
        trade.entry_date;
      const exitTimestamp = trade.exit_time ?? timestampValue(trade.exit_date, trade.exit_time_str);
      const entryMs = timestampMs(entryTimestamp);
      const exitMs = timestampMs(exitTimestamp);
      if (entryMs == null || exitMs == null || exitMs < entryMs) {
        return null;
      }
      return (exitMs - entryMs) / 86400000;
    };

    const winners = trades.filter((trade) => trade.pnl > 0);
    const losers = trades.filter((trade) => trade.pnl < 0);
    const wins = winners.length;
    const winRate = trades.length ? (wins / trades.length) * 100 : null;
    const perf = trades.reduce((sum, trade) => sum + (trade.pnl_portfolio_pct ?? 0), 0);
    return {
      totalTrades: trades.length,
      perf,
      winRate,
      wins,
      losses: losers.length,
      avgWin: average(winners.map((trade) => trade.pnl_pct).filter((value) => value != null)),
      avgLoss: average(losers.map((trade) => trade.pnl_pct).filter((value) => value != null)),
      avgWinnerHolding: average(winners.map(tradeHoldingDays).filter((value) => value != null)),
      avgLoserHolding: average(losers.map(tradeHoldingDays).filter((value) => value != null)),
      range
    };
  }, [analysisFrame, closedTrades, data?.raw_trades]);

  const pageCount = Math.max(1, Math.ceil(filteredClosedTrades.length / pageSize));
  const currentPage = Math.min(historyPage, pageCount);
  const pagedClosedTrades = filteredClosedTrades.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  React.useEffect(() => {
    setHistoryPage(1);
  }, [data?.closed_trades, historyRange.end, historyRange.start]);

  React.useEffect(() => {
    window.localStorage.setItem(stopStorageKey, JSON.stringify(stopPrices));
  }, [stopPrices]);

  const setPositionSortKey = (key: PositionSortKey) => {
    setPositionSort((current) => ({
      key,
      direction: current.key === key && current.direction === "desc" ? "asc" : "desc"
    }));
  };

  const setHistoryDateText = (field: "start" | "end", value: string) => {
    setHistoryRangeText((current) => ({ ...current, [field]: value }));
    const parsed = parseDisplayDate(value);
    if (parsed) {
      setHistoryRange((current) => ({ ...current, [field]: parsed }));
    }
  };

  const resetHistoryDateText = (field: "start" | "end") => {
    setHistoryRangeText((current) => ({ ...current, [field]: displayDateValue(historyRange[field]) }));
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-icon" aria-hidden="true">
            <BarChart3 size={20} />
          </div>
          <div>
            <h1>Trades Dashboard</h1>
          </div>
        </div>
        <div className="topbar-actions">
          <button className="icon-button refresh-button" onClick={() => loadDashboard(true)} disabled={loading} title="Refresh market data">
            <RefreshCw size={17} />
          </button>
          <div className="status-pill">
            <span className="status-dot" />
            <span>{loading ? "syncing" : "IBKR local"}</span>
          </div>
        </div>
      </header>

      {error ? <div className="notice">{error}</div> : null}

      <nav className="tabs" aria-label="Dashboard sections">
        <button className={activeTab === "open" ? "active" : ""} onClick={() => setActiveTab("open")}>Open Positions</button>
        <button className={activeTab === "history" ? "active" : ""} onClick={() => setActiveTab("history")}>Trade History</button>
        <button className={activeTab === "analysis" ? "active" : ""} onClick={() => setActiveTab("analysis")}>Analysis</button>
      </nav>

      {activeTab === "open" ? (
        <section className="panel">
          <div className="panel-header">
            <h2>Open Positions</h2>
            <span>{data?.open_positions.length ?? 0} positions</span>
          </div>
          <div className="table-wrap">
            <table className="positions-table">
              <colgroup>
                <col className="col-symbol" />
                <col className="col-entry" />
                <col className="col-entry-price" />
                <col className="col-stop-price" />
                <col className="col-stop-pct" />
                <col className="col-return" />
                <col className="col-r" />
                <col className="col-heat" />
                <col className="col-size" />
                <col className="col-holding" />
              </colgroup>
              <thead>
                <tr>
                  {positionColumns.map((column) => (
                    <th key={column.key}>
                      <button
                        className="sort-header"
                        type="button"
                        onClick={() => setPositionSortKey(column.key)}
                        aria-sort={positionSort.key === column.key ? (positionSort.direction === "asc" ? "ascending" : "descending") : "none"}
                      >
                        <span>{column.label}</span>
                        <svg
                          className={`sort-indicator ${positionSort.key === column.key ? "sort-indicator-active" : ""} ${positionSort.direction === "asc" ? "sort-indicator-up" : ""}`}
                          viewBox="0 0 10 10"
                          aria-hidden="true"
                        >
                          <path d="M5 7.5 1.5 3.5h7L5 7.5Z" />
                        </svg>
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortedOpenPositions.map((row) => {
                  const stopText = stopPrices[row.symbol] ?? "";
                  const validStop = stopPriceForSymbol(row.symbol);
                  const riskPct = stopPercent(row, validStop);
                  const rr = riskReward(row, validStop);
                  const entry = formatEntry(row.entry_time ?? row.entry_date);
                  return (
                    <tr key={`${row.symbol}-${row.direction}`}>
                      <td>{row.symbol}</td>
                      <td>
                        <span className="entry-cell">
                          <span>{entry.date}</span>
                          {entry.time ? <span className="entry-time">({entry.time})</span> : null}
                        </span>
                      </td>
                      <td>{numeric(row.avg_entry)}</td>
                      <td>
                        <div className="stop-cell">
                          <input
                            value={stopText}
                            inputMode="decimal"
                            placeholder="-"
                            onChange={(event) =>
                              setStopPrices((current) => ({
                                ...current,
                                [row.symbol]: event.target.value
                              }))
                            }
                          />
                        </div>
                      </td>
                      <td><span className={stopClassName(riskPct)}>{riskPct == null ? "-" : percent(riskPct)}</span></td>
                      <td><span className={gainBadgeClassName(row.pnl_pct)}>{percent(row.pnl_pct)}</span></td>
                      <td><span className={rClassName(rr)}>{rr == null ? "-" : `${numeric(rr, 1)}R`}</span></td>
                      <td><span className={gainBadgeClassName(row.pnl_portfolio_pct)}>{row.pnl_portfolio_pct == null ? "-" : `${numeric(row.pnl_portfolio_pct, 2)}%`}</span></td>
                      <td><span className={sizeClassName(row.portfolio_pct)}>{percent(row.portfolio_pct)}</span></td>
                      <td>
                        <span className={holdingClassName(row.holding_days)}>
                          {row.holding_days == null ? "-" : `${row.holding_days}D`}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {activeTab === "history" ? (
        <section className="panel">
          <div className="panel-header">
            <h2>Trade History</h2>
            <div className="history-controls">
              <label>
                <span>From</span>
                <input
                  type="text"
                  inputMode="numeric"
                  placeholder="dd/mm/yyyy"
                  value={historyRangeText.start}
                  onChange={(event) => setHistoryDateText("start", event.target.value)}
                  onBlur={() => resetHistoryDateText("start")}
                />
              </label>
              <label>
                <span>To</span>
                <input
                  type="text"
                  inputMode="numeric"
                  placeholder="dd/mm/yyyy"
                  value={historyRangeText.end}
                  onChange={(event) => setHistoryDateText("end", event.target.value)}
                  onBlur={() => resetHistoryDateText("end")}
                />
              </label>
              <span className="history-count">{filteredClosedTrades.length} trades</span>
            </div>
          </div>
          <div className="table-wrap">
            <table className="history-table">
              <colgroup>
                <col className="history-col-symbol" />
                <col className="history-col-entry" />
                <col className="history-col-exit" />
                <col className="history-col-entry-price" />
                <col className="history-col-exit-price" />
                <col className="history-col-return" />
                <col className="history-col-heat" />
                <col className="history-col-holding" />
              </colgroup>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Entry Price</th>
                  <th>Exit Price</th>
                  <th>Return</th>
                  <th>Heat</th>
                  <th>Holding</th>
                </tr>
              </thead>
              <tbody>
                {pagedClosedTrades.map((row, index) => {
                  const entryTimestamp =
                    row.entry_time ??
                    (row.entry_time_str ? timestampValue(row.entry_date, row.entry_time_str) : null) ??
                    inferEntryTimestamp(row, data?.raw_trades) ??
                    row.entry_date;
                  const exitTimestamp = row.exit_time ?? timestampValue(row.exit_date, row.exit_time_str);
                  const entry = formatEntry(entryTimestamp, row.entry_time_str);
                  const exit = formatEntry(exitTimestamp, row.exit_time_str);
                  const holding = holdingDuration(entryTimestamp, exitTimestamp);
                  return (
                    <tr key={`${row.symbol}-${row.exit_time ?? row.exit_date}-${currentPage}-${index}`}>
                      <td>{row.symbol}</td>
                      <td>
                        <span className="entry-cell">
                          <span>{entry.date}</span>
                          {entry.time ? <span className="entry-time">({entry.time})</span> : null}
                        </span>
                      </td>
                      <td>
                        <span className="entry-cell">
                          <span>{exit.date}</span>
                          {exit.time ? <span className="entry-time">({exit.time})</span> : null}
                        </span>
                      </td>
                      <td>{numeric(row.entry_price)}</td>
                      <td>{numeric(row.exit_price)}</td>
                      <td><span className={gainBadgeClassName(row.pnl_pct)}>{percent(row.pnl_pct)}</span></td>
                      <td><span className={gainBadgeClassName(row.pnl_portfolio_pct)}>{row.pnl_portfolio_pct == null ? "-" : `${numeric(row.pnl_portfolio_pct, 2)}%`}</span></td>
                      <td><span className={holdingClassName(holding.days)}>{holding.label}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <footer className="pagination">
            <button className="icon-button" onClick={() => setHistoryPage((page) => Math.max(1, page - 1))} disabled={currentPage === 1}>
              <ChevronLeft size={16} />
              <span>Previous</span>
            </button>
            <span>Page {currentPage} of {pageCount}</span>
            <button className="icon-button" onClick={() => setHistoryPage((page) => Math.min(pageCount, page + 1))} disabled={currentPage === pageCount}>
              <span>Next</span>
              <ChevronRight size={16} />
            </button>
          </footer>
        </section>
      ) : null}

      {activeTab === "analysis" ? (
        <section className="analysis-view">
          <div className="analysis-toolbar">
            <div>
              <h2>Analysis</h2>
              <span>
                {analysisFrame === "all"
                  ? "All closed trades"
                  : `${displayDateValue(analysisStats.range.start)} - ${displayDateValue(analysisStats.range.end)}`}
              </span>
            </div>
            <div className="segmented-control" role="tablist" aria-label="Analysis timeframe">
              {analysisFrames.map((frame) => (
                <button
                  key={frame.key}
                  className={analysisFrame === frame.key ? "active" : ""}
                  onClick={() => setAnalysisFrame(frame.key)}
                  type="button"
                >
                  {frame.label}
                </button>
              ))}
            </div>
          </div>
          <div className="metrics-grid">
            <Metric label="Perf" value={percent(analysisStats.perf)} />
            <Metric label="Total Trades" value={String(analysisStats.totalTrades)} />
            <Metric label="Win Rate" value={percent(analysisStats.winRate)} />
            <Metric label="Wins" value={String(analysisStats.wins)} />
            <Metric label="Avg Win" value={percent(analysisStats.avgWin)} />
            <Metric label="Avg Winner Holding Time" value={daysValue(analysisStats.avgWinnerHolding)} />
            <Metric label="Loss" value={String(analysisStats.losses)} />
            <Metric label="Avg Loss" value={percent(analysisStats.avgLoss)} />
            <Metric label="Avg Loser Holding Time" value={daysValue(analysisStats.avgLoserHolding)} />
          </div>
        </section>
      ) : null}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
