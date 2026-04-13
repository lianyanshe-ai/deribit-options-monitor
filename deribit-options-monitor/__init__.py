#!/usr/bin/env python3
"""CLI entrypoint for deribit-options-monitor."""

from __future__ import annotations

import argparse
import json

from deribit_options_monitor import DeribitOptionsMonitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Deribit options monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Run connectivity and storage checks")

    dvol_parser = subparsers.add_parser("dvol", help="Get DVOL signal")
    dvol_parser.add_argument("--currency", default="BTC")

    trades_parser = subparsers.add_parser("large-trades", help="Get large option trades")
    trades_parser.add_argument("--currency", default="BTC")
    trades_parser.add_argument("--min-usd-value", type=float, default=500000)
    trades_parser.add_argument("--lookback-minutes", type=int, default=60)

    sell_put_parser = subparsers.add_parser("sell-put", help="Get sell put recommendations")
    sell_put_parser.add_argument("--currency", default="BTC")
    sell_put_parser.add_argument("--max-delta", type=float, default=0.25)
    sell_put_parser.add_argument("--min-apr", type=float, default=15.0)
    sell_put_parser.add_argument("--min-dte", type=int, default=7)
    sell_put_parser.add_argument("--max-dte", type=int, default=45)
    sell_put_parser.add_argument("--top-k", type=int, default=5)
    sell_put_parser.add_argument("--max-spread-pct", type=float, default=10.0, help="Max bid-ask spread percentage")
    sell_put_parser.add_argument("--min-open-interest", type=float, default=100.0, help="Min open interest")

    sell_call_parser = subparsers.add_parser("sell-call", help="Get sell call recommendations")
    sell_call_parser.add_argument("--currency", default="BTC")
    sell_call_parser.add_argument("--max-delta", type=float, default=0.20)
    sell_call_parser.add_argument("--min-apr", type=float, default=15.0)
    sell_call_parser.add_argument("--min-dte", type=int, default=7)
    sell_call_parser.add_argument("--max-dte", type=int, default=45)
    sell_call_parser.add_argument("--top-k", type=int, default=5)
    sell_call_parser.add_argument("--max-spread-pct", type=float, default=10.0, help="Max bid-ask spread percentage")
    sell_call_parser.add_argument("--min-open-interest", type=float, default=100.0, help="Min open interest")

    rv_parser = subparsers.add_parser("rv", help="Get RV/IV realized volatility signal")
    rv_parser.add_argument("--currency", default="BTC")

    scan_parser = subparsers.add_parser("scan", help="Run a full scan")
    scan_parser.add_argument("--currency", default="BTC")
    scan_parser.add_argument("--min-usd-value", type=float, default=500000)
    scan_parser.add_argument("--lookback-minutes", type=int, default=60)
    scan_parser.add_argument("--max-delta", type=float, default=0.25)
    scan_parser.add_argument("--min-apr", type=float, default=15.0)
    scan_parser.add_argument("--min-dte", type=int, default=7)
    scan_parser.add_argument("--max-dte", type=int, default=45)
    scan_parser.add_argument("--top-k", type=int, default=5)
    scan_parser.add_argument("--max-spread-pct", type=float, default=10.0)
    scan_parser.add_argument("--min-open-interest", type=float, default=100.0)
    scan_parser.add_argument("--max-delta-sell-call", type=float, default=0.20, help="Max delta for sell call recommendations")
    scan_parser.add_argument("--min-apr-sell-call", type=float, default=15.0, help="Min APR for sell call recommendations")

    report_parser = subparsers.add_parser("report", help="Render a scan result")
    report_parser.add_argument("--currency", default="BTC")
    report_parser.add_argument("--min-usd-value", type=float, default=500000)
    report_parser.add_argument("--lookback-minutes", type=int, default=60)
    report_parser.add_argument("--max-delta", type=float, default=0.25)
    report_parser.add_argument("--min-apr", type=float, default=15.0)
    report_parser.add_argument("--min-dte", type=int, default=7)
    report_parser.add_argument("--max-dte", type=int, default=45)
    report_parser.add_argument("--top-k", type=int, default=5)
    report_parser.add_argument("--max-spread-pct", type=float, default=10.0)
    report_parser.add_argument("--min-open-interest", type=float, default=100.0)
    report_parser.add_argument("--max-delta-sell-call", type=float, default=0.20, help="Max delta for sell call recommendations")
    report_parser.add_argument("--min-apr-sell-call", type=float, default=15.0, help="Min APR for sell call recommendations")
    report_parser.add_argument("--mode", choices=("report", "json", "alert"), default="report")

    args = parser.parse_args()
    monitor = DeribitOptionsMonitor()

    if args.command == "doctor":
        result = monitor.doctor()
    elif args.command == "dvol":
        result = monitor.get_dvol_signal(currency=args.currency)
    elif args.command == "large-trades":
        result = monitor.get_large_trade_alerts(
            currency=args.currency,
            min_usd_value=args.min_usd_value,
            lookback_minutes=args.lookback_minutes,
        )
    elif args.command == "sell-put":
        result = monitor.get_sell_put_recommendations(
            currency=args.currency,
            max_delta=args.max_delta,
            min_apr=args.min_apr,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            top_k=args.top_k,
            max_spread_pct=args.max_spread_pct,
            min_open_interest=args.min_open_interest,
        )
    elif args.command == "sell-call":
        result = monitor.get_sell_call_recommendations(
            currency=args.currency,
            max_delta=args.max_delta,
            min_apr=args.min_apr,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            top_k=args.top_k,
            max_spread_pct=args.max_spread_pct,
            min_open_interest=args.min_open_interest,
        )
    elif args.command == "rv":
        result = monitor.get_rv_iv_signal(currency=args.currency)
    else:
        scan_result = monitor.run_scan(
            currency=args.currency,
            min_usd_value=args.min_usd_value,
            lookback_minutes=args.lookback_minutes,
            max_delta=args.max_delta,
            min_apr=args.min_apr,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            top_k=args.top_k,
            max_spread_pct=args.max_spread_pct,
            min_open_interest=args.min_open_interest,
            max_delta_sell_call=args.max_delta_sell_call,
            min_apr_sell_call=args.min_apr_sell_call,
        )
        if args.command == "report":
            result = dict(scan_result)
            result["selected_mode"] = args.mode
            result["selected_output"] = monitor.render_report(mode=args.mode, scan_data=scan_result)
        else:
            result = scan_result

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
