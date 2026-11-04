"""
Download Databento GLBX MDP3 trades into the CSV layout CsvTicker reads.

Each trading day is requested separately for a Chicago-time window (default
07:30-15:00 CT, enough warmup for intraday strategies), so you pay only for the
hours you backtest. Files are written as {out_dir}/glbx-mdp3-{YYYYMMDD}.trades.csv.

The API key is read from the DATABENTO_API_KEY environment variable.

Examples:
    # Free: print the cost of each day and the total, download nothing
    python download_databento.py --symbol MNQ.v.0 --start 2025-01-01 --end 2025-12-31

    # Download, refusing to start if the total would exceed $40
    python download_databento.py --symbol MNQ.v.0 --start 2025-01-01 --end 2025-12-31 \\
        --out-dir mnq_2025_historical --download --max-cost 40
"""

import argparse
import os
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import requests

API_URL = "https://hist.databento.com/v0"
CT = ZoneInfo("America/Chicago")


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def trading_days(start: date, end: date) -> List[date]:
    # CME RTH runs Monday-Friday; exchange holidays just return no rows
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def utc_window(d: date, start_ct: time, end_ct: time) -> Tuple[str, str]:
    """Convert a Chicago-time window on day d to UTC ISO strings (handles DST)."""
    start = datetime.combine(d, start_ct, tzinfo=CT).astimezone(timezone.utc)
    end = datetime.combine(d, end_ct, tzinfo=CT).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def request_params(args: argparse.Namespace, start: str, end: str) -> Dict[str, str]:
    return {
        "dataset": args.dataset,
        "symbols": args.symbol,
        "stype_in": args.stype_in,
        "schema": "trades",
        "start": start,
        "end": end,
    }


def post(
    session: requests.Session, endpoint: str, data: Dict[str, str], stream: bool = False
) -> requests.Response:
    for attempt in range(5):
        resp = session.post(
            f"{API_URL}/{endpoint}", data=data, timeout=300, stream=stream
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            time_mod.sleep(2**attempt)
            continue
        if not resp.ok:
            raise RuntimeError(f"{endpoint} failed ({resp.status_code}): {resp.text}")
        return resp
    raise RuntimeError(
        f"{endpoint} failed after retries ({resp.status_code}): {resp.text}"
    )


def get_cost(session: requests.Session, params: Dict[str, str]) -> float:
    return float(post(session, "metadata.get_cost", params).json())


def download_day(
    session: requests.Session, params: Dict[str, str], path: Path
) -> Tuple[int, List[str]]:
    data = dict(
        params, encoding="csv", pretty_px="true", pretty_ts="true", map_symbols="true"
    )
    tmp = path.with_suffix(".part")
    with (
        post(session, "timeseries.get_range", data, stream=True) as resp,
        tmp.open("wb") as f,
    ):
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)

    # Inspect what came back: row count and the values in the symbol column
    rows, symbols = 0, set()
    with tmp.open("r") as f:
        header = f.readline().rstrip("\n").split(",")
        symbol_i = header.index("symbol") if "symbol" in header else None
        for line in f:
            rows += 1
            if symbol_i is not None:
                symbols.add(line.rstrip("\n").split(",")[symbol_i])

    if rows == 0:
        tmp.unlink()
    else:
        tmp.rename(path)
    return rows, sorted(symbols)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Price and download Databento trades for backtesting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dataset", default="GLBX.MDP3")
    parser.add_argument(
        "--symbol", required=True, help="e.g. MNQ.v.0 (front month by volume) or NQ.v.0"
    )
    parser.add_argument(
        "--stype-in", default="continuous", help="continuous | raw_symbol | parent"
    )
    parser.add_argument(
        "--start", required=True, type=date.fromisoformat, help="first day, YYYY-MM-DD"
    )
    parser.add_argument(
        "--end",
        required=True,
        type=date.fromisoformat,
        help="last day (inclusive), YYYY-MM-DD",
    )
    parser.add_argument(
        "--session-start", default="07:30", type=parse_hhmm, help="Chicago time, HH:MM"
    )
    parser.add_argument(
        "--session-end", default="15:00", type=parse_hhmm, help="Chicago time, HH:MM"
    )
    parser.add_argument("--out-dir", type=Path, help="required with --download")
    parser.add_argument(
        "--download",
        action="store_true",
        help="without this flag nothing is downloaded",
    )
    parser.add_argument("--max-cost", type=float, help="USD; required with --download")
    args = parser.parse_args()

    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        print("Set DATABENTO_API_KEY first", file=sys.stderr)
        return 1
    if args.download and (args.out_dir is None or args.max_cost is None):
        parser.error("--download requires --out-dir and --max-cost")

    session = requests.Session()
    session.auth = (api_key, "")

    days = trading_days(args.start, args.end)
    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    # Price every day first (free), skipping files already downloaded
    plan: List[Tuple[date, Dict[str, str], float]] = []
    total = 0.0
    for d in days:
        path = (
            args.out_dir / f"glbx-mdp3-{d:%Y%m%d}.trades.csv" if args.out_dir else None
        )
        if path is not None and path.exists():
            continue
        params = request_params(
            args, *utc_window(d, args.session_start, args.session_end)
        )
        cost = get_cost(session, params)
        total += cost
        plan.append((d, params, cost))
        print(
            f"{d}  {params['start']} -> {params['end']}  ${cost:.4f}  (running total ${total:.2f})"
        )

    print(f"\n{len(plan)} days to fetch, estimated total ${total:.2f}")
    if not args.download:
        print("Dry run only. Add --download --out-dir DIR --max-cost USD to fetch.")
        return 0
    if total > args.max_cost:
        print(
            f"Estimated ${total:.2f} exceeds --max-cost ${args.max_cost:.2f}; nothing downloaded."
        )
        return 1

    for d, params, _ in plan:
        path = args.out_dir / f"glbx-mdp3-{d:%Y%m%d}.trades.csv"
        rows, symbols = download_day(session, params, path)
        note = f"symbols={symbols}" if rows else "no rows (holiday?), no file written"
        print(f"{d}  {rows:>9,} rows  {note}")

    print(
        "\nSet ticker_params.symbols / start_symbol in your config to the symbol values printed above."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
