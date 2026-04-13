#!/usr/bin/env python3
"""Deribit options monitoring skill."""

from __future__ import annotations

import functools
import json
import math
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import requests


DERIBIT_API_BASE = "https://www.deribit.com/api/v2"
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
SUPPORTED_CURRENCIES = {"BTC", "ETH"}

# 流向标签常量
FLOW_LABELS = {
    "protective_hedge",  # 保护性对冲
    "premium_collect",  # 收取权利金
    "speculative_put",  # 投机卖Put
    "call_momentum",  # Call追涨
    "covered_call",  # 备兑卖Call
    "call_overwrite",  # Call改仓
    "call_speculative",  # Call投机
    "unknown",  # 未知
}
HEDGE_LABELS = {"protective_hedge", "call_momentum"}
PREMIUM_LABELS = {"premium_collect", "covered_call"}

# 严重程度常量
SEVERITY_THRESHOLDS = {
    "high": 2_000_000,
    "medium": 500_000,
    "info": 0,
}

MONTH_MAP = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(slots=True)
class InstrumentMeta:
    instrument_name: str
    currency: str
    strike: float
    option_type: str
    expiry_dt: datetime
    expiry_ts: int
    dte: int


class DeribitOptionsMonitor:
    """Monitor BTC options on Deribit using public endpoints only."""

    def __init__(self, db_path: str | None = None):
        state_dir = Path(
            os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")
        ).expanduser()
        default_db = (
            state_dir
            / "workspace"
            / "skills"
            / "deribit-options-monitor"
            / ".cache"
            / "deribit_monitor.sqlite3"
        )
        self.db_path = Path(db_path).expanduser() if db_path else default_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 缓存结构: {instrument_name: {"data": ..., "ts": timestamp_ms}}
        self._order_book_cache: dict[str, dict[str, Any]] = {}
        self._instrument_meta_cache: dict[
            str, InstrumentMeta
        ] = {}  # instrument 解析缓存
        self._cache_ttl_seconds = 60  # 缓存 60 秒
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dvol_history (
                    ts INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    close REAL NOT NULL,
                    resolution TEXT NOT NULL,
                    PRIMARY KEY (ts, currency)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_snapshots (
                    ts INTEGER NOT NULL,
                    instrument_name TEXT NOT NULL,
                    strike REAL NOT NULL,
                    expiry_ts INTEGER NOT NULL,
                    dte INTEGER NOT NULL,
                    delta REAL NOT NULL,
                    mark_iv REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    underlying_price REAL NOT NULL,
                    apr REAL NOT NULL,
                    PRIMARY KEY (ts, instrument_name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS large_trade_events (
                    ts INTEGER NOT NULL,
                    trade_id TEXT NOT NULL PRIMARY KEY,
                    instrument_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    amount REAL NOT NULL,
                    index_price REAL NOT NULL,
                    underlying_notional_usd REAL NOT NULL,
                    premium_usd REAL NOT NULL,
                    flow_label TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rv_history (
                    ts INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    rv_7d REAL,
                    rv_30d REAL,
                    rv_90d REAL,
                    resolution TEXT NOT NULL,
                    PRIMARY KEY (ts, currency)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_cache (
                    currency TEXT NOT NULL PRIMARY KEY,
                    scan_ts INTEGER NOT NULL,
                    scan_data TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL DEFAULT 300
                )
                """
            )

    def _utc_now(self) -> datetime:
        return datetime.now(UTC)

    def _now_ms(self) -> int:
        return int(self._utc_now().timestamp() * 1000)

    def _normalize_currency(self, currency: str) -> str:
        normalized = (currency or "").upper().strip()
        if normalized not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"v1 only supports {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return normalized

    def _request_json(
        self,
        path: str,
        params: dict[str, Any],
        timeout: int = REQUEST_TIMEOUT,
        retries: int = 3,
    ) -> dict[str, Any]:
        url = f"{DERIBIT_API_BASE}/{path.lstrip('/')}"
        last_error: Exception | None = None
        for _ in range(retries):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                return payload
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Deribit request failed for {path}: {last_error}")

    def _parse_instrument_name(self, instrument_name: str) -> InstrumentMeta:
        # 使用缓存避免重复解析
        if instrument_name in self._instrument_meta_cache:
            cached = self._instrument_meta_cache[instrument_name]
            # 重新计算 DTE 因为时间会变化
            seconds = (cached.expiry_dt - self._utc_now()).total_seconds()
            dte = max(0, math.ceil(seconds / 86400))
            if dte != cached.dte:
                return InstrumentMeta(
                    instrument_name=cached.instrument_name,
                    currency=cached.currency,
                    strike=cached.strike,
                    option_type=cached.option_type,
                    expiry_dt=cached.expiry_dt,
                    expiry_ts=cached.expiry_ts,
                    dte=dte,
                )
            return cached

        parts = instrument_name.split("-")
        if len(parts) != 4:
            raise ValueError(f"Unexpected instrument name: {instrument_name}")
        currency, date_token, strike_token, side_token = parts

        # 解析日期 token: 支持单日(如 3APR26)或双日(如 29MAY26)
        # 月份缩写固定为3位，需要从日期字符串中提取
        date_token_upper = date_token.upper()
        month_str = None
        day = None

        # 尝试找月份缩写位置
        for month_abbr in MONTH_MAP:
            idx = date_token_upper.find(month_abbr)
            if idx != -1:
                month_str = month_abbr
                day_str = date_token_upper[:idx]
                break

        if month_str is None or not day_str:
            raise ValueError(f"Cannot parse date token: {date_token}")

        try:
            day = int(day_str)
            month = MONTH_MAP[month_str]
        except (ValueError, KeyError) as e:
            raise ValueError(f"Cannot parse date token: {date_token}") from e

        year = 2000 + int(date_token[-2:])
        expiry_dt = datetime(year, month, day, 8, 0, tzinfo=UTC)
        expiry_ts = int(expiry_dt.timestamp() * 1000)
        seconds = (expiry_dt - self._utc_now()).total_seconds()
        dte = max(0, math.ceil(seconds / 86400))
        option_type = "put" if side_token.upper() == "P" else "call"
        result = InstrumentMeta(
            instrument_name=instrument_name,
            currency=currency,
            strike=float(strike_token),
            option_type=option_type,
            expiry_dt=expiry_dt,
            expiry_ts=expiry_ts,
            dte=dte,
        )
        # 存入缓存
        self._instrument_meta_cache[instrument_name] = result
        return result

    def _percentile(self, values: list[float], current: float) -> float | None:
        """计算当前值在历史数据中的百分位数 (0-100)。

        使用标准 rank 方法：percentile = (rank - 1) / (n - 1) * 100
        其中 rank 是当前值按升序排列的位置。
        """
        if not values:
            return None
        # 升序排列
        sorted_values = sorted(values)
        n = len(sorted_values)

        # 找到当前值的排名（从 1 开始）
        rank = 1
        for i, v in enumerate(sorted_values):
            if v >= current:
                rank = i + 1
                break
        else:
            rank = n

        # 使用 rank 方法计算百分位数
        if n == 1:
            return 50.0

        percentile = (rank - 1) / (n - 1) * 100
        return round(percentile, 2)

    def _format_usd(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"${value:,.0f}"

    def _format_pct(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.2f}%"

    def _severity_from_notional(self, notional: float) -> str:
        if notional >= SEVERITY_THRESHOLDS["high"]:
            return "high"
        if notional >= SEVERITY_THRESHOLDS["medium"]:
            return "medium"
        return "info"

    def _risk_emoji(self, abs_delta: float) -> str:
        if abs_delta > 0.30:
            return "⚠️"
        if abs_delta > 0.20:
            return "🟡"
        return "✅"

    def _resample_hourly(self, rows: list[list[float]]) -> list[dict[str, float]]:
        buckets: dict[int, dict[str, float]] = {}
        for row in rows:
            if len(row) < 5:
                continue
            ts = int(row[0])
            close = float(row[4])
            hour_ts = ts - (ts % 3_600_000)
            current = buckets.get(hour_ts)
            if current is None or ts >= int(current["raw_ts"]):
                buckets[hour_ts] = {"ts": hour_ts, "close": close, "raw_ts": ts}
        points = sorted(buckets.values(), key=lambda item: item["ts"])
        return [
            {"ts": int(item["ts"]), "close": float(item["close"])} for item in points
        ]

    def _fetch_dvol_rows(
        self,
        currency: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
    ) -> list[list[float]]:
        resolution_seconds = int(resolution)
        chunk_span_ms = resolution_seconds * 1000 * 900
        all_rows: list[list[float]] = []
        current_start = start_ts

        while current_start < end_ts:
            current_end = min(end_ts, current_start + chunk_span_ms)
            payload = self._request_json(
                "public/get_volatility_index_data",
                {
                    "currency": currency,
                    "resolution": resolution,
                    "start_timestamp": current_start,
                    "end_timestamp": current_end,
                },
            )
            rows = payload.get("result", {}).get("data", [])
            if rows:
                all_rows.extend(rows)
            if current_end >= end_ts:
                break
            current_start = current_end + resolution_seconds * 1000

        deduped: dict[int, list[float]] = {}
        for row in all_rows:
            if len(row) >= 5:
                deduped[int(row[0])] = row
        return [deduped[key] for key in sorted(deduped)]

    def _fetch_dvol_hourly_history(
        self, currency: str
    ) -> tuple[list[dict[str, float]], str]:
        end_ts = self._now_ms()
        start_ts = end_ts - 7 * 24 * 3600 * 1000
        last_error: Exception | None = None
        for resolution in ("3600", "60", "1"):
            try:
                rows = self._fetch_dvol_rows(currency, resolution, start_ts, end_ts)
                points = self._resample_hourly(rows)
                if len(points) >= 24:
                    return points, resolution
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Unable to fetch DVOL history: {last_error}")

    def _store_dvol_points(
        self, currency: str, resolution: str, points: list[dict[str, float]]
    ) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO dvol_history (ts, currency, close, resolution)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (int(point["ts"]), currency, float(point["close"]), resolution)
                    for point in points
                ],
            )

    def _load_dvol_window(self, currency: str, hours: int) -> list[float]:
        cutoff = self._now_ms() - hours * 3600 * 1000
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT close FROM dvol_history
                WHERE currency = ? AND ts >= ?
                ORDER BY ts ASC
                """,
                (currency, cutoff),
            ).fetchall()
        return [float(row["close"]) for row in rows]

    def _get_book_summaries(self, currency: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            "public/get_book_summary_by_currency",
            {"currency": currency, "kind": "option"},
        )
        return list(payload.get("result", []))

    def _get_order_book(self, instrument_name: str) -> dict[str, Any]:
        """获取订单簿数据，带 TTL 缓存。"""
        now_ms = self._now_ms()
        cached = self._order_book_cache.get(instrument_name)
        if cached is not None:
            cached_ts = cached.get("ts", 0)
            if now_ms - cached_ts < self._cache_ttl_seconds * 1000:
                return cached.get("data", {})

        payload = self._request_json(
            "public/get_order_book",
            {"instrument_name": instrument_name, "depth": 1},
        )
        result = payload.get("result", {})
        self._order_book_cache[instrument_name] = {"data": result, "ts": now_ms}
        return result

    def _clean_expired_cache(self) -> None:
        """清理过期缓存。"""
        now_ms = self._now_ms()
        expired_keys = [
            name
            for name, entry in self._order_book_cache.items()
            if now_ms - entry.get("ts", 0) >= self._cache_ttl_seconds * 1000
        ]
        for key in expired_keys:
            del self._order_book_cache[key]

    def _get_last_trades(
        self, currency: str, count: int = 1000
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "public/get_last_trades_by_currency",
            {"currency": currency, "kind": "option", "count": count},
        )
        return list(payload.get("result", {}).get("trades", []))

    def _get_spot_price(self, currency: str) -> dict[str, Any]:
        """获取现货价格"""
        try:
            index_name = f"{currency.lower()}_usd"
            payload = self._request_json(
                "public/get_index_price",
                {"index_name": index_name},
            )
            result = payload.get("result", {})
            return {
                "spot_price": float(result.get("index_price", 0)),
                "delivery_price": float(result.get("estimated_delivery_price", 0)),
            }
        except Exception:
            return {"spot_price": None, "delivery_price": None}

    def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "skill": "deribit-options-monitor",
            "db_path": str(self.db_path),
            "checks": {},
        }
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            checks["checks"]["sqlite"] = {"ok": True}
        except Exception as exc:
            checks["checks"]["sqlite"] = {"ok": False, "error": str(exc)}

        try:
            self._get_book_summaries("BTC")
            checks["checks"]["book_summary"] = {"ok": True}
        except Exception as exc:
            checks["checks"]["book_summary"] = {"ok": False, "error": str(exc)}

        try:
            self._get_last_trades("BTC", count=5)
            checks["checks"]["last_trades"] = {"ok": True}
        except Exception as exc:
            checks["checks"]["last_trades"] = {"ok": False, "error": str(exc)}

        try:
            points, resolution = self._fetch_dvol_hourly_history("BTC")
            checks["checks"]["dvol"] = {
                "ok": True,
                "resolution": resolution,
                "points": len(points),
            }
        except Exception as exc:
            checks["checks"]["dvol"] = {"ok": False, "error": str(exc)}

        checks["ok"] = all(item.get("ok") for item in checks["checks"].values())
        return checks

    def get_dvol_signal(self, currency: str = "BTC") -> dict[str, Any]:
        """获取 DVOL 信号，以 Z-Score 为主，辅以趋势判断。

        改进点：
        1. 以 Z-Score 为主要判断依据
        2. 添加 24h 趋势判断（上涨/下跌/震荡）
        3. 计算置信度（基于 Z-Score 绝对值）
        4. 动态调整阈值（基于 30 天历史数据）
        """
        currency = self._normalize_currency(currency)
        points, resolution = self._fetch_dvol_hourly_history(currency)
        self._store_dvol_points(currency, resolution, points)

        series_24h = self._load_dvol_window(currency, 24) or [
            float(point["close"]) for point in points[-24:]
        ]
        series_7d = self._load_dvol_window(currency, 24 * 7) or [
            float(point["close"]) for point in points
        ]
        current = float(points[-1]["close"])

        # 计算基础统计
        percentile_24h = self._percentile(series_24h, current)
        percentile_7d = self._percentile(series_7d, current)
        mean_7d = mean(series_7d) if series_7d else None
        std_7d = pstdev(series_7d) if len(series_7d) > 1 else None
        z_score = None
        if mean_7d is not None and std_7d and std_7d > 0:
            z_score = round((current - mean_7d) / std_7d, 2)

        # 趋势判断：过去 24h 的变化方向
        trend = "震荡"
        trend_change = 0.0
        if len(series_24h) >= 2:
            first_4h_avg = (
                mean(series_24h[:4]) if len(series_24h) >= 4 else series_24h[0]
            )
            last_4h_avg = (
                mean(series_24h[-4:]) if len(series_24h) >= 4 else series_24h[-1]
            )
            trend_change = last_4h_avg - first_4h_avg
            if trend_change > 0.5:
                trend = "上涨"
            elif trend_change < -0.5:
                trend = "下跌"

        # 动态阈值：基于过去 30 天数据调整
        # 如果 DVOL 历史波动大，阈值也相应提高
        dynamic_thresholds = self._calculate_dynamic_thresholds(currency)

        # 信号判断：以 Z-Score 为主
        signal = "中性"
        recommendation = "波动率处于中位附近，策略以筛选性价比合约为主。"
        confidence = 50  # 默认置信度

        if z_score is not None:
            abs_z = abs(z_score)
            # 高置信度阈值
            high_conf_threshold = dynamic_thresholds["high_conf"]
            # 中置信度阈值
            mid_conf_threshold = dynamic_thresholds["mid_conf"]

            if abs_z >= high_conf_threshold:
                confidence = min(95, 50 + abs_z * 15)  # Z-Score 越大置信度越高
                if z_score > 0:
                    signal = "异常波动(高)"
                    recommendation = "DVOL 显著偏高，权利金昂贵，适合 Sell Put 收租，但需控制尾部风险。"
                else:
                    signal = "异常波动(低)"
                    recommendation = "DVOL 显著偏低，权利金便宜，建议观望或买波动率。"
            elif abs_z >= mid_conf_threshold:
                confidence = min(85, 40 + abs_z * 12)
                if z_score > 0:
                    signal = "高波动率"
                    recommendation = (
                        "权利金偏贵，可关注保守型 Sell Put，Delta 建议 ≤0.20。"
                    )
                else:
                    signal = "低波动率"
                    recommendation = "权利金偏便宜，建议等待或小仓位实验。"
            else:
                confidence = min(70, 30 + abs_z * 10)
                if trend == "上涨":
                    signal = "中性偏多"
                    recommendation = "DVOL 处于上升趋势，关注卖波动率机会。"
                elif trend == "下跌":
                    signal = "中性偏空"
                    recommendation = "DVOL 处于下降趋势，可适度参与 Sell Put。"
                else:
                    signal = "中性"
                    recommendation = "波动率平稳，筛选高 APR 合约为主。"

        # 额外风险提示
        risk_notes = []
        if trend == "上涨" and z_score and z_score > 1:
            risk_notes.append("DVOL 正在上涨，可能还有空间")
        if percentile_7d and percentile_7d >= 85:
            risk_notes.append("7天分位处于高位")

        return {
            "currency": currency,
            "current_dvol": round(current, 2),
            "history_points": len(series_7d),
            "resolution_used": resolution,
            "iv_percentile_24h": percentile_24h,
            "iv_percentile_7d": percentile_7d,
            "z_score_7d": z_score,
            "trend": trend,
            "trend_change": round(trend_change, 2),
            "signal": signal,
            "confidence": round(confidence, 1),
            "recommendation": recommendation,
            "risk_notes": risk_notes if risk_notes else None,
            "latest_ts": int(points[-1]["ts"]),
            "mean_7d": round(mean_7d, 2) if mean_7d is not None else None,
            "std_7d": round(std_7d, 2) if std_7d is not None else None,
            "dynamic_thresholds": dynamic_thresholds,
        }

    def _calculate_dynamic_thresholds(self, currency: str) -> dict[str, float]:
        """基于过去 30 天历史数据计算动态阈值。

        根据 DVOL 的历史波动性动态调整 Z-Score 阈值。
        波动性越大，阈值越高（避免频繁触发）。
        """
        try:
            # 尝试获取 30 天数据
            end_ts = self._now_ms()
            start_ts = end_ts - 30 * 24 * 3600 * 1000
            rows = self._fetch_dvol_rows(currency, "3600", start_ts, end_ts)
            if len(rows) >= 24:  # 至少有一天数据
                closes = [float(row[4]) for row in rows]
                mean_val = mean(closes)
                std_val = pstdev(closes) if len(closes) > 1 else 1.0

                # 计算变异系数 (CV = std / mean)
                cv = std_val / mean_val if mean_val > 0 else 0.1

                # 根据 CV 调整阈值
                # CV < 0.05: 稳定市场，阈值降低
                # CV > 0.10: 波动市场，阈值提高
                if cv < 0.05:
                    base_high, base_mid = 1.5, 1.0
                elif cv < 0.08:
                    base_high, base_mid = 2.0, 1.5
                else:
                    base_high, base_mid = 2.5, 2.0

                return {
                    "high_conf": base_high + cv * 5,  # 0.1-0.25 范围调整
                    "mid_conf": base_mid + cv * 3,
                    "cv": round(cv, 4),
                    "data_days": len(closes) // 24,
                }
        except Exception:
            pass

        # 默认阈值
        return {"high_conf": 2.0, "mid_conf": 1.5, "cv": None, "data_days": 0}

    # ----------------- RV (Realized Volatility) helpers -----------------
    def _fetch_tradingview_chart_data(
        self, currency: str, resolution: str, start_ts: int, end_ts: int
    ) -> list[list[float]]:
        """Fetch TradingView chart data for a currency's perpetual:
        - instrument_name: BTC-PERPETUAL / ETH-PERPETUAL
        - resolution: e.g., "1D"
        Returns rows as [timestamp_ms, open, high, low, close, volume].
        Handles chunking and basic retries.
        """
        currency = self._normalize_currency(currency)
        instrument = f"{currency}-PERPETUAL"
        rows: list[list[float]] = []
        # chunk up to 30-day windows to be safe
        CHUNK_DAYS = 30
        ms_per_day = 86400 * 1000
        chunk_span = CHUNK_DAYS * ms_per_day
        current_start = int(start_ts)
        end_ts = int(end_ts)
        retries = 3
        while current_start < end_ts:
            current_end = min(end_ts, current_start + chunk_span)
            payload = self._request_json(
                "public/get_tradingview_chart_data",
                {
                    "instrument_name": instrument,
                    "resolution": resolution,
                    "start_timestamp": current_start,
                    "end_timestamp": current_end,
                },
            )
            result_rows = payload.get("result", {})
            ticks = result_rows.get("ticks", [])
            opens = result_rows.get("open", [])
            highs = result_rows.get("high", [])
            lows = result_rows.get("low", [])
            closes = result_rows.get("close", [])
            volumes = result_rows.get("volume", [])
            if ticks:
                for i in range(len(ticks)):
                    rows.append(
                        [
                            int(ticks[i]),
                            float(opens[i]) if i < len(opens) else 0.0,
                            float(highs[i]) if i < len(highs) else 0.0,
                            float(lows[i]) if i < len(lows) else 0.0,
                            float(closes[i]) if i < len(closes) else 0.0,
                            float(volumes[i]) if i < len(volumes) else 0.0,
                        ]
                    )
            current_start = current_end
        return rows

    def _compute_rv_from_prices(
        self, close_prices: list[float], timestamps_ms: list[int], window_days: int
    ) -> float | None:
        """Compute annualized Realized Volatility from price series.

        RV = sqrt(sum(log(p_t/p_{t-1})^2)) / sqrt(T_years)
        where T_years = (last_ts - first_ts) / (365.25*24*3600*1000)
        Only use the last `window_days` data points. Return None if insufficient data.
        """
        if len(close_prices) == 0 or len(timestamps_ms) == 0:
            return None
        # Need at least window_days + 1 points to compute window_days returns
        if len(close_prices) < window_days + 1 or len(timestamps_ms) < window_days + 1:
            return None
        # take the last window_days + 1 points
        start_idx = max(0, len(close_prices) - (window_days + 1))
        prices = close_prices[start_idx:]
        times = timestamps_ms[start_idx:]
        log_rs: list[float] = []
        for i in range(1, len(prices)):
            p0 = prices[i - 1]
            p1 = prices[i]
            if p0 <= 0:
                return None
            log_rs.append(math.log(p1 / p0))
        if not log_rs:
            return None
        T_ms = (times[-1] - times[0]) if times[0] != times[-1] else 1
        T_years = T_ms / (365.25 * 24 * 3600 * 1000)
        if T_years <= 0:
            return None
        rv = math.sqrt(sum(r * r for r in log_rs)) / math.sqrt(T_years)
        return float(rv)

    def get_rv_signal(self, currency: str = "BTC") -> dict:
        """Fetch daily data and compute RV signals for 7d/30d/90d windows.

        Returns a dict with rv values and current price metrics.
        """
        currency = self._normalize_currency(currency)
        end_ts = self._now_ms()
        start_ts = end_ts - 100 * 24 * 3600 * 1000  # last 100 days
        rows = self._fetch_tradingview_chart_data(currency, "1D", start_ts, end_ts)
        if not rows:
            return {
                "currency": currency,
                "rv_7d": None,
                "rv_30d": None,
                "rv_90d": None,
                "current_price": None,
                "price_change_7d_pct": None,
                "data_points": 0,
            }
        closes = [r[4] for r in rows]
        times = [r[0] for r in rows]
        current_price = closes[-1] if closes else None
        price_7d_ago = closes[-7] if len(closes) >= 7 else None
        price_change_7d_pct = (
            ((current_price - price_7d_ago) / price_7d_ago * 100.0)
            if price_7d_ago and price_7d_ago != 0
            else None
        )

        rv_7d = self._compute_rv_from_prices(closes, times, 7)
        rv_30d = self._compute_rv_from_prices(closes, times, 30)
        rv_90d = self._compute_rv_from_prices(closes, times, 90)

        # store to rv_history
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rv_history (ts, currency, rv_7d, rv_30d, rv_90d, resolution) VALUES (?, ?, ?, ?, ?, ?)",
                (end_ts, currency, rv_7d, rv_30d, rv_90d, "1D"),
            )

        return {
            "currency": currency,
            "rv_7d": rv_7d,
            "rv_30d": rv_30d,
            "rv_90d": rv_90d,
            "current_price": float(current_price)
            if current_price is not None
            else None,
            "price_change_7d_pct": price_change_7d_pct,
            "data_points": len(rows),
        }

    def get_rv_iv_signal(
        self,
        currency: str = "BTC",
        dvol_signal: dict | None = None,
        rv_signal: dict | None = None,
    ) -> dict:
        """Combine RV signals with DVOL to generate RV/IV signal and suggestion.

        If dvol_signal or rv_signal are already provided (e.g. from run_scan parallel fetching,
        they will be reused to avoid duplicate API calls.
        """
        currency = self._normalize_currency(currency)
        rv = rv_signal if rv_signal is not None else self.get_rv_signal(currency)
        rv_30d = rv.get("rv_30d")
        iv_current = 0.0
        if dvol_signal is None:
            dvol_signal = self.get_dvol_signal(currency)
        iv_current = float(dvol_signal.get("current_dvol", 0) or 0.0)
        rv_ratio = None
        if rv_30d is not None and iv_current:
            rv_ratio = float(rv_30d) / (float(iv_current) / 100.0)
        signal = "合理区间"
        confidence = 50
        recommendation = "保持关注，观察 DVOL 与 RV 的对比"
        bottom_fishing = False
        if rv_ratio is not None:
            if rv_ratio < 0.6:
                signal = "抄底窗口"
                bottom_fishing = True
            elif rv_ratio < 0.8:
                signal = "卖波动率机会"
            elif rv_ratio < 1.2:
                signal = "合理区间"
            elif rv_ratio < 1.5:
                signal = "RV偏高"
            else:
                signal = "危险区域"
            # 简单置信度：离 1.0 越远越有信心
            diff = abs((rv_ratio or 1.0) - 1.0)
            confidence = int(min(100, 60 + diff * 40))
            if signal == "抄底窗口":
                recommendation = (
                    "在 BTC/ETH 7-30 天的价格区间，考虑买入以对冲最大下行风险。"
                )
            elif signal == "卖波动率机会":
                recommendation = "考虑卖出波动率来收取权利金，同时管理尾部风险。"
            elif signal == "合理区间":
                recommendation = "当前价格相对合理，搭配其他信号进行执行。"
            elif signal == "RV偏高":
                recommendation = "警惕持续上涨的隐含波动，注意对端仓位的控制。"
            else:
                recommendation = "市场波动性较高，需小心对冲与卖出策略。"

        return {
            "rv_7d": rv.get("rv_7d"),
            "rv_30d": rv.get("rv_30d"),
            "rv_90d": rv.get("rv_90d"),
            "iv_current": iv_current,
            "rv_iv_ratio": rv_ratio,
            "signal": signal,
            "confidence": confidence,
            "recommendation": recommendation,
            "bottom_fishing": bottom_fishing,
        }

    def get_max_pain(self, currency: str = "BTC") -> dict:
        """Compute Max Pain price from the current option book."""
        currency = self._normalize_currency(currency)
        summaries = self._get_book_summaries(currency)
        spot = self._get_spot_price(currency).get("spot_price") or 0.0
        # Aggregate pains by strike
        pains: dict[float, float] = {}
        total_call_oi = 0.0
        total_put_oi = 0.0
        for s in summaries:
            inst = s.get("instrument_name", "")
            if not inst:
                continue
            meta = self._parse_instrument_name(inst)
            strike = meta.strike
            oi = float(s.get("open_interest") or 0.0)
            if inst.endswith("-P"):
                total_put_oi += oi
                pain = 0.0
                if spot <= strike:
                    pain = (strike - spot) * oi
                pains[strike] = pains.get(strike, 0.0) + pain
            elif inst.endswith("-C"):
                total_call_oi += oi
                pain = 0.0
                if spot >= strike:
                    pain = (spot - strike) * oi
                pains[strike] = pains.get(strike, 0.0) + pain

        if not pains:
            return {
                "currency": currency,
                "spot_price": spot,
                "max_pain_strike": None,
                "max_pain_value": None,
                "nearby_pain": [],
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "put_call_oi_ratio": (total_put_oi / total_call_oi)
                if total_call_oi
                else None,
            }

        max_pain_strike = max(pains, key=lambda k: pains[k])
        sorted_strikes = sorted(pains.keys())
        idx = (
            sorted_strikes.index(max_pain_strike)
            if max_pain_strike in sorted_strikes
            else 0
        )
        nearby_start = max(0, idx - 5)
        nearby_end = min(len(sorted_strikes), idx + 6)
        nearby = [
            {
                "strike": float(sorted_strikes[i]),
                "pain": float(pains[sorted_strikes[i]]),
            }
            for i in range(nearby_start, nearby_end)
        ]
        return {
            "currency": currency,
            "spot_price": float(spot),
            "max_pain_strike": float(max_pain_strike),
            "max_pain_value": float(pains[max_pain_strike]),
            "nearby_pain": nearby,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "put_call_oi_ratio": (
                total_put_oi / total_call_oi if total_call_oi else None
            ),
        }

    def _calc_liquidity_score(self, spread_pct: float, open_interest: float) -> float:
        """计算流动性评分 (0-100)。

        基于 bid-ask spread 和 open_interest 计算。
        - spread 越低越好 (≤2% = 满分 50)
        - open_interest 越高越好 (≥1000 = 满分 50)
        """
        # spread 评分 (0-50)
        if spread_pct <= 2:
            spread_score = 50
        elif spread_pct <= 5:
            spread_score = 40
        elif spread_pct <= 10:
            spread_score = 25
        elif spread_pct <= 20:
            spread_score = 10
        else:
            spread_score = 0

        # open_interest 评分 (0-50)
        if open_interest >= 1000:
            oi_score = 50
        elif open_interest >= 500:
            oi_score = 40
        elif open_interest >= 200:
            oi_score = 30
        elif open_interest >= 100:
            oi_score = 20
        else:
            oi_score = 10

        return spread_score + oi_score

    def _fetch_order_books_bulk(
        self, instrument_names: list[str], max_workers: int = 8
    ) -> dict[str, dict[str, Any]]:
        """批量获取订单簿，自动处理缓存。"""
        results: dict[str, dict[str, Any]] = {}
        now_ms = self._now_ms()

        # 检查哪些需要重新获取
        pending = []
        for name in instrument_names:
            cached = self._order_book_cache.get(name)
            if cached is None:
                pending.append(name)
            else:
                cached_ts = cached.get("ts", 0)
                if now_ms - cached_ts >= self._cache_ttl_seconds * 1000:
                    pending.append(name)
                else:
                    results[name] = cached.get("data", {})

        # 并行获取需要更新的
        if pending:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(self._get_order_book, name): name
                    for name in pending
                }
                for future in as_completed(future_map):
                    name = future_map[future]
                    try:
                        results[name] = future.result()
                    except Exception:
                        results[name] = {}

        # 填充剩余的（从缓存）
        for name in instrument_names:
            if name not in results:
                cached = self._order_book_cache.get(name)
                results[name] = cached.get("data", {}) if cached else {}

        return results

    def _store_option_snapshots(self, rows: list[dict[str, Any]]) -> None:
        ts = self._now_ms()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO option_snapshots
                (ts, instrument_name, strike, expiry_ts, dte, delta, mark_iv, mark_price, underlying_price, apr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ts,
                        row["instrument_name"],
                        row["strike"],
                        row["expiry_ts"],
                        row["dte"],
                        row["delta"],
                        row["mark_iv"],
                        row["mark_price"],
                        row["underlying_price"],
                        row["apr"],
                    )
                    for row in rows
                ],
            )

    def _store_large_trade_events(self, trades: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO large_trade_events
                (ts, trade_id, instrument_name, direction, amount, index_price, underlying_notional_usd, premium_usd, flow_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["timestamp"],
                        item["trade_id"],
                        item["instrument_name"],
                        item["direction"],
                        item["amount"],
                        item["index_price"],
                        item["underlying_notional_usd"],
                        item["premium_usd"],
                        item["flow_label"],
                    )
                    for item in trades
                ],
            )

    def get_large_trade_alerts(
        self,
        currency: str = "BTC",
        min_usd_value: float = 500000,
        lookback_minutes: int = 60,
    ) -> dict[str, Any]:
        currency = self._normalize_currency(currency)
        cutoff = self._now_ms() - lookback_minutes * 60 * 1000
        trades = [
            item
            for item in self._get_last_trades(currency)
            if int(item.get("timestamp", 0)) >= cutoff
        ]
        instrument_names = sorted({item["instrument_name"] for item in trades})
        order_books = self._fetch_order_books_bulk(instrument_names)

        enriched: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []

        for trade in trades:
            meta = self._parse_instrument_name(trade["instrument_name"])
            underlying_notional = float(trade["amount"]) * float(trade["index_price"])
            if underlying_notional < min_usd_value:
                continue

            book = order_books.get(trade["instrument_name"], {})
            greeks = book.get("greeks") or {}
            delta = float(greeks.get("delta") or 0.0)
            gamma = float(greeks.get("gamma") or 0.0)
            vega = float(greeks.get("vega") or 0.0)
            mark_iv = float(book.get("mark_iv") or trade.get("iv") or 0.0)
            premium_usd = (
                float(trade["price"])
                * float(trade["amount"])
                * float(trade["index_price"])
            )

            # 改进流向标签逻辑，区分 Put 和 Call
            if meta.option_type == "put":
                # Put 期权：买入 = 保护性对冲/看跌，卖出 = 收取权利金
                abs_delta = abs(delta)
                if trade["direction"] == "buy":
                    if 0.10 <= abs_delta <= 0.35 and 7 <= meta.dte <= 60:
                        flow_label = "protective_hedge"
                    else:
                        flow_label = "speculative_put"  # 其他买入 Put 都是投机
                elif trade["direction"] == "sell":
                    if abs_delta <= 0.35:
                        flow_label = "premium_collect"
                    else:
                        flow_label = "speculative_put"  # 高 Delta 卖出也是投机
                else:
                    flow_label = "unknown"
            else:
                # Call 期权：买入 = 追涨/投机，卖出 = 备兑/看跌
                abs_delta = abs(delta)
                if trade["direction"] == "buy":
                    if abs_delta >= 0.30:
                        flow_label = (
                            "call_momentum"  # 买入高 Delta Call 可能预示看涨/机构建仓
                        )
                    else:
                        flow_label = "call_speculative"  # 买入低 Delta Call 是投机
                elif trade["direction"] == "sell":
                    if abs_delta <= 0.40:
                        flow_label = "covered_call"  # 卖出虚值 Call 可能是备兑
                    else:
                        flow_label = "call_overwrite"  # 卖出高 Delta Call 可能是改仓
                else:
                    flow_label = "unknown"

            severity = self._severity_from_notional(underlying_notional)
            item = {
                "timestamp": int(trade["timestamp"]),
                "trade_id": trade["trade_id"],
                "instrument_name": trade["instrument_name"],
                "direction": trade["direction"],
                "strike": meta.strike,
                "expiry": meta.expiry_dt.isoformat(),
                "expiry_ts": meta.expiry_ts,
                "dte": meta.dte,
                "delta": round(delta, 4),
                "gamma": round(gamma, 6),
                "vega": round(vega, 4),
                "mark_iv": round(mark_iv, 2),
                "index_price": float(trade["index_price"]),
                "amount": float(trade["amount"]),
                "underlying_notional_usd": round(underlying_notional, 2),
                "premium_usd": round(premium_usd, 2),
                "flow_label": flow_label,
                "severity": severity,
            }
            enriched.append(item)
            alerts.append(
                {
                    "type": "block_trade",
                    "severity": severity,
                    "title": f"{currency} {meta.option_type.upper()}期权大宗成交 {self._format_usd(underlying_notional)}",
                    "message": (
                        f"{trade['direction']} {trade['instrument_name']}，名义金额 {self._format_usd(underlying_notional)}，"
                        f"Delta {delta:.2f}，判断为 {flow_label}。"
                    ),
                }
            )

        enriched.sort(key=lambda item: item["underlying_notional_usd"], reverse=True)
        alerts.sort(
            key=lambda item: {"high": 3, "medium": 2, "info": 1}[item["severity"]],
            reverse=True,
        )
        if enriched:
            self._store_large_trade_events(enriched)
        return {
            "currency": currency,
            "lookback_minutes": lookback_minutes,
            "min_usd_value": min_usd_value,
            "count": len(enriched),
            "trades": enriched,
            "alerts": alerts,
        }

    def _get_sell_contract_recommendations(
        self,
        currency: str,
        option_type: str,
        max_delta: float,
        min_apr: float,
        min_dte: int,
        max_dte: int,
        top_k: int,
        max_spread_pct: float,
        min_open_interest: float,
    ) -> dict[str, Any]:
        """通用方法：获取 Sell 期权推荐（Put 或 Call），带流动性过滤。

        改进点：
        1. bid-ask spread 过滤（默认 ≤10%）
        2. 最低 open_interest 要求（默认 ≥100）
        3. 返回流动性指标
        4. 支持 Put 和 Call 两种类型
        """
        currency = self._normalize_currency(currency)
        summaries = self._get_book_summaries(currency)
        candidates: list[dict[str, Any]] = []

        suffix = "-P" if option_type == "put" else "-C"

        # 第一轮筛选：基本条件
        for summary in summaries:
            instrument_name = summary.get("instrument_name", "")
            if not instrument_name.endswith(suffix):
                continue
            meta = self._parse_instrument_name(instrument_name)
            if not (min_dte <= meta.dte <= max_dte):
                continue
            mark_price = float(summary.get("mark_price") or 0.0)
            open_interest = float(summary.get("open_interest") or 0.0)
            bid_price = float(summary.get("bid_price") or 0.0)
            ask_price = float(summary.get("ask_price") or 0.0)

            # 流动性过滤
            if mark_price <= 0 or open_interest <= 0:
                continue
            if open_interest < min_open_interest:
                continue

            # bid-ask spread 计算
            spread_pct = 0.0
            if bid_price > 0 and ask_price > 0:
                spread_pct = (ask_price - bid_price) / ask_price * 100
            if spread_pct > max_spread_pct:
                continue

            candidates.append(
                {
                    "instrument_name": instrument_name,
                    "strike": meta.strike,
                    "expiry": meta.expiry_dt.isoformat(),
                    "expiry_ts": meta.expiry_ts,
                    "dte": meta.dte,
                    "bid_price": bid_price,
                    "ask_price": ask_price,
                    "spread_pct": round(spread_pct, 2),
                    "open_interest": open_interest,
                }
            )

        order_books = self._fetch_order_books_bulk(
            [item["instrument_name"] for item in candidates]
        )

        picks: list[dict[str, Any]] = []
        filtered_count = 0  # 记录因流动性被过滤的数量

        for item in candidates:
            book = order_books.get(item["instrument_name"], {})
            greeks = book.get("greeks") or {}
            delta = float(greeks.get("delta") or 0.0)
            mark_price = float(book.get("mark_price") or 0.0)
            underlying_price = float(book.get("underlying_price") or 0.0)
            mark_iv = float(book.get("mark_iv") or 0.0)
            open_interest = float(book.get("open_interest") or 0.0)

            if mark_price <= 0 or underlying_price <= 0 or open_interest <= 0:
                filtered_count += 1
                continue

            # Delta 检查：Put delta 为负，Call delta 为正
            if option_type == "put":
                if abs(delta) > max_delta:
                    continue
            else:  # call
                if delta > max_delta:
                    continue

            if open_interest < min_open_interest:
                filtered_count += 1
                continue

            # 再次检查 spread（使用 order_book 数据更准确）
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            # bids/asks 格式是 [[price, quantity], ...]
            if (
                bids
                and asks
                and isinstance(bids[0], list)
                and isinstance(asks[0], list)
            ):
                bid_px = float(bids[0][0])
                ask_px = float(asks[0][0])
                if bid_px > 0 and ask_px > 0:
                    spread_pct = (ask_px - bid_px) / ask_px * 100
                    if spread_pct > max_spread_pct:
                        filtered_count += 1
                        continue

            premium_usd = mark_price * underlying_price
            apr = premium_usd / item["strike"] * (365 / item["dte"]) * 100
            if apr < min_apr:
                continue

            # 计算 breakeven：Put = strike - premium，Call = strike + premium
            if option_type == "put":
                breakeven = item["strike"] - premium_usd
            else:
                breakeven = item["strike"] + premium_usd

            pick = {
                "instrument_name": item["instrument_name"],
                "strike": round(item["strike"], 2),
                "expiry": item["expiry"],
                "expiry_ts": item["expiry_ts"],
                "dte": item["dte"],
                "delta": round(delta, 4),
                "mark_iv": round(mark_iv, 2),
                "mark_price": round(mark_price, 6),
                "underlying_price": round(underlying_price, 2),
                "premium_usd": round(premium_usd, 2),
                "apr": round(apr, 2),
                "breakeven": round(breakeven, 2),
                "risk_emoji": self._risk_emoji(abs(delta)),
                "open_interest": round(open_interest, 2),
                "spread_pct": item.get("spread_pct", 0),
                # 流动性评级
                "liquidity_score": self._calc_liquidity_score(
                    item.get("spread_pct", 0), open_interest
                ),
            }
            picks.append(pick)

        # 按流动性评分和 APR 综合排序
        picks.sort(key=lambda row: (row["liquidity_score"], row["apr"]), reverse=True)
        final_rows = picks[:top_k]

        if final_rows:
            self._store_option_snapshots(final_rows)

        return {
            "currency": currency,
            "max_delta": max_delta,
            "min_apr": min_apr,
            "min_dte": min_dte,
            "max_dte": max_dte,
            "max_spread_pct": max_spread_pct,
            "min_open_interest": min_open_interest,
            "filtered_count": filtered_count,
            "count": len(final_rows),
            "contracts": final_rows,
        }

    def get_sell_put_recommendations(
        self,
        currency: str = "BTC",
        max_delta: float = 0.25,
        min_apr: float = 15.0,
        min_dte: int = 7,
        max_dte: int = 45,
        top_k: int = 5,
        # 流动性参数
        max_spread_pct: float = 10.0,  # bid-ask spread 最大百分比
        min_open_interest: float = 100.0,  # 最小未平仓合约数
    ) -> dict[str, Any]:
        """获取 Sell Put 推荐，带流动性过滤。

        改进点：
        1. bid-ask spread 过滤（默认 ≤10%）
        2. 最低 open_interest 要求（默认 ≥100）
        3. 返回流动性指标
        """
        return self._get_sell_contract_recommendations(
            currency=currency,
            option_type="put",
            max_delta=max_delta,
            min_apr=min_apr,
            min_dte=min_dte,
            max_dte=max_dte,
            top_k=top_k,
            max_spread_pct=max_spread_pct,
            min_open_interest=min_open_interest,
        )

    def get_sell_call_recommendations(
        self,
        currency: str = "BTC",
        max_delta: float = 0.20,
        min_apr: float = 15.0,
        min_dte: int = 7,
        max_dte: int = 45,
        top_k: int = 5,
        # 流动性参数
        max_spread_pct: float = 10.0,
        min_open_interest: float = 100.0,
    ) -> dict[str, Any]:
        """获取 Sell Call 推荐，带流动性过滤。

        默认参数比 Sell Put 更保守：
        - max_delta = 0.20 (Sell Put 默认 0.25)
        - 其他参数相同
        ⚠️  注意：Sell Call 理论上存在无限亏损风险，仅适合备兑开仓。
        """
        return self._get_sell_contract_recommendations(
            currency=currency,
            option_type="call",
            max_delta=max_delta,
            min_apr=min_apr,
            min_dte=min_dte,
            max_dte=max_dte,
            top_k=top_k,
            max_spread_pct=max_spread_pct,
            min_open_interest=min_open_interest,
        )

    def _build_dvol_alerts(self, dvol: dict[str, Any]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        z_score = dvol.get("z_score_7d")
        percentile_7d = dvol.get("iv_percentile_7d")
        if isinstance(z_score, (int, float)) and z_score > 2:
            alerts.append(
                {
                    "type": "dvol_spike",
                    "severity": "high",
                    "title": "DVOL 异常波动预警",
                    "message": f"BTC DVOL 7日 z-score 达到 {z_score:.2f}，权利金显著昂贵，偏卖波动率环境。",
                }
            )
        elif isinstance(percentile_7d, (int, float)) and percentile_7d >= 80:
            alerts.append(
                {
                    "type": "dvol_spike",
                    "severity": "medium",
                    "title": "DVOL 高位提醒",
                    "message": f"BTC DVOL 7日分位处于 {percentile_7d:.2f}%，可优先关注保守型 Sell Put。",
                }
            )
        return alerts

    def _build_sell_put_alert(
        self, sell_put: dict[str, Any], dvol: dict[str, Any]
    ) -> list[dict[str, Any]]:
        contracts = sell_put.get("contracts") or []
        if not contracts:
            return []
        top = contracts[0]
        severity = "medium" if (dvol.get("iv_percentile_7d") or 0) >= 80 else "info"
        return [
            {
                "type": "sell_put_opportunity",
                "severity": severity,
                "title": "Sell Put 机会",
                "message": (
                    f"发现 {len(contracts)} 个 {sell_put.get('currency', 'BTC')} Sell Put 候选，"
                    f"首选 {top['instrument_name']}，APR {top['apr']:.2f}%，Delta {top['delta']:.2f}。"
                ),
            }
        ]

    def _flow_label_to_cn(self, label: str) -> str:
        """将流向标签翻译为中文"""
        mapping = {
            "protective_hedge": "保护性对冲",
            "premium_collect": "收取权利金",
            "speculative_put": "投机卖Put",
            "call_momentum": "Call追涨",
            "covered_call": "备兑卖Call",
            "call_overwrite": "Call改仓",
            "call_speculative": "投机买Call",
            "unknown": "未知",
        }
        return mapping.get(label, label)

    def _generate_interpretation(
        self, dvol: dict, large_trades: list, currency: str
    ) -> str:
        """生成人性化的市场解读"""
        current = dvol.get("current_dvol", 0)
        z_score = dvol.get("z_score_7d", 0)
        percentile_7d = dvol.get("iv_percentile_7d", 0)
        percentile_24h = dvol.get("iv_percentile_24h", 0)
        trend = dvol.get("trend", "平稳")
        signal = dvol.get("signal", "中性")
        trend_change = dvol.get("trend_change", 0)

        lines = []

        # DVOL 变化趋势解读
        if trend_change > 0:
            lines.append(
                f"• DVOL 从昨天的 {current - trend_change:.2f} 小幅上升到 {current:.2f}，波动率继续回升，恐慌情绪在升温"
            )
        elif trend_change < 0:
            lines.append(
                f"• DVOL 从昨天的 {current - trend_change:.2f} 小幅下降到 {current:.2f}，波动率有所回落，恐慌情绪有所缓解"
            )
        else:
            lines.append(f"• DVOL 当前为 {current:.2f}，波动率走势平稳")

        # 分位数解读
        if percentile_7d >= 80:
            percentile_desc = "处于高位"
        elif percentile_7d <= 20:
            percentile_desc = "处于低位"
        else:
            percentile_desc = "处于中性区间"

        # Z-Score 解读
        if abs(z_score) >= 2:
            zscore_desc = "属于极端信号"
        elif abs(z_score) >= 1:
            zscore_desc = "偏离正常波动区间"
        else:
            zscore_desc = "不算极端，属于正常范围"

        lines.append(
            f"• 目前处于 7 日 {percentile_7d:.0f}% 分位，Z-Score {z_score:+.2f}，{zscore_desc}"
        )

        # 趋势解读
        if trend == "上涨":
            lines.append("• 波动率呈上升趋势，符合市场反弹后恐慌还在延续的判断")
        elif trend == "下跌":
            lines.append("• 波动率呈下降趋势，市场情绪有所恢复")
        else:
            lines.append("• 波动率走势平稳")

        return "\n".join(lines)

    def _generate_strategy(self, dvol: dict, sell_put: list, large_trades: list) -> str:
        """生成策略建议"""
        signal = dvol.get("signal", "中性")
        percentile_7d = dvol.get("iv_percentile_7d", 50)
        sentiment = "中性"
        trend = dvol.get("trend", "平稳")
        if large_trades:
            analysis = self._analyze_large_trades(large_trades)
            sentiment = analysis.get("sentiment", "中性")

        lines = []

        # 基于 DVOL 的策略
        if signal in {"高波动率", "异常波动"}:
            lines.append(
                "• 波动率回升，意味着期权权利金价格上涨，现在卖 Put 收租比低位时性价比更高了"
            )
            lines.append("• 但波动率还没到极端高位，不适合买波动率赌大波动")
        elif signal == "低波动率":
            lines.append("• 当前波动率处于低位，权利金偏薄，卖 Put 赔率不够友好")
            lines.append("• 可以考虑做买方策略(如买 Call/Put)或等待波动率回升")
        else:
            lines.append("• 波动率处于中性区间，期权权利金价格相对合理")

        # 基于趋势的策略
        if trend == "上涨":
            lines.append("• 当前市场处于反弹阶段，波动率仍在升温，适合卖 Put 接货")
        elif trend == "下跌":
            lines.append("• 市场处于下跌后反弹阶段，波动率缓慢回落，可以分批建仓")
        else:
            lines.append("• 市场方向不明，适合观望或轻仓操作")

        # 推荐合约
        if sell_put:
            best = sell_put[0]
            lines.append(
                f"• 当前市场方向向下后反弹，期权策略适合卖 Put 接货，在{best['strike']:.0f}价位卖 Put 收权利金"
            )
        else:
            lines.append("• 当前未筛出满足条件的 Sell Put 合约，建议等待更好机会")

        return "\n".join(lines)

    def _generate_summary(self, dvol: dict, large_trades: list, sell_put: list) -> str:
        """生成市场总结"""
        signal = dvol.get("signal", "中性")
        percentile_7d = dvol.get("iv_percentile_7d", 50)
        trend = dvol.get("trend", "平稳")

        # 确定市场状态
        if signal in {"高波动率", "异常波动"} and percentile_7d >= 80:
            status = "高波动率环境"
        elif signal == "低波动率" and percentile_7d <= 20:
            status = "低波动率环境"
        else:
            status = "中性偏高" if percentile_7d > 50 else "中性偏低"

        # 趋势总结
        if trend == "上涨":
            trend_summary = "波动率上升阶段"
        elif trend == "下跌":
            trend_summary = "波动率下降阶段"
        else:
            trend_summary = "波动率平稳阶段"

        # 市场阶段描述
        if trend == "上涨":
            phase = '"上涨反弹"阶段'
        elif trend == "下跌":
            phase = '"下跌后反弹"阶段'
        else:
            phase = '"震荡"阶段'

        # 总结
        if signal in {"高波动率", "异常波动"}:
            summary = f"波动率处于高位，权利金偏贵，没有极端信号，当前市场处于{phase}，{trend_summary}，符合预期节奏。"
        elif signal == "低波动率":
            summary = (
                f"波动率处于低位，权利金偏便宜，当前市场处于{phase}，{trend_summary}。"
            )
        else:
            summary = f"波动率{status}，没有极端信号，当前市场处于{phase}，{trend_summary}，符合预期节奏。"

        return summary

    def _generate_risk_tips(self, dvol: dict) -> str:
        """生成风险提示"""
        signal = dvol.get("signal", "中性")
        percentile_7d = dvol.get("iv_percentile_7d", 50)

        if signal == "异常波动":
            return "波动率显著偏离均值，Sell Put 收益高但需控制尾部风险，建议仓位不超过总资金的 10%。"
        elif signal == "高波动率":
            return "当前处于高波动率环境，权利金较贵但需警惕回落风险，建议选择 Delta ≤0.20 的保守合约。"
        elif signal == "低波动率":
            return "权利金偏薄，卖 Put 赔率不够友好，建议减少卖方策略仓位。"
        else:
            return "当前环境可做筛选式收租，但不宜过度追求高 Delta，保持适当仓位分散。"

    def _analyze_large_trades(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """分析大宗异动，返回市场解读"""
        if not trades:
            return {
                "sentiment": "中性",
                "total_notional": 0,
                "call_count": 0,
                "put_count": 0,
                "hedge_count": 0,
                "premium_count": 0,
                "call_momentum_count": 0,
                "notable_contracts": [],
            }

        total_notional = sum(t.get("underlying_notional_usd", 0) for t in trades)

        # 分类统计
        call_count = sum(1 for t in trades if "call" in t.get("flow_label", "").lower())
        put_count = sum(1 for t in trades if "put" in t.get("flow_label", "").lower())
        hedge_count = sum(1 for t in trades if t.get("flow_label") in HEDGE_LABELS)
        premium_count = sum(1 for t in trades if t.get("flow_label") in PREMIUM_LABELS)
        call_momentum_count = sum(
            1 for t in trades if t.get("flow_label") == "call_momentum"
        )

        # 重点合约（高Delta Call 或 对冲类）
        notable = []
        for t in trades:
            label = t.get("flow_label", "")
            if label in ("call_momentum", "protective_hedge"):
                notable.append(
                    {
                        "instrument_name": t["instrument_name"],
                        "direction": t["direction"],
                        "notional": t.get("underlying_notional_usd", 0),
                        "label_cn": self._flow_label_to_cn(label),
                    }
                )

        # 重点合约按名义金额排序
        notable.sort(key=lambda x: x["notional"], reverse=True)

        # 市场情绪判断
        sentiment = "中性"
        call_buy_notional = sum(
            t.get("underlying_notional_usd", 0)
            for t in trades
            if t.get("flow_label") == "call_momentum" and t.get("direction") == "buy"
        )
        put_buy_notional = sum(
            t.get("underlying_notional_usd", 0)
            for t in trades
            if t.get("flow_label") == "protective_hedge" and t.get("direction") == "buy"
        )

        # 买入Call Momentum + 保护性对冲 的总名义
        bullish_notional = call_buy_notional + put_buy_notional

        # 卖出权利金的总名义
        bearish_notional = sum(
            t.get("underlying_notional_usd", 0)
            for t in trades
            if t.get("flow_label") in ("premium_collect", "covered_call")
        )

        if bullish_notional > bearish_notional * 1.5 and bullish_notional > 1_000_000:
            sentiment = "看涨"
        elif bearish_notional > bullish_notional * 1.5 and bearish_notional > 1_000_000:
            sentiment = "看跌"

        return {
            "sentiment": sentiment,
            "total_notional": total_notional,
            "call_count": call_count,
            "put_count": put_count,
            "hedge_count": hedge_count,
            "premium_count": premium_count,
            "call_momentum_count": call_momentum_count,
            "notable_contracts": notable,
            "bullish_notional": bullish_notional,
            "bearish_notional": bearish_notional,
        }

    def _build_alert_text(self, scan: dict[str, Any]) -> str:
        parts: list[str] = []
        dvol = scan["dvol"]
        rv_iv = scan.get("rv_iv", {})

        # DVOL + RV/IV 信息
        if rv_iv.get("rv_iv_ratio") is not None:
            dvol_val = dvol.get("current_dvol")
            rv30_val = rv_iv.get("rv_30d")
            rv_ratio_val = rv_iv.get("rv_iv_ratio")
            parts.append(
                f"DVOL {f'{dvol_val:.2f}' if dvol_val is not None else 'N/A'} | "
                f"RV30d {f'{rv30_val * 100:.1f}%' if rv30_val is not None else 'N/A'} | "
                f"RV/IV {f'{rv_ratio_val:.2f}' if rv_ratio_val is not None else 'N/A'} "
                f"{rv_iv.get('signal', '')}"
            )
        else:
            dvol_val = dvol.get("current_dvol")
            parts.append(
                f"DVOL {f'{dvol_val:.2f}' if dvol_val is not None else 'N/A'}，"
                f"7日分位 {self._format_pct(dvol.get('iv_percentile_7d'))}，"
                f"信号 {dvol.get('signal', 'N/A')}"
            )

        sell_put = scan.get("sell_put") or []
        if sell_put:
            top = sell_put[0]
            parts.append(
                f"Top Sell Put: {top['instrument_name']}，APR {top['apr']:.2f}% / Delta {top['delta']:.2f}"
            )

        sell_call = scan.get("sell_call") or []
        if sell_call:
            top = sell_call[0]
            parts.append(
                f"Top Sell Call: {top['instrument_name']}，APR {top['apr']:.2f}% / Delta {top['delta']:.2f}"
            )

        large_trades = scan.get("large_trades") or []
        if large_trades:
            top_flow = large_trades[0]
            # 统计各类流向
            hedge_count = sum(
                1 for item in large_trades if item["flow_label"] in HEDGE_LABELS
            )
            premium_count = sum(
                1 for item in large_trades if item["flow_label"] in PREMIUM_LABELS
            )
            call_count = sum(1 for item in large_trades if "call" in item["flow_label"])

            flow_summary = (
                f"近{len(large_trades)}笔大宗成交，最大单 {top_flow['instrument_name']} "
                f"{self._format_usd(top_flow['underlying_notional_usd'])}"
            )
            if call_count:
                flow_summary += f"，其中 Call {call_count} 笔"
            if hedge_count:
                flow_summary += f"，疑似对冲 {hedge_count} 笔"
            if premium_count:
                flow_summary += f"，权利金卖出 {premium_count} 笔"
            parts.append(flow_summary)

        return " | ".join(parts)

    def run_scan(
        self,
        currency: str = "BTC",
        min_usd_value: float = 500000,
        lookback_minutes: int = 60,
        max_delta: float = 0.25,
        min_apr: float = 15.0,
        min_dte: int = 7,
        max_dte: int = 45,
        top_k: int = 5,
        timeout_seconds: float = 120.0,
        # 流动性参数
        max_spread_pct: float = 10.0,
        min_open_interest: float = 100.0,
        # Sell Call 独立参数 - 默认更保守
        max_delta_sell_call: float = 0.20,
        min_apr_sell_call: float = 15.0,
        # 新增缓存与 RV 指标
        use_cache: bool = False,
        cache_ttl: int = 300,
    ) -> dict[str, Any]:
        """运行完整扫描，支持超时和降级。"""
        currency = self._normalize_currency(currency)

        # 清理过期缓存，防止无限增长
        self._clean_expired_cache()

        # 默认降级结果
        default_dvol = {
            "currency": currency,
            "current_dvol": None,
            "error": "获取失败",
        }
        default_large = {"trades": [], "alerts": [], "count": 0, "error": "获取失败"}
        default_sell_put = {"contracts": [], "count": 0, "error": "获取失败"}
        default_sell_call = {"contracts": [], "count": 0, "error": "获取失败"}
        default_rv = {
            "currency": currency,
            "rv_7d": None,
            "rv_30d": None,
            "rv_90d": None,
            "current_price": None,
            "price_change_7d_pct": None,
            "data_points": 0,
        }
        default_max_pain = {
            "currency": currency,
            "spot_price": None,
            "max_pain_strike": None,
            "max_pain_value": None,
            "nearby_pain": [],
            "total_call_oi": 0,
            "total_put_oi": 0,
            "put_call_oi_ratio": None,
        }
        # rv_iv is computed after dvol and rv are fetched, no need for separate future
        rv_iv = {
            "rv_7d": None,
            "rv_30d": None,
            "rv_90d": None,
            "iv_current": None,
            "rv_iv_ratio": None,
            "signal": "合理区间",
            "confidence": 50,
            "recommendation": "保持关注，观察 DVOL 与 RV 的对比",
            "bottom_fishing": False,
        }

        dvol, large, sell_put, sell_call, rv, max_pain = (
            default_dvol,
            default_large,
            default_sell_put,
            default_sell_call,
            default_rv,
            default_max_pain,
        )
        errors: list[str] = []

        max_workers = 6
        # 尝试从缓存读取
        if use_cache:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT scan_data, scan_ts, ttl_seconds FROM scan_cache WHERE currency = ?",
                    (currency,),
                ).fetchone()
                if row:
                    ts = int(row["scan_ts"] or 0)
                    ttl = int(row["ttl_seconds"] or cache_ttl)
                    if self._now_ms() - ts <= ttl * 1000:
                        try:
                            return json.loads(row["scan_data"])
                        except Exception:
                            pass

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_dvol = executor.submit(self.get_dvol_signal, currency)
            future_large = executor.submit(
                self.get_large_trade_alerts,
                currency,
                min_usd_value,
                lookback_minutes,
            )
            future_sell_put = executor.submit(
                self.get_sell_put_recommendations,
                currency,
                max_delta,
                min_apr,
                min_dte,
                max_dte,
                top_k,
                max_spread_pct,
                min_open_interest,
            )
            future_sell_call = executor.submit(
                self.get_sell_call_recommendations,
                currency,
                max_delta_sell_call,
                min_apr_sell_call,
                min_dte,
                max_dte,
                top_k,
                max_spread_pct,
                min_open_interest,
            )
            future_rv = executor.submit(self.get_rv_signal, currency)
            future_max_pain = executor.submit(self.get_max_pain, currency)

            # 带超时的结果获取
            for name, future in [
                ("DVOL", future_dvol),
                ("大宗交易", future_large),
                ("Sell Put", future_sell_put),
                ("Sell Call", future_sell_call),
                ("RV", future_rv),
                ("MaxPain", future_max_pain),
            ]:
                try:
                    result = future.result(timeout=timeout_seconds)
                    if name == "DVOL":
                        dvol = result
                    elif name == "大宗交易":
                        large = result
                    elif name == "Sell Put":
                        sell_put = result
                    elif name == "Sell Call":
                        sell_call = result
                    elif name == "RV":
                        rv = result
                    elif name == "RV_IV":
                        rv_iv = result
                    elif name == "MaxPain":
                        max_pain = result
                except Exception as e:
                    errors.append(f"{name}: {str(e)}")
                    # 保留默认值

        alerts: list[dict[str, Any]] = []
        alerts.extend(self._build_dvol_alerts(dvol))
        if isinstance(large, dict):
            alerts.extend(large.get("alerts", []))
        alerts.extend(self._build_sell_put_alert(sell_put, dvol))
        severity_rank = {"high": 3, "medium": 2, "info": 1}
        alerts.sort(
            key=lambda item: severity_rank.get(item["severity"], 0), reverse=True
        )

        # 如果有错误，添加错误告警
        if errors:
            alerts.append(
                {
                    "type": "system_error",
                    "severity": "medium",
                    "title": "部分数据获取失败",
                    "message": "; ".join(errors),
                }
            )

        # 计算 RV/IV 信号使用已获取的结果，避免重复请求
        try:
            rv_iv = self.get_rv_iv_signal(currency, dvol_signal=dvol, rv_signal=rv)
        except Exception as e:
            errors.append(f"RV_IV: {str(e)}")

        # 获取现货价格
        spot_info = self._get_spot_price(currency)

        # 组装结果
        result = {
            "scan_ts": self._utc_now().isoformat(),
            "currency": currency,
            "spot_price": spot_info.get("spot_price"),
            "dvol": dvol,
            "large_trades": large.get("trades", []) if isinstance(large, dict) else [],
            "sell_put": sell_put.get("contracts", [])
            if isinstance(sell_put, dict)
            else [],
            "sell_call": sell_call.get("contracts", [])
            if isinstance(sell_call, dict)
            else [],
            "rv": rv if isinstance(rv, dict) else {},
            "rv_iv": rv_iv if isinstance(rv_iv, dict) else {},
            "max_pain": max_pain if isinstance(max_pain, dict) else {},
            "alerts": alerts,
            "errors": errors if errors else None,
            "position_risk": {
                "status": "not_configured",
                "message": "v1 未接入私有持仓，跳过 Gamma/Delta 风险检查",
            },
        }

        result["alert_text"] = self._build_alert_text(result)
        result["report_text"] = self.render_report(mode="report", scan_data=result)

        # 缓存结果
        if use_cache:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO scan_cache (currency, scan_ts, scan_data, ttl_seconds) VALUES (?, ?, ?, ?)",
                    (
                        currency,
                        int(self._now_ms()),
                        json.dumps(result),
                        int(cache_ttl),
                    ),
                )

        return result

    def render_report(
        self,
        mode: str = "report",
        scan_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        scan = scan_data or self.run_scan(**kwargs)
        if mode == "json":
            return json.dumps(scan, ensure_ascii=False, indent=2)
        if mode == "alert":
            return scan.get("alert_text") or self._build_alert_text(scan)

        dvol = scan["dvol"]
        sell_put = scan["sell_put"]
        large_trades = scan["large_trades"]
        currency = scan.get("currency", "BTC")

        # 生成市场结论
        thesis_parts = []
        if dvol["signal"] in {"异常波动", "高波动率"}:
            thesis_parts.append("当前权利金整体偏贵")
        elif dvol["signal"] == "低波动率":
            thesis_parts.append("当前权利金整体偏便宜")
        else:
            thesis_parts.append("当前期权环境中性")
        if large_trades:
            top_flow = large_trades[0]["flow_label"]
            thesis_parts.append(
                f"近一小时出现 {len(large_trades)} 笔大宗成交，主导标签为 {top_flow}"
            )
        if sell_put:
            thesis_parts.append(f"Sell Put 已筛出 {len(sell_put)} 个高 APR 候选")
        market_conclusion = "；".join(thesis_parts) + "。"

        # 生成解读
        interpretation = self._generate_interpretation(dvol, large_trades, currency)

        # 生成策略建议
        strategy = self._generate_strategy(dvol, sell_put, large_trades)

        # 生成总结
        summary = self._generate_summary(dvol, large_trades, sell_put)

        lines = [
            f"Deribit {currency} 期权分析师报告",
            f"生成时间：{scan['scan_ts']}",
            f"{currency} 现货价格：${scan.get('spot_price', 'N/A'):,.2f}"
            if scan.get("spot_price")
            else "",
            "",
            "1. 市场结论",
            market_conclusion,
            "",
            "2. DVOL 健康度",
            (
                f"- 当前 DVOL：{dvol['current_dvol']:.2f} | 7d z-score：{dvol.get('z_score_7d', 'N/A')} | "
                f"趋势：{dvol.get('trend', 'N/A')}"
            ),
            (
                f"- 7d 分位：{self._format_pct(dvol.get('iv_percentile_7d'))} | 24h 分位：{self._format_pct(dvol.get('iv_percentile_24h'))} | "
                f"置信度：{dvol.get('confidence', 'N/A')}%"
            ),
            f"- 动态阈值：高信={dvol.get('dynamic_thresholds', {}).get('high_conf', 'N/A')} | 中信={dvol.get('dynamic_thresholds', {}).get('mid_conf', 'N/A')}",
            f"- 信号：{dvol['signal']}；建议：{dvol['recommendation']}",
        ]

        # 新增：RV 实现波动率分析
        rv_iv = scan.get("rv_iv", {})
        if rv_iv:
            lines.extend(["", "3. RV 实现波动率分析"])
            rv_7d = rv_iv.get("rv_7d")
            rv_30d = rv_iv.get("rv_30d")
            rv_90d = rv_iv.get("rv_90d")
            iv_current = rv_iv.get("iv_current")
            rv_ratio = rv_iv.get("rv_iv_ratio")

            if rv_7d is not None:
                lines.append(f"- 7d  已实现波动率 (RV): {rv_7d * 100:.1f}%")
            if rv_30d is not None:
                lines.append(f"- 30d 已实现波动率 (RV): {rv_30d * 100:.1f}%")
            if rv_90d is not None:
                lines.append(f"- 90d 已实现波动率 (RV): {rv_90d * 100:.1f}%")
            if iv_current is not None:
                lines.append(f"- 当前隐含波动率 (DVOL): {iv_current:.2f}")
            if rv_ratio is not None:
                lines.append(f"- RV/IV 比值: {rv_ratio:.2f}")
                lines.append(
                    f"- 信号: {rv_iv.get('signal')}；置信度: {rv_iv.get('confidence')}%"
                )
                lines.append(f"- 建议: {rv_iv.get('recommendation')}")

        lines.extend(["", "4. Sell Put 推荐表 (已过滤流动性)"])

        if sell_put:
            for row in sell_put:
                liq_score = row.get("liquidity_score", 0)
                liq_emoji = (
                    "🟢" if liq_score >= 70 else "🟡" if liq_score >= 40 else "🔴"
                )
                lines.append(
                    f"- {row['risk_emoji']} {row['instrument_name']} | DTE {row['dte']} | "
                    f"Δ {row['delta']:.2f} | APR {row['apr']:.1f}% | "
                    f"权利金 {self._format_usd(row['premium_usd'])} | 流动性 {liq_emoji}{liq_score}"
                )
        else:
            lines.append("- 未筛到满足条件的 Sell Put 合约。")

        # 新增：Sell Call 推荐表
        sell_call = scan.get("sell_call") or []
        lines.extend(["", "5. Sell Call 推荐表 (已过滤流动性)"])
        if sell_call:
            for row in sell_call:
                liq_score = row.get("liquidity_score", 0)
                liq_emoji = (
                    "🟢" if liq_score >= 70 else "🟡" if liq_score >= 40 else "🔴"
                )
                lines.append(
                    f"- {row['risk_emoji']} {row['instrument_name']} | DTE {row['dte']} | "
                    f"Δ {row['delta']:.2f} | APR {row['apr']:.1f}% | "
                    f"权利金 {self._format_usd(row['premium_usd'])} | 流动性 {liq_emoji}{liq_score}"
                )
            lines.append(
                "\n⚠️  注意：Sell Call 理论上存在无限亏损风险，仅适合备兑开仓。"
            )
        else:
            lines.append("- 未筛到满足条件的 Sell Call 合约。")

        lines.extend(["", "6. 大宗异动分析"])

        if large_trades:
            analysis = self._analyze_large_trades(large_trades)
            lines.append(
                f"- 总成交: {len(large_trades)} 笔 | 总名义: {self._format_usd(analysis['total_notional'])}"
            )

            sentiment = analysis["sentiment"]
            if sentiment == "看涨":
                sentiment_emoji = "📈"
                sentiment_desc = "机构/大户偏看涨布局"
            elif sentiment == "看跌":
                sentiment_emoji = "📉"
                sentiment_desc = "卖方力量偏强或机构对冲"
            else:
                sentiment_emoji = "➡️"
                sentiment_desc = "多空平衡或观望为主"

            lines.append(
                f"- 市场情绪: {sentiment_emoji} {sentiment} ({sentiment_desc})"
            )

            lines.append(
                f"- 分类: Call {analysis['call_count']}笔 / Put {analysis['put_count']}笔 | "
                f"对冲 {analysis['hedge_count']}笔 / 权利金 {analysis['premium_count']}笔"
            )

            if analysis.get("notable_contracts"):
                lines.append("- 重点合约:")
                for nc in analysis["notable_contracts"][:3]:
                    lines.append(
                        f"  • {nc['instrument_name']}: {nc['direction']} {self._format_usd(nc['notional'])} → {nc['label_cn']}"
                    )

            lines.append("")
            lines.append("  成交明细 (Top 5):")
            for row in large_trades[:5]:
                label_cn = self._flow_label_to_cn(row["flow_label"])
                lines.append(
                    f"  - {row['severity'].upper()} {row['instrument_name']} | {row['direction']} | "
                    f"{self._format_usd(row['underlying_notional_usd'])} | Δ{row['delta']:.2f} | {label_cn}"
                )
        else:
            lines.append("- 近一小时暂无满足阈值的大宗期权成交。")

        # Max Pain 分析
        max_pain = scan.get("max_pain", {})
        lines.extend(["", "7. Max Pain 分析"])
        if max_pain and max_pain.get("max_pain_strike") is not None:
            mp_strike = max_pain["max_pain_strike"]
            mp_value = max_pain["max_pain_value"]
            spot = max_pain.get("spot_price", 0)
            put_call_ratio = max_pain.get("put_call_oi_ratio")
            total_call_oi = max_pain.get("total_call_oi", 0)
            total_put_oi = max_pain.get("total_put_oi", 0)

            lines.append(f"- 最大痛点: {mp_strike:,.0f}")
            lines.append(f"- 当前现货价: ${spot:,.2f}")
            lines.append(
                f"- 痛点偏离: {mp_strike - spot:+,.0f} ({(mp_strike - spot) / spot * 100:+.2f}%)"
            )
            lines.append(
                f"- 总 Call OI: {total_call_oi:,.0f} | 总 Put OI: {total_put_oi:,.0f}"
            )
            if put_call_ratio is not None:
                lines.append(f"- Put/Call OI 比: {put_call_ratio:.2f}")
                if put_call_ratio > 1.2:
                    lines.append("  → Put 持仓占优，市场偏防守")
                elif put_call_ratio < 0.8:
                    lines.append("  → Call 持仓占优，市场偏进攻")
                else:
                    lines.append("  → 多空持仓均衡")

            nearby = max_pain.get("nearby_pain", [])
            if nearby:
                lines.append("- 附近痛点分布:")
                sorted_nearby = sorted(nearby, key=lambda x: x["pain"], reverse=True)
                for np_item in sorted_nearby[:5]:
                    bar = (
                        "█" * int(np_item["pain"] / max(x["pain"] for x in nearby) * 20)
                        if max(x["pain"] for x in nearby) > 0
                        else ""
                    )
                    lines.append(
                        f"  • {np_item['strike']:,.0f}: {bar} {np_item['pain']:,.0f}"
                    )
        else:
            lines.append("- 无法计算 Max Pain（无期权数据）。")

        lines.extend(["", "8. 解读", interpretation])

        # 添加 RV 解读
        rv_iv = scan.get("rv_iv", {})
        if rv_iv and rv_iv.get("rv_iv_ratio") is not None:
            ratio = rv_iv["rv_iv_ratio"]
            signal = rv_iv["signal"]
            interpretation_rv = []
            interpretation_rv.append(f"\nRV/IV 额外解读：")
            if ratio < 0.6:
                interpretation_rv.append(
                    f"  • RV/IV = {ratio:.2f}，IV 显著高于 RV，期权整体高估，这是卖波动率的黄金窗口。"
                )
            elif ratio < 0.8:
                interpretation_rv.append(
                    f"  • RV/IV = {ratio:.2f}，IV 高于 RV，适合建立卖方头寸。"
                )
            elif ratio < 1.2:
                interpretation_rv.append(
                    f"  • RV/IV = {ratio:.2f}，IV 定价合理，买卖双方性价比平衡。"
                )
            elif ratio < 1.5:
                interpretation_rv.append(
                    f"  • RV/IV = {ratio:.2f}，RV 高于 IV，期权整体低估，适合买方策略。"
                )
            else:
                interpretation_rv.append(
                    f"  • RV/IV = {ratio:.2f}，RV 显著高于 IV，波动率卖方风险极高。"
                )
            lines.append("\n".join(interpretation_rv))

        lines.extend(["", "8. 策略建议", strategy])
        lines.extend(["", "总结", summary])

        lines.append(
            "\n风险提示：本报告仅供参考，不构成投资建议。期权交易涉及高风险，请控制仓位做好风险管理。"
        )

        return "\n".join(lines)
