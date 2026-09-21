export type DashMode = "paper" | "live";

export type StrategyCard = {
  name: string;
  display_name?: string;
  trades: number;
  buys: number;
  sells: number;
  win_rate: number;
  win_rate_pct?: number;
  cash: number;
  positions_value: number;
  total_value?: number;
  starting_balance: number;
  pnl: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  roi_pct: number;
  sharpe_ratio?: number;
  max_drawdown: number;
  total_fees?: number;
};

export type Portfolio = {
  cash: number;
  positions: number;
  total: number;
  pnl: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  roi_pct: number;
  trades: number;
  buys: number;
  sells: number;
  win_rate: number;
  win_rate_pct?: number;
  max_drawdown: number;
  fees: number;
  avg_trade: number;
  by_strategy: StrategyCard[];
};

export type PositionRow = {
  strategy: string;
  market_question: string;
  outcome: string;
  shares: number;
  avg_entry_price: number;
  live_price: number;
  unrealized_pnl: number;
  percent_pnl: number;
  url?: string;
};

export type TradeRow = {
  strategy: string;
  market_question: string;
  outcome?: string;
  side: string;
  amount_usd: number;
  avg_price: number;
  created_at?: string;
  realized_pnl?: number | null;
  copy_latency_ms?: number | null;
  url?: string;
};

export type CopyWallet = {
  address: string;
  label?: string;
  source?: string;
};

export type CopyMeta = {
  active?: boolean;
  username?: string;
  wallet?: string;
  scale?: number | null;
  seen_trades?: number;
  wallets?: CopyWallet[];
  latency?: {
    count?: number;
    avg_ms?: number;
    p50_ms?: number;
    p95_ms?: number;
    last_ms?: number;
  };
};

export type ActivityRow = {
  ts?: string;
  level?: string;
  decision?: string;
  event?: string;
  strategy?: string;
  message?: string;
  reason?: string;
  skip_summary?: string;
  city?: string;
  event_date?: string;
  ensemble_members?: number;
  ensemble_source?: string;
  api_error?: string;
  notable_buckets?: Array<{
    bucket?: string;
    ask?: number | null;
    p_model?: number;
    fail?: string;
  }>;
  candidates?: number;
  match_markets?: number;
  events_in_horizon?: number;
  near_miss?: {
    bucket?: string;
    ask?: number | null;
    p_model?: number;
    fail?: string;
  };
  bucket?: string;
  slug?: string;
  latency_ms?: number;
  fill_latency_ms?: number;
  order_id?: string;
  clob_trade_id?: string;
};

export type PhSignal = {
  ts?: string;
  event?: string;
  group_title?: string;
  slug?: string;
  bucket?: string;
  reason?: string;
  roi_pct?: number;
  ph_edge_no?: number;
  dislocation?: number;
  total_cost?: number;
  is_polymarket_pair?: boolean;
  legs?: Array<{ platform?: string; side?: string }>;
  source?: string;
};

export type DashboardPayload = {
  ok?: boolean;
  error?: string;
  mode: DashMode | string;
  data_dir: string;
  updated_at?: string;
  starting_balance?: number;
  portfolio: Portfolio;
  positions: PositionRow[];
  trades: TradeRow[];
  equity_curve?: Array<{ ts?: string; value: number }>;
  scan_history?: Array<{ ts?: string; total: number }>;
  copy?: CopyMeta;
  activity_log?: ActivityRow[];
  skipped_trades?: Array<{
    ts?: string;
    strategy?: string;
    action?: string;
    slug?: string;
    reason?: string;
    error?: string;
  }>;
  live_open_orders?: Array<{
    id?: string;
    side?: string;
    price?: number;
    original_size?: number;
    size_matched?: number;
    status?: string;
  }>;
  live_sync?: {
    last_sync?: string;
    open_orders?: number;
    seen_trades?: number;
  };
  predictionhunt?: {
    enabled?: boolean;
    api_key_configured?: boolean;
    shadow_only?: boolean;
    scan_arb?: boolean;
    signals?: PhSignal[];
  };
  strategy_labels?: Record<string, string>;
};
