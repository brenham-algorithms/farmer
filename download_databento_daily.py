"""
Download Databento GLBX MDP3 daily bars (ohlcv-1d) for a list of futures roots.

For each root, both the front contract (ROOT.v.0) and the next one (ROOT.v.1) are
requested, ranked by volume. On a roll day the new front contract was the previous
day's .v.1, so returns can be stitched across rolls without the roll gap. Files are
written as {out_dir}/{ROOT}.ohlcv-1d.csv.

The API key is read from the DATABENTO_API_KEY environment variable.

Examples:
    # Free: print the cost per root and the total, download nothing. With --out-dir,
    # roots already downloaded there are skipped (in the quote too)
    python download_databento_daily.py --roots ES NQ ZN CL GC --start 2010-06-06 --end 2026-11-06

    # Download, refusing to start if the total would exceed $10
    python download_databento_daily.py --roots ES NQ ZN CL GC --start 2010-06-06 --end 2026-11-06 \\
        --out-dir daily_historical --download --max-cost 10
"""

import argparse
import os
import sys
import time as time_mod
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List

import requests

from download_databento import get_cost, post

# Trend-following research universe (CME Globex): equity indexes, rates, FX, energy,
# metals, grains and livestock
DEFAULT_ROOTS = [
    "ES", "NQ", "RTY", "YM",
    "ZT", "ZF", "ZN", "ZB",
    "6E", "6J", "6B", "6A", "6C",
    "CL", "NG", "HO", "RB",
    "GC", "SI", "HG",
    "ZC", "ZS", "ZW",
    "LE", "HE",
]  # fmt: skip


def request_params(
    args: argparse.Namespace, root: str, end_exclusive: str
) -> Dict[str, str]:
    return {
        "dataset": args.dataset,
        "symbols": f"{root}.v.0,{root}.v.1",
        "stype_in": "continuous",
        "schema": "ohlcv-1d",
        "start": args.start,
        "end": end_exclusive,
    }


def download_root(
    session: requests.Session, params: Dict[str, str], path: Path, attempts: int = 3
) -> int:
    """Download one root, retrying if the stream breaks partway through."""
    for attempt in range(1, attempts + 1):
        try:
            return _download_once(session, params, path)
        except (
            requests.exceptions.ChunkedEncodingError,  # "Response ended prematurely"
            requests.ConnectionError,
            requests.Timeout,
        ) as e:
            print(f"  attempt {attempt}/{attempts} failed: {e}", file=sys.stderr)
            if attempt == attempts:
                raise
            time_mod.sleep(5 * attempt)
    return 0


def _download_once(
    session: requests.Session, params: Dict[str, str], path: Path
) -> int:
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
    with tmp.open("r") as f:
        rows = sum(1 for _ in f) - 1
    if rows <= 0:
        tmp.unlink()
        return 0
    tmp.rename(path)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", default="GLBX.MDP3")
    parser.add_argument("--roots", nargs="+", default=DEFAULT_ROOTS)
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last day (inclusive), YYYY-MM-DD")
    parser.add_argument("--out-dir", help="required with --download")
    parser.add_argument(
        "--download", action="store_true", help="without this nothing is downloaded"
    )
    parser.add_argument("--max-cost", type=float, help="USD; required with --download")
    args = parser.parse_args()

    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("DATABENTO_API_KEY is not set", file=sys.stderr)
        return 1
    if args.download and (args.out_dir is None or args.max_cost is None):
        print("--download needs --out-dir and --max-cost", file=sys.stderr)
        return 1

    # The API's end is exclusive; add a day so --end is inclusive
    end_exclusive = (date.fromisoformat(args.end) + timedelta(days=1)).isoformat()

    session = requests.Session()
    session.auth = (key, "")

    # Roots that already have a file in --out-dir are skipped, so a failed run can resume
    roots = args.roots
    if args.out_dir is not None:
        have = [r for r in roots if (Path(args.out_dir) / f"{r}.ohlcv-1d.csv").exists()]
        if have:
            print(f"already downloaded, skipping: {' '.join(have)}")
        roots = [r for r in roots if r not in have]

    costs: List[float] = []
    for root in roots:
        cost = get_cost(session, request_params(args, root, end_exclusive))
        costs.append(cost)
        print(f"{root:>4}: ${cost:.4f}")
    total = sum(costs)
    print(f"total: ${total:.2f} for {len(roots)} roots")

    if not args.download:
        return 0
    if total > args.max_cost:
        print(f"total ${total:.2f} exceeds --max-cost {args.max_cost}", file=sys.stderr)
        return 1

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for root in roots:
        path = out / f"{root}.ohlcv-1d.csv"
        rows = download_root(session, request_params(args, root, end_exclusive), path)
        print(f"{root:>4}: {rows} rows -> {path if rows else 'nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
