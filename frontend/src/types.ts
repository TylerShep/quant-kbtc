export interface MarketState {
  symbol: string;
  spot_price: number | null;
  kalshi_ticker: string | null;
  best_bid: number | null;
  best_ask: number | null;
  mid: number | null;
  spread: number | null;
  obi?: number | null;
  time_remaining_sec: number | null;
  volume: number | null;
}

export interface Features {
  obi: number;
  total_bid_vol: number;
  total_ask_vol: number;
  spread_cents: number | null;
  spot_price: number | null;
  mid_price: number | null;
}

export interface WSMessage {
  type: string;
  symbol?: string;
  data?: Features;
  state?: MarketState;
  [key: string]: unknown;
}

export interface RiskState {
  can_trade: boolean;
  halt_reason: string | null;
  bankroll: number;
  peak_bankroll: number;
  drawdown_pct: number;
  daily_loss_pct: number;
  weekly_loss_pct: number;
  trades_today: number;
}

export interface ATRState {
  regime: 'LOW' | 'MEDIUM' | 'HIGH';
  atr_pct: number | null;
  smoothed: number | null;
}

export interface PaperPosition {
  ticker: string;
  direction: string;
  contracts: number;
  entry_price: number;
  candles_held: number;
  conviction: string;
  signal_driver?: string;
}

export interface PaperTrade {
  ticker: string;
  direction: string;
  pnl: number;
  exit_reason: string;
  exit_time: string;
}

export interface DBTrade {
  timestamp: string;
  ticker: string;
  direction: string;
  contracts: number;
  entry_price: number | null;
  exit_price: number | null;
  pnl: number;
  pnl_pct: number;
  fees: number;
  exit_reason: string;
  conviction: string;
  regime_at_entry: string;
  candles_held: number;
  closed_at: string | null;
  signal_driver?: string;
}

export interface TradesResponse {
  trades: DBTrade[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface ErroredTrade extends DBTrade {
  error_reason: string;
  flagged_at: string | null;
}

export interface ErroredTradesResponse {
  trades: ErroredTrade[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface EquityPoint {
  time: number;
  bankroll: number;
  peak_bankroll: number;
  drawdown_pct: number;
  daily_pnl: number;
  trade_count: number;
}

export interface EquityResponse {
  equity: EquityPoint[];
}

export interface CumulativeStats {
  initial_bankroll: number;
  total_trades: number;
  total_pnl: number;
  equity: number;
  wins: number;
  losses: number;
  win_rate: number;
  best_trade: number;
  worst_trade: number;
  avg_pnl: number;
}

export interface PaperState {
  has_position: boolean;
  position: PaperPosition | null;
  total_trades: number;
  recent_trades: PaperTrade[];
}

export interface OrphanedPosition {
  ticker: string;
  direction: string;
  contracts: number;
  avg_entry_price: number;
  detected_at: string;
  cause?: string | null;
}

export interface Decision {
  direction: string | null;
  conviction: string;
  obi_dir: string;
  roc_dir: string;
  spread_state?: string;
  signal_driver?: string;
  skip_reason: string | null;
  should_trade: boolean;
}

export interface StatusResponse {
  market_states: Record<string, MarketState>;
  atr: ATRState;
  risk: RiskState;
  paper: PaperState;
  live: PaperState;
  trading_mode?: string;
  trading_paused?: 'off' | 'settling' | 'paused';
  orphaned_positions?: OrphanedPosition[];
  paper_bankroll?: number;
  live_bankroll?: number;
  paper_decision?: Decision | null;
  live_decision?: Decision | null;
  paper_risk?: RiskState;
  live_risk?: RiskState;
}

export interface WSFeedInfo {
  connected: boolean;
  last_message_age_sec: number | null;
  message_count: number;
  connect_attempts: number;
  active_tickers: Record<string, string>;
}

export interface DiagnosticsResponse {
  kalshi_ws: WSFeedInfo;
  spot_ws: WSFeedInfo;
  tick_count: number;
  candle_count: number;
  atr_regime: string;
  trading_mode: string;
}

export interface PnLPoint {
  time: number;
  value: number;
}
