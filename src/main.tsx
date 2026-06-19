import React from "react";
import { createRoot } from "react-dom/client";
import { RefreshCw } from "lucide-react";
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
  symbol: string;
  direction: string;
  quantity: number;
  avg_entry: number;
  last_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
  portfolio_pct: number;
  holding_days: number | null;
};

type ClosedTrade = {
  symbol: string;
  direction: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  pnl_portfolio_pct: number;
  exit_date: string | null;
};

type DashboardData = {
  total_portfolio: number | null;
  stats: Stats;
  open_positions: OpenPosition[];
  closed_trades: ClosedTrade[];
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

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Trades Dashboard</h1>
          <p>React frontend backed by the IBKR Flex parser.</p>
        </div>
        <button className="icon-button" onClick={() => loadDashboard(true)} disabled={loading} title="Refresh market data">
          <RefreshCw size={18} />
          <span>{loading ? "Refreshing" : "Refresh"}</span>
        </button>
      </header>

      {error ? <div className="notice">{error}</div> : null}

      <section className="metrics-grid">
        <Metric label="Balance" value={money(data?.total_portfolio)} />
        <Metric label="Open P&L" value={money(data?.stats.total_unrealized)} />
        <Metric label="Closed P&L" value={money(data?.stats.total_pnl)} />
        <Metric label="Win Rate" value={percent(data?.stats.win_rate)} />
        <Metric label="Total Trades" value={String(data?.stats.total_trades ?? "-")} />
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Open Positions</h2>
          <span>{data?.open_positions.length ?? 0} positions</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Avg Entry</th>
                <th>Last</th>
                <th>P&L</th>
                <th>Return</th>
                <th>Portfolio</th>
                <th>Days</th>
              </tr>
            </thead>
            <tbody>
              {(data?.open_positions ?? []).map((row) => (
                <tr key={`${row.symbol}-${row.direction}`}>
                  <td>{row.symbol}</td>
                  <td>{row.direction}</td>
                  <td>{numeric(row.quantity, 0)}</td>
                  <td>{numeric(row.avg_entry)}</td>
                  <td>{numeric(row.last_price)}</td>
                  <td className={row.unrealized_pnl >= 0 ? "positive" : "negative"}>{money(row.unrealized_pnl)}</td>
                  <td className={row.pnl_pct >= 0 ? "positive" : "negative"}>{percent(row.pnl_pct)}</td>
                  <td>{percent(row.portfolio_pct)}</td>
                  <td>{row.holding_days ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2>Recent Closed Trades</h2>
          <span>{data?.closed_trades.length ?? 0} trades</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P&L</th>
                <th>Return</th>
                <th>Portfolio</th>
                <th>Exit Date</th>
              </tr>
            </thead>
            <tbody>
              {(data?.closed_trades ?? []).slice(-50).reverse().map((row, index) => (
                <tr key={`${row.symbol}-${row.exit_date}-${index}`}>
                  <td>{row.symbol}</td>
                  <td>{row.direction}</td>
                  <td>{numeric(row.quantity, 0)}</td>
                  <td>{numeric(row.entry_price)}</td>
                  <td>{numeric(row.exit_price)}</td>
                  <td className={row.pnl >= 0 ? "positive" : "negative"}>{money(row.pnl)}</td>
                  <td className={row.pnl_pct >= 0 ? "positive" : "negative"}>{percent(row.pnl_pct)}</td>
                  <td>{percent(row.pnl_portfolio_pct)}</td>
                  <td>{row.exit_date ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
