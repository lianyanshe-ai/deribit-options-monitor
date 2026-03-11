#!/usr/bin/env python3
"""Deribit options monitoring skill."""

from __future__ import annotations

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
SUPPORTED_CURRENCIES = {"BTC"}


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
        state_dir = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")).expanduser()
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
        self._order_book_cache: dict[str, dict[str, Any]] = {}
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

    def _utc_now(self) -> datetime:
        return datetime.now(UTC)

    def _now_ms(self) -> int:
        return int(self._utc_now().timestamp() * 1000)

    def _normalize_currency(self, currency: str) -> str:
        normalized = (currency or "").upper().strip()
        if normalized not in SUPPORTED_CURRENCIES:
            raise ValueError(f"v1 only supports {', '.join(sorted(SUPPORTED_CURRENCIES))}")
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
        parts = instrument_name.split("-")
        if len(parts) != 4:
            raise ValueError(f"Unexpected instrument name: {instrument_name}")
        currency, date_token, strike_token, side_token = parts
        day = int(date_token[:2])
        month = MONTH_MAP[date_token[2:5].upper()]
        year = 2000 + int(date_token[5:7])
        expiry_dt = datetime(year, month, day, 8, 0, tzinfo=UTC)
        expiry_ts = int(expiry_dt.timestamp() * 1000)
        seconds = (expiry_dt - self._utc_now()).total_seconds()
        dte = max(0, math.ceil(seconds / 86400))
        option_type = "put" if side_token.upper() == "P" else "call"
        return InstrumentMeta(
            instrument_name=instrument_name,
            currency=currency,
            strike=float(strike_token),
            option_type=option_type,
            expiry_dt=expiry_dt,
            expiry_ts=expiry_ts,
            dte=dte,
        )

    def _percentile(self, values: list[float], current: float) -> float | None:
        if not values:
            return None
        below_or_equal = sum(1 for value in values if value <= current)
        return round(below_or_equal / len(values) * 100, 2)

    def _format_usd(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"${value:,.0f}"

    def _format_pct(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.2f}%"

    def _severity_from_notional(self, notional: float) -> str:
        if notional >= 2_000_000:
            return "high"
        if notional >= 500_000:
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
        return [{"ts": int(item["ts"]), "close": float(item["close"])} for item in points]

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

    def _fetch_dvol_hourly_history(self, currency: str) -> tuple[list[dict[str, float]], str]:
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

    def _store_dvol_points(self, currency: str, resolution: str, points: list[dict[str, float]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO dvol_history (ts, currency, close, resolution)
                VALUES (?, ?, ?, ?)
                """,
                [(int(point["ts"]), currency, float(point["close"]), resolution) for point in points],
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
        cached = self._order_book_cache.get(instrument_name)
        if cached is not None:
            return cached
        payload = self._request_json(
            "public/get_order_book",
            {"instrument_name": instrument_name, "depth": 1},
        )
        result = payload.get("result", {})
        self._order_book_cache[instrument_name] = result
        return result

    def _get_last_trades(self, currency: str, count: int = 1000) -> list[dict[str, Any]]:
        payload = self._request_json(
            "public/get_last_trades_by_currency",
            {"currency": currency, "kind": "option", "count": count},
        )
        return list(payload.get("result", {}).get("trades", []))

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
        currency = self._normalize_currency(currency)
        points, resolution = self._fetch_dvol_hourly_history(currency)
        self._store_dvol_points(currency, resolution, points)

        series_24h = self._load_dvol_window(currency, 24) or [float(point["close"]) for point in points[-24:]]
        series_7d = self._load_dvol_window(currency, 24 * 7) or [float(point["close"]) for point in points]
        current = float(points[-1]["close"])

        percentile_24h = self._percentile(series_24h, current)
        percentile_7d = self._percentile(series_7d, current)
        mean_7d = mean(series_7d) if series_7d else None
        std_7d = pstdev(series_7d) if len(series_7d) > 1 else None
        z_score = None
        if mean_7d is not None and std_7d and std_7d > 0:
            z_score = round((current - mean_7d) / std_7d, 2)

        if z_score is not None and z_score > 2:
            signal = "异常波动"
            recommendation = "Sell Vol / Sell Put 环境偏优，但需控制尾部风险。"
        elif percentile_7d is not None and percentile_7d >= 80:
            signal = "高波动率"
            recommendation = "权利金偏贵，可优先关注保守型 Sell Put。"
        elif percentile_7d is not None and percentile_7d <= 20:
            signal = "低波动率"
            recommendation = "权利金偏便宜，谨慎卖 Put，偏等待或买波动率。"
        else:
            signal = "中性"
            recommendation = "波动率处于中位附近，策略以筛选性价比合约为主。"

        return {
            "currency": currency,
            "current_dvol": round(current, 2),
            "history_points": len(series_7d),
            "resolution_used": resolution,
            "iv_percentile_24h": percentile_24h,
            "iv_percentile_7d": percentile_7d,
            "z_score_7d": z_score,
            "signal": signal,
            "recommendation": recommendation,
            "latest_ts": int(points[-1]["ts"]),
            "mean_7d": round(mean_7d, 2) if mean_7d is not None else None,
            "std_7d": round(std_7d, 2) if std_7d is not None else None,
        }

    def _fetch_order_books_bulk(self, instrument_names: list[str], max_workers: int = 8) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        pending = [name for name in instrument_names if name not in self._order_book_cache]
        if pending:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(self._get_order_book, name): name for name in pending}
                for future in as_completed(future_map):
                    name = future_map[future]
                    try:
                        results[name] = future.result()
                    except Exception:
                        results[name] = {}
        for name in instrument_names:
            results[name] = self._order_book_cache.get(name, results.get(name, {}))
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
        trades = [item for item in self._get_last_trades(currency) if int(item.get("timestamp", 0)) >= cutoff]
        instrument_names = sorted({item["instrument_name"] for item in trades})
        order_books = self._fetch_order_books_bulk(instrument_names)

        enriched: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []

        for trade in trades:
            meta = self._parse_instrument_name(trade["instrument_name"])
            if meta.option_type != "put":
                continue
            underlying_notional = float(trade["amount"]) * float(trade["index_price"])
            if underlying_notional < min_usd_value:
                continue

            book = order_books.get(trade["instrument_name"], {})
            greeks = book.get("greeks") or {}
            delta = float(greeks.get("delta") or 0.0)
            gamma = float(greeks.get("gamma") or 0.0)
            vega = float(greeks.get("vega") or 0.0)
            mark_iv = float(book.get("mark_iv") or trade.get("iv") or 0.0)
            premium_usd = float(trade["price"]) * float(trade["amount"]) * float(trade["index_price"])

            if (
                trade["direction"] == "buy"
                and 0.10 <= abs(delta) <= 0.35
                and 7 <= meta.dte <= 60
            ):
                flow_label = "institutional_hedge_likely"
            elif trade["direction"] == "sell" and abs(delta) <= 0.35:
                flow_label = "premium_sale_likely"
            elif abs(delta) > 0.35:
                flow_label = "speculative_flow"
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
                    "title": f"BTC 期权大宗成交 {self._format_usd(underlying_notional)}",
                    "message": (
                        f"{trade['direction']} {trade['instrument_name']}，名义金额 {self._format_usd(underlying_notional)}，"
                        f"Delta {delta:.2f}，判断为 {flow_label}。"
                    ),
                }
            )

        enriched.sort(key=lambda item: item["underlying_notional_usd"], reverse=True)
        alerts.sort(key=lambda item: {"high": 3, "medium": 2, "info": 1}[item["severity"]], reverse=True)
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

    def get_sell_put_recommendations(
        self,
        currency: str = "BTC",
        max_delta: float = 0.25,
        min_apr: float = 15.0,
        min_dte: int = 7,
        max_dte: int = 45,
        top_k: int = 5,
    ) -> dict[str, Any]:
        currency = self._normalize_currency(currency)
        summaries = self._get_book_summaries(currency)
        candidates: list[dict[str, Any]] = []
        for summary in summaries:
            instrument_name = summary.get("instrument_name", "")
            if not instrument_name.endswith("-P"):
                continue
            meta = self._parse_instrument_name(instrument_name)
            if not (min_dte <= meta.dte <= max_dte):
                continue
            mark_price = float(summary.get("mark_price") or 0.0)
            open_interest = float(summary.get("open_interest") or 0.0)
            if mark_price <= 0 or open_interest <= 0:
                continue
            candidates.append(
                {
                    "instrument_name": instrument_name,
                    "strike": meta.strike,
                    "expiry": meta.expiry_dt.isoformat(),
                    "expiry_ts": meta.expiry_ts,
                    "dte": meta.dte,
                }
            )

        order_books = self._fetch_order_books_bulk([item["instrument_name"] for item in candidates])
        picks: list[dict[str, Any]] = []
        for item in candidates:
            book = order_books.get(item["instrument_name"], {})
            greeks = book.get("greeks") or {}
            delta = float(greeks.get("delta") or 0.0)
            mark_price = float(book.get("mark_price") or 0.0)
            underlying_price = float(book.get("underlying_price") or 0.0)
            mark_iv = float(book.get("mark_iv") or 0.0)
            open_interest = float(book.get("open_interest") or 0.0)
            if mark_price <= 0 or underlying_price <= 0 or open_interest <= 0:
                continue
            if abs(delta) > max_delta:
                continue
            premium_usd = mark_price * underlying_price
            apr = premium_usd / item["strike"] * (365 / item["dte"]) * 100
            if apr < min_apr:
                continue
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
                "breakeven": round(item["strike"] - premium_usd, 2),
                "risk_emoji": self._risk_emoji(abs(delta)),
                "open_interest": round(open_interest, 2),
            }
            picks.append(pick)

        picks.sort(key=lambda row: row["apr"], reverse=True)
        final_rows = picks[:top_k]
        if final_rows:
            self._store_option_snapshots(final_rows)
        return {
            "currency": currency,
            "max_delta": max_delta,
            "min_apr": min_apr,
            "min_dte": min_dte,
            "max_dte": max_dte,
            "count": len(final_rows),
            "contracts": final_rows,
        }

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

    def _build_sell_put_alert(self, sell_put: dict[str, Any], dvol: dict[str, Any]) -> list[dict[str, Any]]:
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
                    f"发现 {len(contracts)} 个 BTC Sell Put 候选，"
                    f"首选 {top['instrument_name']}，APR {top['apr']:.2f}%，Delta {top['delta']:.2f}。"
                ),
            }
        ]

    def _build_alert_text(self, scan: dict[str, Any]) -> str:
        parts: list[str] = []
        dvol = scan["dvol"]
        parts.append(
            f"DVOL {dvol['current_dvol']:.2f}，7日分位 {self._format_pct(dvol.get('iv_percentile_7d'))}，信号 {dvol['signal']}"
        )

        sell_put = scan.get("sell_put") or []
        if sell_put:
            top = sell_put[0]
            parts.append(
                f"Top Sell Put: {top['instrument_name']}，APR {top['apr']:.2f}% / Delta {top['delta']:.2f}"
            )

        large_trades = scan.get("large_trades") or []
        if large_trades:
            top_flow = large_trades[0]
            hedge_count = sum(1 for item in large_trades if item["flow_label"] == "institutional_hedge_likely")
            flow_summary = (
                f"近{len(large_trades)}笔大宗成交，最大单 {top_flow['instrument_name']} "
                f"{self._format_usd(top_flow['underlying_notional_usd'])}"
            )
            if hedge_count:
                flow_summary += f"，其中疑似机构对冲 {hedge_count} 笔"
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
    ) -> dict[str, Any]:
        currency = self._normalize_currency(currency)
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_dvol = executor.submit(self.get_dvol_signal, currency)
            future_large = executor.submit(
                self.get_large_trade_alerts,
                currency,
                min_usd_value,
                lookback_minutes,
            )
            future_sell = executor.submit(
                self.get_sell_put_recommendations,
                currency,
                max_delta,
                min_apr,
                min_dte,
                max_dte,
                top_k,
            )
            dvol = future_dvol.result()
            large = future_large.result()
            sell_put = future_sell.result()
        alerts: list[dict[str, Any]] = []
        alerts.extend(self._build_dvol_alerts(dvol))
        alerts.extend(large.get("alerts", []))
        alerts.extend(self._build_sell_put_alert(sell_put, dvol))
        severity_rank = {"high": 3, "medium": 2, "info": 1}
        alerts.sort(key=lambda item: severity_rank.get(item["severity"], 0), reverse=True)

        result = {
            "scan_ts": self._utc_now().isoformat(),
            "currency": currency,
            "dvol": dvol,
            "large_trades": large.get("trades", []),
            "sell_put": sell_put.get("contracts", []),
            "alerts": alerts,
            "position_risk": {
                "status": "not_configured",
                "message": "v1 未接入私有持仓，跳过 Gamma/Delta 风险检查",
            },
        }
        result["alert_text"] = self._build_alert_text(result)
        result["report_text"] = self.render_report(mode="report", scan_data=result)
        return result

    def render_report(self, mode: str = "report", scan_data: dict[str, Any] | None = None, **kwargs: Any) -> str:
        scan = scan_data or self.run_scan(**kwargs)
        if mode == "json":
            return json.dumps(scan, ensure_ascii=False, indent=2)
        if mode == "alert":
            return scan.get("alert_text") or self._build_alert_text(scan)

        dvol = scan["dvol"]
        sell_put = scan["sell_put"]
        large_trades = scan["large_trades"]

        thesis_parts = []
        if dvol["signal"] in {"异常波动", "高波动率"}:
            thesis_parts.append("当前权利金整体偏贵")
        elif dvol["signal"] == "低波动率":
            thesis_parts.append("当前权利金整体偏便宜")
        else:
            thesis_parts.append("当前期权环境中性")
        if large_trades:
            top_flow = large_trades[0]["flow_label"]
            thesis_parts.append(f"近一小时出现 {len(large_trades)} 笔大宗成交，主导标签为 {top_flow}")
        if sell_put:
            thesis_parts.append(f"Sell Put 已筛出 {len(sell_put)} 个高 APR 候选")
        market_conclusion = "；".join(thesis_parts) + "。"

        lines = [
            "Deribit BTC 期权分析师报告",
            f"生成时间：{scan['scan_ts']}",
            "",
            "1. 市场结论",
            market_conclusion,
            "",
            "2. DVOL 健康度",
            (
                f"- 当前 DVOL：{dvol['current_dvol']:.2f}；24h 分位：{self._format_pct(dvol.get('iv_percentile_24h'))}；"
                f"7d 分位：{self._format_pct(dvol.get('iv_percentile_7d'))}；7d z-score："
                f"{dvol.get('z_score_7d', 'N/A')}"
            ),
            f"- 信号：{dvol['signal']}；建议：{dvol['recommendation']}",
            "",
            "3. Sell Put 推荐表",
        ]

        if sell_put:
            for row in sell_put:
                lines.append(
                    f"- {row['risk_emoji']} {row['instrument_name']} | DTE {row['dte']} | "
                    f"Delta {row['delta']:.2f} | APR {row['apr']:.2f}% | "
                    f"权利金 {self._format_usd(row['premium_usd'])} | Break-even {self._format_usd(row['breakeven'])}"
                )
        else:
            lines.append("- 未筛到满足条件的 Sell Put 合约。")

        lines.extend(["", "4. 大宗异动摘要"])
        if large_trades:
            for row in large_trades[:5]:
                lines.append(
                    f"- {row['severity'].upper()} {row['instrument_name']} | {row['direction']} | "
                    f"名义金额 {self._format_usd(row['underlying_notional_usd'])} | "
                    f"Delta {row['delta']:.2f} | 判断 {row['flow_label']}"
                )
        else:
            lines.append("- 近一小时暂无满足阈值的大宗期权成交。")

        lines.extend(["", "5. 风险提示与行动建议"])
        if dvol["signal"] == "异常波动":
            lines.append("- 风险提示：波动率显著偏离均值，Sell Put 收益高，但需控制黑天鹅尾部风险。")
        elif dvol["signal"] == "低波动率":
            lines.append("- 风险提示：权利金偏薄，卖 Put 的赔率不够友好。")
        else:
            lines.append("- 风险提示：当前环境可做筛选式收租，但不宜过度追求 Delta。")
        if sell_put:
            best = sell_put[0]
            lines.append(
                f"- 行动建议：优先观察 {best['instrument_name']}，APR {best['apr']:.2f}% / "
                f"Delta {best['delta']:.2f}，适合作为 v1 收租基准参考。"
            )
        else:
            lines.append("- 行动建议：等待更高 IV 或更好的 skew，再考虑执行卖 Put。")
        lines.append("- 持仓风险：v1 未接入真实账户持仓，Gamma/Delta 风险需手工复核。")
        return "\n".join(lines)
