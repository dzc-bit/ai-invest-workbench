"""Scheduled reports: 收盘复盘报告 + 策略库自动体检, stored under 运行产物/AI报告.

Two background jobs share one scheduler thread (60s tick, local time):

- 复盘报告 (``report_time``): gathers the same read-only sources as the AI
  digest (news / realtime / fupan / zaopan / risk / digest items) and writes a
  dated markdown report — model narrative when an LLM is configured, a
  rule-based template otherwise, so the file always appears.
- 策略体检 (``evolution_time``): re-runs every saved strategy on the most
  recent data window, records metric drift against the previous run, sweeps a
  tiny parameter grid and writes the comparison into the report.  It never
  modifies the user's saved strategies — suggestions go into the report only.

Files are exposed via ``GET /ai/reports`` and ``GET /ai/report/file`` so the
desktop app can list and download them.  Everything here is best-effort: a
failing job logs and retries the next day.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.context import wrap_untrusted
from astock_backtester.models import BacktestSettings, StrategyConfig

REPORT_DIR_NAME = "AI报告"
EVOLUTION_STATE_FILE = "evolution-state.json"
SCHEDULER_TICK_SECONDS = 30.0
EVOLUTION_MAX_STRATEGIES = 12
EVOLUTION_WINDOW_DAYS = 180
EVOLUTION_WARMUP_DAYS = 150


@dataclass
class ReportFileMeta:
    name: str
    size: int
    created_at: str


class ReportStore:
    """Markdown files under 运行产物/AI报告; names are safe, reads are validated."""

    def __init__(self, ai_base_dir: str | Path) -> None:
        self._dir = Path(ai_base_dir) / REPORT_DIR_NAME
        self._state_path = self._dir / EVOLUTION_STATE_FILE

    @property
    def directory(self) -> Path:
        return self._dir

    def list(self) -> list[ReportFileMeta]:
        if not self._dir.exists():
            return []
        items: list[ReportFileMeta] = []
        for path in self._dir.glob("*.md"):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                ReportFileMeta(
                    name=path.name,
                    size=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                )
            )
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items

    def read(self, name: str) -> str | None:
        safe = _safe_report_name(name)
        if safe is None:
            return None
        path = self._dir / safe
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def save(self, name: str, content: str) -> ReportFileMeta:
        safe = _safe_report_name(name)
        if safe is None:
            raise ValueError(f"报告文件名不合法：{name}")
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / safe
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
        stat = path.stat()
        return ReportFileMeta(
            name=safe,
            size=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        )

    def load_evolution_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_evolution_state(self, state: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp_path, self._state_path)


def _safe_report_name(name: str) -> str | None:
    cleaned = str(name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or not cleaned.endswith(".md"):
        return None
    if cleaned.startswith("."):
        return None
    return cleaned[:120]


def review_report_name(now: datetime) -> str:
    return f"复盘报告-{now.astimezone().strftime('%Y%m%d-%H%M')}.md"


def evolution_report_name(now: datetime) -> str:
    return f"策略体检-{now.astimezone().strftime('%Y%m%d-%H%M')}.md"


def _crawled_review_block(label: str, body: str) -> str:
    # 同 digest._gather_sources：爬取正文进模型上下文前必须套不可信分隔符。
    return f"{label}\n{wrap_untrusted(body)}"


def _gather_review_sources(backend: Any, digest_items: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    try:
        snapshot = backend.realtime_provider.market_snapshot()
        lines = [
            f"- {index.name} {index.last}（{(index.change_pct or 0):+.2f}%）" for index in snapshot.indexes[:5]
        ]
        breadth = snapshot.breadth
        if breadth is not None:
            lines.append(f"- 红盘 {breadth.up} / 绿盘 {breadth.down} / 全市场 {breadth.total}")
        if snapshot.strong_sectors:
            lines.append(
                "- 强势板块：" + "、".join(f"{s.name} {(s.change_pct or 0):+.2f}%" for s in snapshot.strong_sectors[:5])
            )
        sections.append(_crawled_review_block("【实时行情】(" + snapshot.status + ")", "\n".join(lines)))
    except Exception:  # noqa: BLE001
        pass
    try:
        news = backend.news_provider.latest_news()
        headlines = [f"- {item.title}（{item.source}）" for item in news.items[:15]]
        if headlines:
            sections.append(_crawled_review_block("【新闻/电报】", "\n".join(headlines)))
    except Exception:  # noqa: BLE001
        pass
    try:
        fupan = backend.briefing_provider.latest_fupan()
        if fupan.summary:
            sections.append(_crawled_review_block("【同花顺复盘】", fupan.summary[:600]))
    except Exception:  # noqa: BLE001
        pass
    try:
        zaopan = backend.briefing_provider.latest_zaopan()
        if zaopan.summary:
            sections.append(_crawled_review_block("【同花顺早盘】", zaopan.summary[:400]))
    except Exception:  # noqa: BLE001
        pass
    try:
        alerts = backend.risk_provider.current_alerts()
        if alerts.items:
            sections.append(f"【风险提示】共 {len(alerts.items)} 条，前 5 条：")
            sections.extend(f"- {item.summary}" for item in alerts.items[:5])
    except Exception:  # noqa: BLE001
        pass
    if digest_items:
        lines = [f"- {item.get('title')}｜{item.get('summary')}" for item in digest_items[:6]]
        sections.append("【AI 聚合要点】\n" + "\n".join(lines))
    return "\n\n".join(sections)


REVIEW_PROMPT = """你是 A 股收盘复盘撰稿人。基于以下当日多源数据，输出一份 markdown 复盘报告（500-900 字）：
# {date} 收盘复盘
## 大盘与量能（指数涨跌、红绿家数、量能观察；数据缺失的部分明确写“数据缺失”）
## 主线与板块（从新闻/复盘/涨停信息归纳 1-3 条主线，注明来源）
## 消息面要点（3-5 条，标注来源）
## 风险与明日关注（2-3 条，含风险提示）
只使用给定数据中的事实与数字，禁止编造；结尾加一行“本报告由本地 AI 自动生成，仅供辅助观察，不构成投资建议。”。

数据：
{data}"""


def _fallback_review_report(now_local: datetime, sources: str) -> str:
    header = f"# {now_local.strftime('%Y-%m-%d')} 收盘复盘（数据摘要版）\n\n"
    note = "> AI 模型未配置或调用失败，本报告为原始数据摘要，仅供参考，不构成投资建议。\n\n"
    return header + note + (sources or "（当日未抓取到可用数据）")


def _load_saved_strategies(ai_base_dir: Path) -> list[dict[str, Any]]:
    path = Path(ai_base_dir) / "策略配置" / "saved-strategies.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and isinstance(item.get("strategy"), dict)]


def _run_strategy_backtest(
    backend: Any,
    strategy_payload: dict[str, Any],
    start_date: str,
    end_date: str,
    *,
    frame: Any = None,
    fixed_holding_days: int | None = None,
    take_profit_pct: float | None = None,
) -> dict[str, Any] | None:
    """Run one saved strategy over [start_date, end_date]; returns metrics.

    ``frame`` lets one scheduler job reuse a single warehouse read across all
    strategies and parameter variants (the parquet scan dominates runtime)."""
    try:
        strategy = StrategyConfig.model_validate(strategy_payload)
        settings = BacktestSettings(
            start_date=start_date,
            end_date=end_date,
            initial_cash=1_000_000,
        )
        if fixed_holding_days is not None:
            settings = settings.model_copy(update={"fixed_holding_days": max(1, fixed_holding_days)})
        if take_profit_pct is not None:
            settings = settings.model_copy(update={"take_profit_pct": take_profit_pct})
    except Exception:  # noqa: BLE001 - 单个策略配置坏了不能拖垮整份报告
        return None
    if (settings.end_date - settings.start_date).days > _STRATEGY_BACKTEST_MAX_RANGE_DAYS:
        return None
    from astock_backtester.backtest_runner import run_configured_backtest

    if frame is None:
        warmup_start = settings.start_date - timedelta(days=EVOLUTION_WARMUP_DAYS)
        frame = backend.warehouse.read_daily_bars(
            start_date=warmup_start.isoformat(),
            end_date=settings.end_date.isoformat(),
            require_ohlc=True,
        )
    if frame is None or frame.empty:
        return None
    try:
        result = run_configured_backtest(frame, strategy, settings)
    except Exception:  # noqa: BLE001
        return None
    return {
        "metrics": result.metrics.model_dump(mode="json"),
        "strategy": strategy.model_dump(mode="json"),
    }


EVOLUTION_VARIANTS: tuple[dict[str, Any], ...] = (
    {"fixed_holding_days": 3},
    {"fixed_holding_days": 8},
    {"take_profit_pct": 0.08},
    {"take_profit_pct": 0.15},
)
_STRATEGY_BACKTEST_MAX_RANGE_DAYS = 366


def _evolution_for_strategy(
    backend: Any,
    preset: dict[str, Any],
    start_date: str,
    end_date: str,
    *,
    frame: Any = None,
) -> dict[str, Any] | None:
    strategy_payload = preset.get("strategy") or {}
    base = _run_strategy_backtest(backend, strategy_payload, start_date, end_date, frame=frame)
    if base is None:
        return None
    variants: list[dict[str, Any]] = []
    for override in EVOLUTION_VARIANTS:
        variant = _run_strategy_backtest(backend, strategy_payload, start_date, end_date, frame=frame, **override)
        if variant is None:
            continue
        variants.append(
            {
                "params": override,
                "total_return_pct": variant["metrics"].get("total_return_pct"),
                "max_drawdown_pct": variant["metrics"].get("max_drawdown_pct"),
                "trade_count": variant["metrics"].get("trade_count"),
            }
        )
    better = [item for item in variants if isinstance(item.get("total_return_pct"), (int, float))]
    best_variant = max(better, key=lambda item: float(item["total_return_pct"])) if better else None
    base_return = base["metrics"].get("total_return_pct")
    if best_variant and isinstance(base_return, (int, float)) and float(best_variant["total_return_pct"]) > float(base_return) + 0.01:
        suggestion = (
            f"参数 {json.dumps(best_variant['params'], ensure_ascii=False)} 在同窗口收益 "
            f"{best_variant['total_return_pct']:+.1%}（当前 {base_return:+.1%}），可考虑在策略工作台验证。"
        )
    else:
        suggestion = "当前默认参数在附近网格中已接近最优，暂无更优建议。"
    return {
        "name": str(preset.get("name") or strategy_payload.get("name") or "未命名策略"),
        "metrics": base["metrics"],
        "variants": variants,
        "suggestion": suggestion,
    }


def _evolution_markdown(now_local: datetime, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# {now_local.strftime('%Y-%m-%d')} 策略库自动体检报告",
        "",
        f"体检窗口：最近 {EVOLUTION_WINDOW_DAYS} 个自然日（含 {EVOLUTION_WARMUP_DAYS} 日线预热）；对照网格：持仓天数 3/8 与止盈 8%/15%。",
        "",
        "| 策略 | 总收益 | 年化 | 最大回撤 | 胜率 | 交易数 | 体检结论 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        metrics = row.get("metrics") or {}
        overfit = row.get("overfit") or {}
        level = str(overfit.get("level") or "none")
        level_text = {"critical": "高风险信号", "warning": "存在疑点", "info": "轻微提示", "none": "未见明显问题"}.get(level, level)
        lines.append(
            f"| {row.get('name')} | {metrics.get('total_return_pct', 0):+.2%} "
            f"| {metrics.get('annualized_return_pct', 0):+.2%} "
            f"| {metrics.get('max_drawdown_pct', 0):.2%} "
            f"| {metrics.get('win_rate_pct', 0):.1%} | {metrics.get('trade_count', 0)} | {level_text} |"
        )
    lines.append("")
    for row in rows:
        lines.append(f"## {row.get('name')}")
        overfit = row.get("overfit") or {}
        for finding in overfit.get("findings") or []:
            lines.append(f"- ⚠ {finding.get('message')}")
        lines.append(f"- 参数建议：{row.get('suggestion')}")
        lines.append("")
    lines.append("> 本报告由本地定时任务自动生成，不构成投资建议；参数建议请在策略工作台自行回测验证后再采用。")
    return "\n".join(lines)


class ScheduledReportEngine:
    """One daemon thread; every job runs at most once per local day."""

    def __init__(
        self,
        *,
        backend: Any,
        model_provider: Callable[[], Any],
        config_provider: Callable[[], AiConfig],
        store: ReportStore,
        digest_items_provider: Callable[[], list[dict[str, Any]]] | None = None,
        ai_base_dir: str | Path | None = None,
        check_interval_seconds: float = SCHEDULER_TICK_SECONDS,
    ) -> None:
        self._backend = backend
        self._model_provider = model_provider
        self._config_provider = config_provider
        self._store = store
        self._digest_items_provider = digest_items_provider or (lambda: [])
        self._ai_base_dir = Path(ai_base_dir) if ai_base_dir else None
        self._interval = check_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_report_date: str | None = None
        self._last_evolution_date: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ai-report-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 调度线程绝不能崩
                self._log("warning", f"AI 定时报告调度失败：{exc}")

    def tick(self) -> dict[str, Any]:
        config = self._config_provider()
        now_local = datetime.now().astimezone()
        today = now_local.strftime("%Y-%m-%d")
        ran: list[str] = []
        with self._lock:
            if config.report_enabled and self._time_matches(now_local, config.report_time) and self._last_report_date != today:
                self._last_report_date = today
                ran.append("report")
            if config.evolution_enabled and self._time_matches(now_local, config.evolution_time) and self._last_evolution_date != today:
                self._last_evolution_date = today
                ran.append("evolution")
        for job in ran:
            try:
                if job == "report":
                    self.generate_review_report()
                elif job == "evolution":
                    self.generate_evolution_report()
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"AI 定时任务 {job} 失败：{exc}")
        return {"ran": ran}

    @staticmethod
    def _time_matches(now_local: datetime, hhmm: str) -> bool:
        # 命中目标时刻，或在其后 30 分钟内补跑（桌面端休眠/重启错过整点时）。
        current = _minutes_since_midnight(now_local)
        target = _parse_minutes(hhmm)
        return current == target or 0 < current - target <= 30

    # ------------------------------------------------------------ jobs
    def generate_review_report(self, *, force: bool = False) -> dict[str, Any]:
        config = self._config_provider()
        if not force and not config.report_enabled:
            return {"ok": False, "skipped": "disabled"}
        now_local = datetime.now().astimezone()
        sources = _gather_review_sources(self._backend, self._digest_items_provider())
        if not sources.strip():
            return {"ok": False, "skipped": "no_sources"}
        content = ""
        model = self._model_provider() if config.is_configured() else None
        if model is not None:
            try:
                for event in model.chat(
                    [{"role": "user", "content": REVIEW_PROMPT.format(date=now_local.strftime("%Y-%m-%d"), data=sources)}],
                    tools=None,
                ):
                    if event[0] == "final":
                        content = str((event[1] or {}).get("content") or "")
            except Exception:  # noqa: BLE001 - 模型失败回退数据摘要版
                content = ""
        content = content.strip()
        if not content:
            content = _fallback_review_report(now_local, sources)
        meta = self._store.save(review_report_name(now_local), content)
        self._log("info", f"AI 定时复盘报告已生成：{meta.name}")
        return {"ok": True, "name": meta.name}

    def generate_evolution_report(self, *, force: bool = False) -> dict[str, Any]:
        config = self._config_provider()
        if not force and not config.evolution_enabled:
            return {"ok": False, "skipped": "disabled"}
        if self._ai_base_dir is None:
            return {"ok": False, "skipped": "no_base_dir"}
        presets = _load_saved_strategies(self._ai_base_dir)[:EVOLUTION_MAX_STRATEGIES]
        if not presets:
            return {"ok": False, "skipped": "no_saved_strategies"}
        end_date = self._latest_data_date() or datetime.now().astimezone().date().isoformat()
        start_date = (datetime.fromisoformat(end_date) - timedelta(days=EVOLUTION_WINDOW_DAYS)).date().isoformat()
        # 整份报告只读一次行情框：全市场日线扫描远贵于单次回测。
        try:
            warmup_start = (datetime.fromisoformat(start_date) - timedelta(days=EVOLUTION_WARMUP_DAYS)).date().isoformat()
            frame = self._backend.warehouse.read_daily_bars(
                start_date=warmup_start,
                end_date=end_date,
                require_ohlc=True,
            )
        except Exception:  # noqa: BLE001
            frame = None
        previous = self._store.load_evolution_state()
        rows: list[dict[str, Any]] = []
        next_state: dict[str, Any] = {}
        for preset in presets:
            outcome = _evolution_for_strategy(self._backend, preset, start_date, end_date, frame=frame)
            if outcome is None:
                continue
            key = re.sub(r"\s+", "", str(preset.get("name") or ""))
            before = previous.get(key) or {}
            before_return = before.get("total_return_pct")
            current_return = outcome["metrics"].get("total_return_pct")
            drift = ""
            if isinstance(before_return, (int, float)) and isinstance(current_return, (int, float)):
                delta = float(current_return) - float(before_return)
                drift = f"（较上次体检 {delta:+.1%}）"
            overfit = assess_strategy_overfit(outcome["metrics"])
            outcome["overfit"] = overfit
            warnings = [finding["message"] for finding in overfit.get("findings", [])]
            prefix = drift + ("；" + "；".join(warnings) if warnings else "")
            connector = "；" if prefix else ""
            outcome["suggestion"] = prefix + connector + str(outcome.get("suggestion", ""))
            rows.append(outcome)
            next_state[key] = {"total_return_pct": current_return, "checked_at": datetime.now(UTC).isoformat()}
        if not rows:
            return {"ok": False, "skipped": "no_backtest_data"}
        self._store.save_evolution_state(next_state)
        now_local = datetime.now().astimezone()
        meta = self._store.save(evolution_report_name(now_local), _evolution_markdown(now_local, rows))
        self._log("info", f"AI 策略库体检报告已生成：{meta.name}（{len(rows)} 个策略）")
        return {"ok": True, "name": meta.name, "strategies": len(rows)}

    def _latest_data_date(self) -> str | None:
        try:
            coverage = self._backend.warehouse.coverage()
        except Exception:  # noqa: BLE001
            return None
        for item in coverage:
            if getattr(item, "dataset", "") == "daily_bars" and getattr(item, "end_date", None):
                return str(item.end_date)
        return None

    def _log(self, level: str, message: str) -> None:
        try:
            self._backend.log(level, message)
        except Exception:  # noqa: BLE001
            pass


def _parse_minutes(hhmm: str) -> int:
    parts = hhmm.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def _minutes_since_midnight(now_local: datetime) -> int:
    return now_local.hour * 60 + now_local.minute


def assess_strategy_overfit(metrics: dict[str, Any]) -> dict[str, Any]:
    """Lazy import keeps the reports module import-light for non-AI users."""
    from astock_backtester.ai.overfit import assess_overfit

    return assess_overfit(metrics)
