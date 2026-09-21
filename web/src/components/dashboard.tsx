"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addCopyWallet,
  fetchDashboard,
  removeCopyWallet,
  resetBalances,
  resetStatistics,
  setStrategyBudget,
} from "@/lib/api";
import {
  dt,
  failLabel,
  money,
  ms,
  pct,
  pnlClass,
  price,
  shortAddr,
} from "@/lib/format";
import type {
  ActivityRow,
  DashMode,
  DashboardPayload,
  PositionRow,
  TradeRow,
} from "@/lib/types";

const STRATEGIES = [
  "all",
  "asymmetric",
  "contrarian",
  "conviction",
  "arbitrage",
  "weatherlock",
  "endgame",
  "copy",
  "esports",
  "momentum",
  "volspike",
] as const;

const POLL_MS = 15_000;

function useLocalMode(): [DashMode, (m: DashMode) => void] {
  const [mode, setModeState] = useState<DashMode>("paper");
  useEffect(() => {
    const saved = localStorage.getItem("pmDashMode");
    if (saved === "live" || saved === "paper") setModeState(saved);
  }, []);
  const setMode = (m: DashMode) => {
    setModeState(m);
    localStorage.setItem("pmDashMode", m);
  };
  return [mode, setMode];
}

function EquityChart({
  curve,
  history,
}: {
  curve: Array<{ ts?: string; value: number }>;
  history: Array<{ ts?: string; total: number }>;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const pts =
      history && history.length
        ? history.map((r) => ({ ts: r.ts, value: r.total }))
        : curve;
    if (!pts.length) {
      ctx.fillStyle = "#94a3b8";
      ctx.fillText("Noch keine realisierten Trades", 16, h / 2);
      return;
    }
    const vals = pts.map((p) => Number(p.value));
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const pad = 12;
    const range = max - min || 1;
    ctx.strokeStyle = "#7dd3fc";
    ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = pad + (i / Math.max(pts.length - 1, 1)) * (w - pad * 2);
      const y = h - pad - ((Number(p.value) - min) / range) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [curve, history]);
  return <canvas ref={ref} width={1100} height={180} />;
}

export function Dashboard() {
  const [mode, setMode] = useLocalMode();
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const [activityFilter, setActivityFilter] = useState("all");
  const [budget, setBudget] = useState("");
  const [budgetTouched, setBudgetTouched] = useState(false);
  const [walletInput, setWalletInput] = useState("");
  const [walletLabel, setWalletLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [strategyBudgets, setStrategyBudgets] = useState<Record<string, string>>(
    {},
  );

  const labels = useMemo(
    () => ({
      weatherlock: "Weatherlock",
      endgame: "Endgame",
      ...(data?.strategy_labels || {}),
    }),
    [data?.strategy_labels],
  );
  const strategyLabel = (name: string) =>
    labels[name as keyof typeof labels] || name;

  const load = useCallback(async () => {
    try {
      const payload = await fetchDashboard(mode);
      setData(payload);
      setError(null);
      if (!budgetTouched && payload.starting_balance != null) {
        setBudget(String(Math.round(Number(payload.starting_balance))));
      }
      const next: Record<string, string> = {};
      for (const s of payload.portfolio?.by_strategy || []) {
        next[s.name] = String(Math.round(Number(s.cash ?? s.starting_balance ?? 0)));
      }
      setStrategyBudgets(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [mode, budgetTouched]);

  useEffect(() => {
    setLoading(true);
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const positions = useMemo(() => {
    const rows = data?.positions || [];
    const q = search.trim().toLowerCase();
    return rows.filter((p) => {
      if (filter !== "all" && p.strategy !== filter) return false;
      if (
        q &&
        !`${p.market_question} ${p.outcome}`.toLowerCase().includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [data?.positions, filter, search]);

  const trades = useMemo(() => {
    const rows = data?.trades || [];
    const q = search.trim().toLowerCase();
    return rows.filter((t) => {
      if (filter !== "all" && t.strategy !== filter) return false;
      if (
        q &&
        !`${t.market_question} ${t.outcome || ""}`.toLowerCase().includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [data?.trades, filter, search]);

  const activity = useMemo(() => {
    const rows = data?.activity_log || [];
    return rows.filter((r) => {
      if (activityFilter === "all") return true;
      if (activityFilter === "error")
        return r.level === "error" || r.event === "skipped_trade";
      if (activityFilter === "scan")
        return (
          (r.decision || r.event || "") === "scan" ||
          r.event === "esports_scan" ||
          r.event === "momentum_scan"
        );
      const d = (r.decision || r.event || "").toLowerCase();
      return d === activityFilter;
    });
  }, [data?.activity_log, activityFilter]);

  const showLatency = filter === "copy" || filter === "all";
  const p = data?.portfolio;
  const copy = data?.copy || {};
  const ph = data?.predictionhunt;

  async function onSetBudget() {
    const balance = Number(budget);
    if (!Number.isFinite(balance) || balance <= 0) {
      alert("Bitte ein gültiges Budget eingeben.");
      return;
    }
    let msg = `Alle Strategien auf $${balance} zurücksetzen?\n\nPositionen und Trade-Historie werden gelöscht.`;
    if (mode === "live") {
      msg +=
        "\n\nLive-Modus: betrifft nur lokale Ledger, nicht dein Polymarket-Wallet.";
    }
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await resetBalances(balance, mode);
      setBudgetTouched(false);
      await load();
    } catch (e) {
      alert(`Fehler: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function onResetStats() {
    let msg =
      "Alle Statistiken zurücksetzen und alle Logs/Trades löschen?\n\nDanach startest du im Dashboard von 0.";
    if (mode === "live") {
      msg +=
        "\n\nLive-Modus: betrifft nur lokale Ledger, nicht dein Polymarket-Wallet.";
    }
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await resetStatistics(mode);
      setBudgetTouched(false);
      await load();
    } catch (e) {
      alert(`Fehler: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function onSetStrategyBudget(strategy: string) {
    const balance = Number(strategyBudgets[strategy]);
    if (!Number.isFinite(balance) || balance <= 0) {
      alert("Bitte ein gültiges Budget eingeben.");
      return;
    }
    let msg = `Strategie "${strategy}" auf $${balance} setzen?\n\nNur dieses Strategie-Ledger wird zurückgesetzt.`;
    if (mode === "live") {
      msg +=
        "\n\nLive-Modus: betrifft nur lokale Ledger, nicht dein Polymarket-Wallet.";
    }
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await setStrategyBudget(strategy, balance, mode);
      await load();
    } catch (e) {
      alert(`Fehler: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function onAddWallet() {
    const address = walletInput.trim();
    if (!address) {
      alert("Wallet address required");
      return;
    }
    setBusy(true);
    try {
      await addCopyWallet(address, walletLabel.trim(), mode);
      setWalletInput("");
      setWalletLabel("");
      await load();
    } catch (e) {
      alert(`Fehler: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function onRemoveWallet(address: string) {
    setBusy(true);
    try {
      await removeCopyWallet(address, mode);
      await load();
    } catch (e) {
      alert(`Fehler: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dash">
      <div className="top">
        <div>
          <h1>Papertrader Dashboard</h1>
          <div className="sub">
            Asymmetric · Contrarian · Conviction · Arbitrage · Weatherlock ·
            Endgame · Copy · Esports · Momentum · Volspike
          </div>
        </div>
        <div className="actions">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value === "live" ? "live" : "paper")}
            title="Paper vs Live ledger"
          >
            <option value="paper">Paper</option>
            <option value="live">Live</option>
          </select>
          <input
            placeholder="Markt suchen…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            {STRATEGIES.map((s) => (
              <option key={s} value={s}>
                {s === "all" ? "Alle Strategien" : strategyLabel(s)}
              </option>
            ))}
          </select>
          <label className="muted" style={{ fontSize: 12 }}>
            Budget $
          </label>
          <input
            type="number"
            min={1}
            step={1}
            style={{ width: 88 }}
            value={budget}
            onChange={(e) => {
              setBudget(e.target.value);
              setBudgetTouched(true);
            }}
          />
          <button className="danger" disabled={busy} onClick={() => void onSetBudget()}>
            Budget setzen
          </button>
          <button className="danger" disabled={busy} onClick={() => void onResetStats()}>
            Reset all statistics
          </button>
          <button disabled={busy || loading} onClick={() => void load()}>
            Aktualisieren
          </button>
        </div>
      </div>

      <div className={`mode-badge ${mode}`}>
        <span className="mode-pill">{mode}</span>
        <span>
          Ledger: <code className="mono">{data?.data_dir || "–"}</code>
        </span>
        <span className="muted">
          {data?.updated_at ? dt(data.updated_at) : loading ? "Lade…" : "–"}
        </span>
        {mode === "live" && (
          <span className="bad">
            Live ledger — paper runners write to ~/.pm-trader
          </span>
        )}
      </div>

      <div className="grid">
        <div className="card">
          <div className="label">P&amp;L (realisiert)</div>
          <div className={`value ${pnlClass(p?.pnl)}`}>
            {p ? `${p.pnl >= 0 ? "+" : ""}${money(p.pnl)}` : "–"}
          </div>
          <div className="mini">
            Kontostand {p ? money(p.total) : "–"}
          </div>
        </div>
        <div className="card">
          <div className="label">ROI (realisiert)</div>
          <div className={`value ${pnlClass(p?.roi_pct)}`}>
            {p ? pct(p.roi_pct) : "–"}
          </div>
          <div className="mini">nur geschlossene Trades</div>
        </div>
        <div className="card">
          <div className="label">Cash</div>
          <div className="value">{p ? money(p.cash) : "–"}</div>
          <div className="mini">
            Offene Positionen {p ? money(p.positions) : "–"} (Mark-to-Market)
          </div>
        </div>
        <div className="card">
          <div className="label">Win Rate</div>
          <div className="value">
            {p ? pct(p.win_rate_pct ?? p.win_rate * 100) : "–"}
          </div>
          <div className="mini">
            {p ? `${p.trades} Trades · ${p.sells} Verkäufe` : "Trades –"}
          </div>
        </div>
        <div className="card">
          <div className="label">Max Drawdown</div>
          <div className="value">
            {p ? pct(p.max_drawdown * 100) : "–"}
          </div>
          <div className="mini">Fees {p ? money(p.fees) : "–"}</div>
        </div>
        <div className="card">
          <div className="label">Offene Positionen</div>
          <div className="value">{positions.length}</div>
          <div className="mini">Ø Trade {p ? money(p.avg_trade) : "–"}</div>
        </div>
      </div>

      <div className="section grid grid-4">
        {(p?.by_strategy || []).map((s) => (
          <div className="card" key={s.name}>
            <div className={`label strategy-${s.name}`}>
              {s.display_name || strategyLabel(s.name)}
            </div>
            <div className={`value ${pnlClass(s.pnl)}`}>
              {s.pnl >= 0 ? "+" : ""}
              {money(s.pnl)}
            </div>
            <div className="mini">
              ROI {pct(s.roi_pct)} · Win Rate{" "}
              {pct(s.win_rate_pct ?? s.win_rate * 100)}
            </div>
            <div className="mini">
              {s.trades} Trades ({s.buys} buys / {s.sells} sells)
            </div>
            <div className="mini">
              Cash {money(s.cash)} · Offen {money(s.positions_value)}
            </div>
            <div className="budget-row">
              <input
                type="number"
                min={1}
                step={1}
                style={{ width: 96 }}
                value={strategyBudgets[s.name] ?? ""}
                onChange={(e) =>
                  setStrategyBudgets((prev) => ({
                    ...prev,
                    [s.name]: e.target.value,
                  }))
                }
              />
              <button
                disabled={busy}
                onClick={() => void onSetStrategyBudget(s.name)}
              >
                Setzen
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="section card">
        <h2>Copy Trader</h2>
        <div className="pill-row">
          <span className="pill">
            {copy.active ? "engine aktiv" : "engine idle"}
          </span>
          <span className="pill">@{copy.username || "–"}</span>
          <span className="pill">
            {copy.seen_trades || 0} gesehene Leader-Trades
          </span>
          <span className="pill">
            Scale{" "}
            {copy.scale != null ? Number(copy.scale).toFixed(4) : "–"}
          </span>
          {copy.latency?.count ? (
            <span className="pill">
              Ø Latenz {ms(copy.latency.avg_ms)} (p50 {ms(copy.latency.p50_ms)},
              p95 {ms(copy.latency.p95_ms)})
            </span>
          ) : null}
        </div>
        <div style={{ marginTop: 12 }}>
          <div className="mini" style={{ marginBottom: 6 }}>
            Leader-Wallets (Settings + Dashboard)
          </div>
          <div className="pill-row">
            {(copy.wallets || []).length === 0 ? (
              <span className="pill muted">keine Wallets</span>
            ) : (
              (copy.wallets || []).map((w) => (
                <span className="pill" key={w.address} title={w.address}>
                  {shortAddr(w.address)}
                  {w.label ? ` ${w.label}` : ""}
                  <span className="muted">
                    {" "}
                    · {w.source === "settings" ? "settings" : "dashboard"}
                  </span>
                  {w.source === "dashboard" ? (
                    <button
                      type="button"
                      style={{ marginLeft: 6 }}
                      disabled={busy}
                      onClick={() => void onRemoveWallet(w.address)}
                    >
                      ×
                    </button>
                  ) : null}
                </span>
              ))
            )}
          </div>
          <div
            style={{
              display: "flex",
              gap: 8,
              flexWrap: "wrap",
              marginTop: 10,
              alignItems: "center",
            }}
          >
            <input
              style={{ flex: 1, minWidth: 220 }}
              placeholder="0x… wallet address"
              value={walletInput}
              onChange={(e) => setWalletInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void onAddWallet();
              }}
            />
            <input
              style={{ width: 140 }}
              placeholder="label (optional)"
              value={walletLabel}
              onChange={(e) => setWalletLabel(e.target.value)}
            />
            <button disabled={busy} onClick={() => void onAddWallet()}>
              Add wallet
            </button>
          </div>
          <div className="mini" style={{ marginTop: 6 }}>
            Runs as a separate process:{" "}
            <code>papertrader run --strategy copy</code>. New wallets are picked
            up on the next poll.
          </div>
        </div>
      </div>

      {ph && (ph.enabled || (ph.signals || []).length > 0) ? (
        <div className="section card">
          <h2>Prediction Hunt · Edges &amp; Arb</h2>
          <div className="pill-row">
            <span className="pill">
              {ph.api_key_configured ? "API key OK" : "API key missing"}
            </span>
            <span className="pill">
              {ph.shadow_only ? "shadow edges" : "live edges"}
            </span>
            <span className="pill">
              arb scan {ph.scan_arb ? "on" : "off"}
            </span>
            <span className="pill">{(ph.signals || []).length} signals</span>
          </div>
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table>
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Typ</th>
                  <th>Titel / Markt</th>
                  <th className="right">ROI / Edge</th>
                  <th className="right">Cost</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {(ph.signals || []).slice(0, 40).map((row, i) => {
                  let metric = "–";
                  if (row.roi_pct != null) metric = `${Number(row.roi_pct).toFixed(2)}%`;
                  else if (row.ph_edge_no != null)
                    metric = Number(row.ph_edge_no).toFixed(3);
                  else if (row.dislocation != null)
                    metric = `Δ ${Number(row.dislocation).toFixed(3)}`;
                  const cost =
                    row.total_cost != null
                      ? Number(row.total_cost).toFixed(3)
                      : "–";
                  const detail = row.is_polymarket_pair
                    ? "PM pair"
                    : row.legs
                      ? row.legs
                          .map((l) => `${l.platform}:${l.side}`)
                          .join(" / ")
                      : row.source || "";
                  return (
                    <tr key={`${row.ts}-${i}`}>
                      <td className="mono">{dt(row.ts)}</td>
                      <td>{row.event || "–"}</td>
                      <td>
                        {row.group_title ||
                          row.slug ||
                          row.bucket ||
                          row.reason ||
                          "–"}
                      </td>
                      <td className="right">{metric}</td>
                      <td className="right">{cost}</td>
                      <td className="muted">{detail}</td>
                    </tr>
                  );
                })}
                {(ph.signals || []).length === 0 ? (
                  <tr>
                    <td colSpan={6} className="muted">
                      Noch keine PH-Signale.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      <div className="section card">
        <h2>Equity</h2>
        <EquityChart
          curve={data?.equity_curve || []}
          history={data?.scan_history || []}
        />
      </div>

      <div className="section">
        <h2>Offene Positionen</h2>
        {positions.length === 0 ? (
          <div className="status">Keine offenen Positionen</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Strategie</th>
                  <th>Markt</th>
                  <th>Outcome</th>
                  <th>Shares</th>
                  <th className="right">Entry</th>
                  <th className="right">Live</th>
                  <th className="right">uPnL</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos: PositionRow, i) => (
                  <tr key={`${pos.strategy}-${pos.market_question}-${i}`}>
                    <td>
                      <span className={`badge strategy-${pos.strategy}`}>
                        {strategyLabel(pos.strategy)}
                      </span>
                    </td>
                    <td>
                      <div>{pos.market_question}</div>
                      {pos.url ? (
                        <div className="muted">
                          <a href={pos.url} target="_blank" rel="noreferrer">
                            Polymarket ↗
                          </a>
                        </div>
                      ) : null}
                    </td>
                    <td>{pos.outcome}</td>
                    <td>{Number(pos.shares).toFixed(2)}</td>
                    <td className="right">{price(pos.avg_entry_price)}</td>
                    <td className="right">{price(pos.live_price)}</td>
                    <td className={`right ${pnlClass(pos.unrealized_pnl)}`}>
                      {money(pos.unrealized_pnl)} ({pct(pos.percent_pnl)})
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <h2>Trades</h2>
        {error ? (
          <div className="status error">Fehler: {error}</div>
        ) : trades.length === 0 ? (
          <div className="status">
            {loading ? "Lade…" : "Keine Trades"}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Strategie</th>
                  <th>Markt</th>
                  <th>Side</th>
                  <th className="right">USD</th>
                  <th className="right">Preis</th>
                  <th className="right">PnL</th>
                  <th className="right">Win</th>
                  {showLatency ? <th className="right">Latenz</th> : null}
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 200).map((t: TradeRow, i) => (
                  <tr key={`${t.created_at}-${t.strategy}-${i}`}>
                    <td className="muted">{dt(t.created_at)}</td>
                    <td>
                      <span className={`badge strategy-${t.strategy}`}>
                        {strategyLabel(t.strategy)}
                      </span>
                    </td>
                    <td>
                      <div>{t.market_question}</div>
                      {t.url ? (
                        <div className="muted">
                          <a href={t.url} target="_blank" rel="noreferrer">
                            Polymarket ↗
                          </a>
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <span className={`badge ${t.side}`}>{t.side}</span>
                    </td>
                    <td className="right">{money(t.amount_usd)}</td>
                    <td className="right">{price(t.avg_price)}</td>
                    {t.side === "sell" && t.realized_pnl != null ? (
                      <td className={`right ${pnlClass(t.realized_pnl)}`}>
                        {t.realized_pnl >= 0 ? "+" : ""}
                        {money(t.realized_pnl)}
                      </td>
                    ) : (
                      <td className="right muted">–</td>
                    )}
                    {t.side === "sell" && t.realized_pnl != null ? (
                      <td className={`right ${pnlClass(t.realized_pnl)}`}>
                        {t.realized_pnl >= 0 ? "Win" : "Loss"}
                      </td>
                    ) : (
                      <td className="right muted">–</td>
                    )}
                    {showLatency ? (
                      <td className="right muted">
                        {t.copy_latency_ms != null
                          ? ms(t.copy_latency_ms)
                          : "–"}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!error && trades.length > 0 ? (
          <div className="status">{trades.length} Trades</div>
        ) : null}
      </div>

      {mode === "live" && (data?.live_open_orders || []).length > 0 ? (
        <div className="section">
          <h2>Live Open Orders</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Side</th>
                  <th className="right">Price</th>
                  <th className="right">Size</th>
                  <th className="right">Matched</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(data?.live_open_orders || []).map((o, i) => (
                  <tr key={`${o.id}-${i}`}>
                    <td className="mono muted">
                      {(o.id || "").slice(0, 12)}…
                    </td>
                    <td>
                      <span className={`badge ${(o.side || "").toLowerCase()}`}>
                        {o.side}
                      </span>
                    </td>
                    <td className="right">{price(o.price)}</td>
                    <td className="right">
                      {Number(o.original_size || 0).toFixed(2)}
                    </td>
                    <td className="right">
                      {Number(o.size_matched || 0).toFixed(2)}
                    </td>
                    <td className="muted">{o.status || "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {(data?.skipped_trades || []).length > 0 ? (
        <div className="section">
          <h2>Skipped</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Strategie</th>
                  <th>Action</th>
                  <th>Markt</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {(data?.skipped_trades || []).slice(0, 100).map((r, i) => (
                  <tr key={`${r.ts}-${i}`}>
                    <td className="muted">{dt(r.ts)}</td>
                    <td>
                      <span className={`badge strategy-${r.strategy}`}>
                        {strategyLabel(r.strategy || "system")}
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${r.action}`}>{r.action}</span>
                    </td>
                    <td>
                      <div>{r.slug}</div>
                      <div className="muted">{r.reason || ""}</div>
                    </td>
                    <td className="bad">{r.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      <div className="section">
        <h2>Aktivität</h2>
        <div className="filter-row">
          <select
            value={activityFilter}
            onChange={(e) => setActivityFilter(e.target.value)}
          >
            <option value="all">Alle</option>
            <option value="buy">Buys</option>
            <option value="sell">Sells</option>
            <option value="skip">Skips</option>
            <option value="scan">Scans</option>
            <option value="error">Errors</option>
          </select>
          <div className="pill-row" style={{ marginTop: 0 }}>
            {mode === "live" ? (
              <>
                <span className="pill">
                  Letzter Sync:{" "}
                  {data?.live_sync?.last_sync
                    ? dt(data.live_sync.last_sync)
                    : "–"}
                </span>
                <span className="pill">
                  {data?.live_sync?.open_orders || 0} offene Orders
                </span>
                <span className="pill">
                  {data?.live_sync?.seen_trades || 0} gesehene Trades
                </span>
              </>
            ) : null}
            <span className="pill">Logs: 3-Tage-Retention</span>
          </div>
        </div>
        {activity.length === 0 ? (
          <div className="status">Noch keine Aktivität protokolliert</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Zeit</th>
                  <th>Level</th>
                  <th>Decision</th>
                  <th>Strategie</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {activity.slice(0, 300).map((r: ActivityRow, i) => {
                  const level = (r.level || "info").toLowerCase();
                  const decision = (r.decision || r.event || "–").toLowerCase();
                  const detailParts: string[] = [];
                  const headline = r.skip_summary || r.message || r.reason || "";
                  if (headline) detailParts.push(headline);
                  if (r.city)
                    detailParts.push(
                      `${r.city}${r.event_date ? ` · ${r.event_date}` : ""}`,
                    );
                  if (r.ensemble_members != null)
                    detailParts.push(
                      `Ensemble ${r.ensemble_members} (${r.ensemble_source || "–"})`,
                    );
                  if (r.api_error) detailParts.push(`API: ${r.api_error}`);
                  if (r.notable_buckets?.length) {
                    detailParts.push(
                      r.notable_buckets
                        .map((b) => {
                          const ask =
                            b.ask == null ? "kein Ask" : `${price(b.ask)}¢`;
                          const pm =
                            b.p_model != null
                              ? `${Math.round(b.p_model * 100)}%`
                              : "–";
                          return `${b.bucket}: ${ask}, P=${pm} → ${failLabel(b.fail)}`;
                        })
                        .join(" | "),
                    );
                  } else if (r.candidates != null) {
                    detailParts.push(
                      `candidates=${r.candidates} markets=${r.match_markets ?? "–"} events=${r.events_in_horizon ?? "–"}`,
                    );
                  } else if (r.near_miss) {
                    const n = r.near_miss;
                    detailParts.push(
                      `Nächster Kandidat: ${n.bucket} Ask ${
                        n.ask == null ? "kein" : `${price(n.ask)}¢`
                      }, P=${Math.round((n.p_model || 0) * 100)}% → ${failLabel(n.fail)}`,
                    );
                  }
                  if (r.bucket) detailParts.push(r.bucket);
                  if (r.slug) detailParts.push(r.slug);
                  if (r.latency_ms != null)
                    detailParts.push(`detect ${ms(r.latency_ms)}`);
                  if (r.fill_latency_ms != null)
                    detailParts.push(`fill ${ms(r.fill_latency_ms)}`);
                  if (r.order_id) detailParts.push(`order ${r.order_id}`);
                  if (r.clob_trade_id)
                    detailParts.push(`trade ${r.clob_trade_id}`);
                  return (
                    <tr key={`${r.ts}-${i}`}>
                      <td className="muted">{dt(r.ts)}</td>
                      <td>
                        <span className={`badge level-${level}`}>{level}</span>
                      </td>
                      <td className={`mono decision-${decision}`}>
                        {r.decision || r.event || "–"}
                      </td>
                      <td>
                        <span
                          className={`badge strategy-${r.strategy || "system"}`}
                        >
                          {strategyLabel(r.strategy || "system")}
                        </span>
                      </td>
                      <td>
                        <div>{detailParts[0] || ""}</div>
                        <div className="muted mono">
                          {detailParts.slice(1).join(" · ")}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
