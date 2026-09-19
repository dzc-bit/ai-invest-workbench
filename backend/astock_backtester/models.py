from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


class ConditionOperator(str, Enum):  # noqa: UP042 - str+Enum keeps pydantic JSON serialization stable
    AND = "and"
    OR = "or"
    SCORE = "score"


class ConditionNode(BaseModel):
    id: str
    condition_id: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    weight: float | None = None
    data_lag_days: int = 0
    expression: str | None = None

    @field_validator("data_lag_days")
    @classmethod
    def data_lag_days_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("data_lag_days must be >= 0")
        return value


class ConditionGroup(BaseModel):
    id: str
    operator: ConditionOperator
    conditions: list[ConditionNode]

    @model_validator(mode="after")
    def require_conditions(self) -> ConditionGroup:
        if not self.conditions:
            raise ValueError("condition group must contain at least one condition")
        return self


class StrategyConfig(BaseModel):
    name: str
    market_filters: list[ConditionNode] = Field(default_factory=list)
    entry_groups: list[ConditionGroup]
    exit_rules: list[ConditionNode] = Field(default_factory=list)
    score_threshold: float | None = None

    @model_validator(mode="after")
    def require_entry_groups(self) -> StrategyConfig:
        if not self.entry_groups:
            raise ValueError("strategy must contain at least one entry group")
        if self.score_threshold is not None and self.score_threshold < 0:
            raise ValueError("score_threshold must be >= 0")
        return self


class BacktestSettings(BaseModel):
    start_date: date
    end_date: date
    initial_cash: float
    stock_pool: Literal["all", "main_board", "gem", "star", "beijing", "custom"] = "all"
    custom_symbols: list[str] = Field(default_factory=list)
    benchmark_symbol: str = "000300.SH"
    buy_price: Literal["next_open"] = "next_open"
    conservative_execution: bool = True
    limit_up_blocks_buy: bool = True
    limit_down_blocks_sell: bool = True
    fee_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    slippage_rate: float = 0.0005
    position_sizing_mode: Literal["fixed_ratio", "equal_slots"] = "equal_slots"
    position_size_pct: float = 0.2
    fixed_holding_days: int = 5
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    max_positions: int = 10
    max_daily_buys: int = 3
    min_listing_days: int = 60
    exclude_st: bool = True

    @model_validator(mode="after")
    def validate_dates_and_money(self) -> BacktestSettings:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")
        if self.stamp_tax_rate < 0:
            raise ValueError("stamp_tax_rate must be >= 0")
        if self.slippage_rate < 0:
            raise ValueError("slippage_rate must be >= 0")
        if self.position_size_pct <= 0 or self.position_size_pct > 1:
            raise ValueError("position_size_pct must be > 0 and <= 1")
        if self.fixed_holding_days < 1:
            raise ValueError("fixed_holding_days must be >= 1")
        if self.max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        if self.max_daily_buys < 1:
            raise ValueError("max_daily_buys must be >= 1")
        if self.min_listing_days < 0:
            raise ValueError("min_listing_days must be >= 0")
        if self.stop_loss_pct is not None and self.stop_loss_pct >= 0:
            raise ValueError("stop_loss_pct must be negative")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        self.custom_symbols = [str(symbol).strip() for symbol in self.custom_symbols if str(symbol).strip()]
        if self.stock_pool == "custom" and not self.custom_symbols:
            raise ValueError("custom stock pool requires at least one symbol")
        return self


class DatasetCoverage(BaseModel):
    dataset: str
    symbols: int
    start_date: date | None
    end_date: date | None
    # 可行动缺口：尾部停更 + thin day（疑似写入失败）的内部洞。
    missing_rows: int = 0
    # 停牌类缺行：市场正常日（横截面行数正常）的内部洞——公开渠道天然没有
    # 停牌日 K 线，这些行不可补、也不是数据质量问题，单列以保证数字诚实。
    suspension_rows: int = 0


class ServiceLogEntry(BaseModel):
    level: Literal["info", "warning", "error"]
    message: str


class DailyBarsCoverageItem(BaseModel):
    symbol: str
    start_date: date | None
    end_date: date | None
    rows: int
    missing_trade_dates: list[date] = Field(default_factory=list)
    missing_capital_flow_dates: list[date] = Field(default_factory=list)
    missing_market_cap_dates: list[date] = Field(default_factory=list)
    listing_date: date | None = None
    delisted_date: date | None = None
    lifecycle_status: Literal["unknown", "listed", "delisted"] = "unknown"


class DailyBarsCoverageResponse(BaseModel):
    items: list[DailyBarsCoverageItem]


class DataOperationResult(BaseModel):
    status: Literal["ok", "partial"]
    imported_rows: int
    returned_rows: int = 0
    filled_missing_rows: int = 0
    requested_symbols: list[str] = Field(default_factory=list)
    fetched_symbols: list[str] = Field(default_factory=list)
    missing_symbols: list[str] = Field(default_factory=list)
    skipped_symbols: list[str] = Field(default_factory=list)
    coverage: list[DatasetCoverage]
    logs: list[ServiceLogEntry] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)


class ServiceHealth(BaseModel):
    ok: bool
    cache_path: str
    port: int | None = None
    process_id: int | None = None
    executable_path: str | None = None
    executable_sha256: str | None = None
    started_at: datetime | None = None
    instance_id: str | None = None
    coverage: list[DatasetCoverage]
    coverage_refreshing: bool = False


class SyncJobStatus(BaseModel):
    job_id: str
    mode: Literal["full_market_bootstrap", "capital_flow_backfill", "incremental_update", "retry_failed"]
    status: Literal["running", "cancelling", "cancelled", "completed", "completed_with_errors", "failed"]
    total_symbols: int
    completed_symbols: int = 0
    failed_symbols: int = 0
    processed_symbols: int = 0
    skipped_symbols: int = 0
    imported_rows: int = 0
    returned_rows: int = 0
    filled_missing_rows: int = 0
    filled_daily_rows: int = 0
    filled_market_cap_rows: int = 0
    current_symbol: str | None = None
    start_date: date
    end_date: date
    errors: list[str] = Field(default_factory=list)
    last_error: str | None = None
    recent_failures: list[dict[str, Any]] = Field(default_factory=list)


class MarketIndexQuote(BaseModel):
    symbol: str
    name: str
    last: float
    previous_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    source: str
    updated_at: datetime | None = None


class MarketBreadth(BaseModel):
    up: int
    down: int
    flat: int
    total: int
    source: str
    distribution: dict[str, int] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize_model(self, handler):
        data = handler(self)
        if not self.distribution:
            data.pop("distribution", None)
        return data


class SectorMover(BaseModel):
    name: str
    change_pct: float
    leading_symbol: str | None = None
    source: str


class RealtimeMarketSnapshot(BaseModel):
    status: Literal["live", "stale", "unavailable"]
    source: str
    updated_at: datetime
    market_phase: Literal["trading", "pre_open", "lunch_break", "post_close", "non_trading"] = "trading"
    indexes: list[MarketIndexQuote] = Field(default_factory=list)
    breadth: MarketBreadth | None = None
    strong_sectors: list[SectorMover] = Field(default_factory=list)
    yesterday_strong_sectors: list[SectorMover] = Field(default_factory=list)
    message: str
    diagnostics: list[str] = Field(default_factory=list)


class MarketNewsItem(BaseModel):
    title: str
    summary: str | None = None
    source: str
    published_at: datetime | None = None
    url: str | None = None
    tags: list[str] = Field(default_factory=list)
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"


class MarketNewsResponse(BaseModel):
    updated_at: datetime
    source: str
    items: list[MarketNewsItem] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class MarketCommentaryPoint(BaseModel):
    title: str
    detail: str
    weight: Literal["high", "medium", "low"] = "medium"


class MarketCommentaryResponse(BaseModel):
    updated_at: datetime
    trade_date: date
    source: str
    mode: Literal[
        "intraday",
        "lunch_break_review",
        "post_close",
        "non_trading_review",
        "news_fallback",
        "local_brief_review",
    ] = "intraday"
    stance: Literal["positive", "neutral", "defensive"]
    summary: str
    drivers: list[MarketCommentaryPoint] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_watch: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class ClsFinanceTlinePoint(BaseModel):
    date: int | None = None
    minute: int
    last_px: float
    change: float | None = None


class ClsFinanceAnchor(BaseModel):
    code: str
    name: str
    article_id: int | None = None
    c_time: str | None = None
    direction: Literal["up", "down", "flat"] = "flat"
    url: str | None = None


class ClsFinancePlate(BaseModel):
    code: str
    name: str
    change_pct: float | None = None


class ClsFinancePoolItem(BaseModel):
    symbol: str
    name: str
    change_pct: float | None = None
    last: float | None = None
    time: str | None = None
    reason: str | None = None
    limit_up_days: int | None = None
    plates: list[ClsFinancePlate] = Field(default_factory=list)


class ClsFinanceEmotion(BaseModel):
    market_degree: float | None = None
    market_degree_source: str | None = None
    market_degree_label: str | None = None
    shsz_balance: str | None = None
    shsz_balance_change: str | None = None
    breadth: MarketBreadth | None = None
    up_limit: int | None = None
    open_limit: int | None = None
    performance: str | None = None


class ClsFinanceResponse(BaseModel):
    updated_at: datetime
    source: str = "cls-finance"
    source_url: str = "https://www.cls.cn/finance"
    preclose_px: float | None = None
    tline: list[ClsFinanceTlinePoint] = Field(default_factory=list)
    anchors: list[ClsFinanceAnchor] = Field(default_factory=list)
    emotion: ClsFinanceEmotion | None = None
    up_pool: list[ClsFinancePoolItem] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class MarketNewsTheme(BaseModel):
    title: str
    summary: str
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"
    source_count: int = 0
    headlines: list[str] = Field(default_factory=list)


class MarketNewsSummaryResponse(BaseModel):
    updated_at: datetime
    source: str
    item_count: int
    themes: list[MarketNewsTheme] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class MarketBriefingLink(BaseModel):
    title: str
    url: str | None = None
    published_at: datetime | None = None
    category: str | None = None


class MarketBriefingTable(BaseModel):
    title: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)


class MarketBriefingSection(BaseModel):
    title: str
    content: str | None = None
    links: list[MarketBriefingLink] = Field(default_factory=list)
    tables: list[MarketBriefingTable] = Field(default_factory=list)


class MarketBriefingResponse(BaseModel):
    kind: Literal["fupan", "zaopan"]
    updated_at: datetime
    source: str
    source_url: str | None = None
    summary: str
    sections: list[MarketBriefingSection] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class RiskAlertItem(BaseModel):
    symbol: str
    name: str
    risk_type: str
    reason: str
    severity: Literal["high", "medium", "low"]
    source: str
    detected_at: datetime


class RiskAlertsResponse(BaseModel):
    updated_at: datetime
    source: str
    items: list[RiskAlertItem] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class ConditionValidationError(BaseModel):
    code: str
    message: str


class ConditionValidationResult(BaseModel):
    ok: bool
    normalized_text: str
    condition: ConditionNode | None = None
    errors: list[ConditionValidationError] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class StockSymbolValidationResult(BaseModel):
    ok: bool
    valid_symbols: list[str] = Field(default_factory=list)
    invalid_symbols: list[str] = Field(default_factory=list)
    normalized_symbols: list[str] = Field(default_factory=list)
    source: str


class RecommendedStrategy(BaseModel):
    id: str
    name: str
    description: str
    suitable_market: str
    risk_note: str
    example_conditions: list[str] = Field(default_factory=list)
    scenario: str
    featured: bool = False
    required_datasets: list[str] = Field(default_factory=list)
    capability_note: str | None = None
    strategy: StrategyConfig


class RecommendedStrategiesResponse(BaseModel):
    items: list[RecommendedStrategy]


class PreflightIssue(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"]
    dataset: str | None = None


class Trade(BaseModel):
    symbol: str
    buy_signal_date: date
    buy_date: date
    sell_signal_date: date | None = None
    sell_date: date | None = None
    buy_price: float
    sell_price: float | None = None
    shares: int
    planned_amount: float
    buy_amount: float
    sell_amount: float | None = None
    target_position_pct: float
    actual_position_pct: float
    buy_reason: list[str]
    sell_reason: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    pnl: float | None = None
    pnl_pct: float | None = None


class StrategyMatch(BaseModel):
    symbol: str
    signal_date: date
    trade_date: date
    name: str | None = None
    close: float
    change_pct: float | None = None
    reasons: list[str]
    rank_score: float


class DailyStrategyMatches(BaseModel):
    signal_date: date
    trade_date: date
    matches: list[StrategyMatch]


class EquityPoint(BaseModel):
    trade_date: date
    equity: float
    cash: float
    market_value: float
    drawdown_pct: float


class BacktestMetrics(BaseModel):
    total_return_pct: float
    annualized_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    trade_count: int
    average_trade_return_pct: float
    average_position_pct: float
    max_position_pct: float


class BacktestResult(BaseModel):
    metrics: BacktestMetrics
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    preflight_issues: list[PreflightIssue] = Field(default_factory=list)
    latest_strategy_matches: DailyStrategyMatches | None = None
