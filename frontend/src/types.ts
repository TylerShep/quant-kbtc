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
  symbol: string;
  data: Features;
  state: MarketState;
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
}

export interface PaperTrade {
  ticker: string;
  direction: string;
  pnl: number;
  exit_reason: string;
  exit_time: string;
}

export interface PaperState {
  has_position: boolean;
  position: PaperPosition | null;
  total_trades: number;
  recent_trades: PaperTrade[];
}

export interface StatusResponse {
  market_states: Record<string, MarketState>;
  atr: ATRState;
  risk: RiskState;
  paper: PaperState;
}

export interface PnLPoint {
  time: number;
  value: number;
}
