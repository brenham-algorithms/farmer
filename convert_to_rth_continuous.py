"""
Convert full-day Databento trade CSVs into the RTH-only, continuous-symbol layout
written by download_databento.py, so existing data can share a directory with
newly downloaded data.

For each weekday file in --src:
  1. The front contract is the listed symbol with the most volume that day.
  2. Only that contract's rows inside the Chicago-time window are kept
     (default 07:30-15:00 CT, the downloader's default).
  3. The symbol column is relabelled (default MNQ.v.0).

Existing files in --dst are never overwritten.

Example:
    python convert_to_rth_continuous.py --src mnq_historical --dst mnq_2026_historical \\
        --symbols MNQM6,MNQU6 --label MNQ.v.0
"""

import argparse
import csv
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def utc_bounds(d: date, start_ct: time, end_ct: time) -> Tuple[str, str]:
    # No trailing "Z": a bare prefix compares correctly against nanosecond
    # timestamps like 2026-05-04T12:30:00.123456789Z
    fmt = "%Y-%m-%dT%H:%M:%S"
    start = datetime.combine(d, start_ct, tzinfo=CT).astimezone(timezone.utc)
    end = datetime.combine(d, end_ct, tzinfo=CT).astimezone(timezone.utc)
    return start.strftime(fmt), end.strftime(fmt)


def convert(
    src: Path, dst: Path, symbols: List[str], label: str, start_ct: time, end_ct: time
) -> str:
    d = datetime.strptime(src.name[10:18], "%Y%m%d").date()

    with src.open("r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        symbol_i, size_i = header.index("symbol"), header.index("size")
        volumes: Counter[str] = Counter()
        for row in reader:
            if row[symbol_i] in symbols:
                volumes[row[symbol_i]] += int(row[size_i])

    if not volumes:
        return f"{d}  skipped: none of {symbols} traded"
    front = volumes.most_common(1)[0][0]

    start, end = utc_bounds(d, start_ct, end_ct)
    ts_i = header.index("ts_recv")
    tmp = dst.with_suffix(".part")
    rows = 0
    with src.open("r", newline="") as f, tmp.open("w", newline="") as out:
        reader = csv.reader(f)
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(next(reader))
        for row in reader:
            if row[symbol_i] != front or not (start <= row[ts_i] < end):
                continue
            row[symbol_i] = label
            writer.writerow(row)
            rows += 1

    if rows == 0:
        tmp.unlink()
        return f"{d}  skipped: no {front} trades in window"
    tmp.rename(dst)
    return f"{d}  {front} -> {label}  {rows:>9,} rows"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert full-day trade CSVs to RTH-only continuous-symbol files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument(
        "--symbols", required=True, help="comma-separated, e.g. MNQM6,MNQU6"
    )
    parser.add_argument("--label", default="MNQ.v.0")
    parser.add_argument("--session-start", default="07:30", type=parse_hhmm)
    parser.add_argument("--session-end", default="15:00", type=parse_hhmm)
    parser.add_argument(
        "--start", type=date.fromisoformat, help="first day, YYYY-MM-DD"
    )
    parser.add_argument("--end", type=date.fromisoformat, help="last day (inclusive)")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    args.dst.mkdir(parents=True, exist_ok=True)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    jobs = []
    for src in sorted(args.src.glob("glbx-mdp3-*.trades.csv")):
        d: Optional[date] = None
        try:
            d = datetime.strptime(src.name[10:18], "%Y%m%d").date()
        except ValueError:
            continue
        if d.weekday() >= 5:
            continue
        if (args.start and d < args.start) or (args.end and d > args.end):
            continue
        dst = args.dst / src.name
        if dst.exists():
            print(f"{d}  exists in {args.dst}, left untouched")
            continue
        jobs.append((src, dst))

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                convert,
                src,
                dst,
                symbols,
                args.label,
                args.session_start,
                args.session_end,
            )
            for src, dst in jobs
        ]
        for fut in futures:
            print(fut.result())

    print(f"\n{len(jobs)} files processed into {args.dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
