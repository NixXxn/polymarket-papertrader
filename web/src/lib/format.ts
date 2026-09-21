export const money = (n: number | null | undefined) =>
  new Intl.NumberFormat("de-DE", { style: "currency", currency: "USD" }).format(
    Number(n || 0),
  );

export const pct = (n: number | null | undefined) =>
  `${Number(n || 0).toLocaleString("de-DE", { maximumFractionDigits: 2 })}%`;

export const price = (n: number | null | undefined) =>
  Number(n || 0).toLocaleString("de-DE", { maximumFractionDigits: 4 });

export const dt = (s: string | null | undefined) =>
  s ? new Date(s).toLocaleString("de-DE") : "–";

export const ms = (n: number | null | undefined) =>
  n == null ? "–" : `${Math.round(Number(n)).toLocaleString("de-DE")} ms`;

export const pnlClass = (v: number | null | undefined) =>
  Number(v) >= 0 ? "good" : "bad";

export const shortAddr = (a: string) =>
  a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "–";

export const REJECT_LABELS: Record<string, string> = {
  no_ask: "kein Ask",
  ask_size_too_small: "zu wenig Volumen",
  ask_too_cheap: "Ask <2¢ (settled)",
  ask_too_expensive: "Ask >45¢",
  already_in_position: "bereits investiert",
  order_book_error: "Orderbuch-Fehler",
  low_model_prob: "Modell <20%",
  low_prob_ratio: "Ratio zu niedrig",
  low_edge: "Edge zu klein",
  forecast_mismatch: "Forecast passt nicht",
  ask_too_high: "Ask zu hoch",
  event_outside_horizon: "Event außerhalb 6h",
  low_event_volume: "Volumen zu niedrig",
  prop_market: "Prop-Markt",
  not_match_market: "kein Match",
  market_outside_horizon: "Markt außerhalb 6h",
  no_valid_ask: "kein gültiger Ask",
  max_open_positions: "max Positionen",
  already_in_event: "bereits im Event",
  insufficient_cash: "zu wenig Cash",
};

export const failLabel = (k: string | undefined) =>
  (k && REJECT_LABELS[k]) || k || "–";
