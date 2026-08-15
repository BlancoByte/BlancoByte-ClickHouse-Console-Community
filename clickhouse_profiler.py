#!/usr/bin/env python3
"""
ClickHouse Log Profiler v1.1
============================================================
A powerful CLI tool for analyzing ClickHouse server logs.
Requires Python 3.7+. S3 support requires: pip install boto3

Usage:
  python clickhouse_profiler.py clickhouse-server.log
  python clickhouse_profiler.py -n 10 -r html -o report.html server.log
  python clickhouse_profiler.py s3://my-bucket/logs/clickhouse-server.log
  python clickhouse_profiler.py s3://my-bucket/logs/ --s3-prefix clickhouse-server
"""

import re
import io
import os
import sys
import gzip
import bz2
import json
import csv
import socket
import argparse
import datetime
import statistics
from typing import List, Dict, Optional, Tuple, Any
from collections import defaultdict
from dataclasses import dataclass, field
from io import StringIO


# ============================================================
# Data Models
# ============================================================

@dataclass
class QueryInfo:
    """Stores information about a single query execution."""
    timestamp:          Optional[datetime.datetime] = None
    query_id:           str   = ""
    thread_id:          str   = ""
    client_host:        str   = ""
    client_port:        str   = ""
    user:               str   = ""
    query:              str   = ""
    normalized_query:   str   = ""
    completed:          bool  = False
    peak_memory_bytes:  float = 0.0
    rows_read:          int   = 0
    bytes_read:         float = 0.0
    duration_sec:       float = 0.0
    initial_query_id:   str   = ""
    databases:          List[str] = field(default_factory=list)
    tables:             List[str] = field(default_factory=list)
    error_code:         str   = ""
    error_message:      str   = ""
    has_error:          bool  = False


@dataclass
class QueryGroup:
    """Groups similar queries and aggregates their statistics."""
    normalized_query: str = ""
    queries: List[QueryInfo] = field(default_factory=list)

    @property
    def count(self)            -> int:         return len(self.queries)
    @property
    def exec_times(self)       -> List[float]: return [q.duration_sec for q in self.queries]
    @property
    def rows_read_list(self)   -> List[int]:   return [q.rows_read for q in self.queries]
    @property
    def bytes_read_list(self)  -> List[float]: return [q.bytes_read for q in self.queries]
    @property
    def peak_memory_list(self) -> List[float]: return [q.peak_memory_bytes for q in self.queries]

    @property
    def qps(self) -> float:
        """Queries per second within the observed time window."""
        timestamps = [q.timestamp for q in self.queries if q.timestamp]
        if len(timestamps) < 2:
            return 0.0
        span = (max(timestamps) - min(timestamps)).total_seconds()
        return (len(timestamps) / span) if span > 0 else 0.0

    @property
    def time_range(self) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime]]:
        ts = [q.timestamp for q in self.queries if q.timestamp]
        return (min(ts), max(ts)) if ts else (None, None)

    @property
    def users(self) -> Dict[str, int]:
        d: Dict[str, int] = defaultdict(int)
        for q in self.queries:
            if q.user:
                d[q.user] += 1
        return dict(d)

    @property
    def hosts(self) -> Dict[str, int]:
        d: Dict[str, int] = defaultdict(int)
        for q in self.queries:
            if q.client_host:
                d[q.client_host] += 1
        return dict(d)

    @property
    def databases(self) -> Dict[str, int]:
        d: Dict[str, int] = defaultdict(int)
        for q in self.queries:
            for db in q.databases:
                d[db] += 1
        return dict(d)

    @property
    def completion_rate(self) -> Tuple[int, int]:
        completed = sum(1 for q in self.queries if q.completed)
        return completed, len(self.queries)

    @property
    def error_count(self) -> int:
        return sum(1 for q in self.queries if q.has_error)

    def get_sort_value(self, sort_field: str, sort_operation: str) -> float:
        mapping = {
            "ExecTime":   self.exec_times,
            "RowsRead":   [float(x) for x in self.rows_read_list],
            "BytesRead":  self.bytes_read_list,
            "PeakMemory": self.peak_memory_list,
            "QPS":        [self.qps],
            "QueryCount": [float(self.count)],
        }
        vals = mapping.get(sort_field, self.exec_times)
        if sort_field in ("QPS", "QueryCount"):
            return vals[0] if vals else 0.0
        return _stat(vals, sort_operation)


# ============================================================
# Statistics Helpers
# ============================================================

def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    idx = (p / 100.0) * (len(sv) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sv) - 1)
    return sv[lo] + (idx - lo) * (sv[hi] - sv[lo])


def _stat(values: List[float], op: str) -> float:
    if not values:
        return 0.0
    ops = {
        "sum":    lambda v: sum(v),
        "min":    lambda v: min(v),
        "max":    lambda v: max(v),
        "avg":    lambda v: sum(v) / len(v),
        "per95":  lambda v: _percentile(v, 95),
        "stddev": lambda v: (statistics.stdev(v) if len(v) > 1 else 0.0),
        "median": lambda v: statistics.median(v),
    }
    return ops.get(op, ops["max"])(values)


def compute_stats(values: List[float]) -> Dict[str, float]:
    """Return common statistical measures for the given value list."""
    if not values:
        return {k: 0.0 for k in ("total", "min", "max", "avg", "p95", "stddev", "median")}
    return {
        "total":  sum(values),
        "min":    min(values),
        "max":    max(values),
        "avg":    sum(values) / len(values),
        "p95":    _percentile(values, 95),
        "stddev": (statistics.stdev(values) if len(values) > 1 else 0.0),
        "median": statistics.median(values),
    }


# ============================================================
# Formatting Helpers
# ============================================================

def fmt_bytes(b: float) -> str:
    """Format a byte count as a human-readable string."""
    if b <= 0:
        return "0.00B"
    for unit, thr in [("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)]:
        if b >= thr:
            return f"{b/thr:.2f}{unit}"
    return f"{b:.2f}B"


def fmt_time(sec: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    if sec <= 0:
        return "0.00s"
    if sec >= 1000:
        return f"{sec/1000:.2f}ks"
    if sec >= 1:
        return f"{sec:.2f}s"
    if sec >= 1e-3:
        return f"{sec*1e3:.2f}ms"
    return f"{sec*1e6:.2f}us"


def fmt_num(n: float) -> str:
    """Format a large number with SI suffixes."""
    if n >= 1e9:
        return f"{n/1e9:.2f}G"
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.2f}k"
    return f"{n:.2f}"


# ============================================================
# S3 Helpers
# ============================================================

def _s3_client(aws_access_key: Optional[str] = None,
               aws_secret_key: Optional[str] = None,
               aws_session_token: Optional[str] = None,
               endpoint_url: Optional[str] = None,
               region: Optional[str] = None):
    """
    Return a boto3 S3 client.
    Credentials are resolved in this order:
      1. Explicit CLI arguments (--s3-access-key / --s3-secret-key)
      2. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
      3. ~/.aws/credentials or IAM instance role (boto3 default chain)
    """
    try:
        import boto3
    except ImportError:
        print(
            "[!] S3 support requires boto3.\n"
            "    Install it with:  pip install boto3",
            file=sys.stderr,
        )
        sys.exit(1)

    kwargs: Dict[str, Any] = {}
    if aws_access_key:
        kwargs["aws_access_key_id"] = aws_access_key
    if aws_secret_key:
        kwargs["aws_secret_access_key"] = aws_secret_key
    if aws_session_token:
        kwargs["aws_session_token"] = aws_session_token
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url  # MinIO / compatible stores
    if region:
        kwargs["region_name"] = region

    return boto3.client("s3", **kwargs)


def _parse_s3_url(url: str) -> Tuple[str, str]:
    """Parse s3://bucket/key/path into (bucket, key)."""
    without_scheme = url[len("s3://"):]
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def _s3_list_objects(client, bucket: str, prefix: str) -> List[str]:
    """List all object keys under bucket/prefix, return full s3:// URLs."""
    keys: List[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(f"s3://{bucket}/{obj['Key']}")
    return sorted(keys)


def _open_s3_object(client, bucket: str, key: str) -> io.TextIOWrapper:
    """
    Stream an S3 object and return a text file-like object.
    Transparently decompresses .gz and .bz2 files.
    """
    response = client.get_object(Bucket=bucket, Key=key)
    raw: io.IOBase = response["Body"]

    lower = key.lower()
    if lower.endswith(".gz"):
        binary = gzip.GzipFile(fileobj=raw)
    elif lower.endswith((".bz2", ".bzip2")):
        # bz2 doesn't accept a streaming object — buffer in memory
        binary = io.BytesIO(bz2.decompress(raw.read()))
    else:
        binary = raw  # type: ignore[assignment]

    return io.TextIOWrapper(binary, encoding="utf-8", errors="replace")


# ============================================================
# ClickHouse Log Parser
# ============================================================

class ClickHouseLogParser:
    """Parses ClickHouse server log files and extracts query information."""

    LOG_LINE_RE = re.compile(
        r"^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
        r"\s+\[\s*(\d+)\s*\]"
        r"\s+\{([^}]*)\}"
        r"\s+<(\w+)>"
        r"\s+(.*)",
        re.DOTALL,
    )

    # Matches both formats:
    #   (from HOST:PORT) QUERY (stage: Complete)
    #   (from HOST:PORT, user: bob) QUERY
    QUERY_START_RE = re.compile(
        r"executeQuery: \(from ([^:]+):(\d+)(?:[^)]+)?\)\s+(.*)",
        re.DOTALL,
    )

    # Optional user field inside the from-block
    USER_RE = re.compile(r"\buser:\s*([^\s,)]+)")

    # Newer ClickHouse versions prepend "(query N, line N)" to the query text
    QUERY_PREFIX_RE = re.compile(r"^\s*\(query\s+\d+,\s*line\s+\d+\)\s*")

    QUERY_COMPLETE_RE = re.compile(
        r"executeQuery: Read ([\d,]+) rows,\s*"
        r"([0-9.]+)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\s+in\s+([0-9.]+)\s+sec\."
    )

    PEAK_MEM_RE = re.compile(
        r"MemoryTracker: Peak memory usage \(for query\):\s+([0-9.]+)\s+(\w+)\."
    )

    ERROR_RE = re.compile(
        r"Code:\s*(\d+)\.\s*(?:DB::)?Exception[^:]*:\s*(.*?)(?:\n|Stack trace:|$)",
        re.DOTALL,
    )

    INITIAL_QID_RE = re.compile(r"initial_query_id:\s*([a-f0-9-]{36})")

    # Best-effort extraction of referenced databases and tables
    TABLE_RE = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+`?(\w+)`?\.`?(\w+)`?",
        re.IGNORECASE,
    )

    TIMESTAMP_FORMAT = "%Y.%m.%d %H:%M:%S.%f"

    # DDL / write statements to skip
    SKIP_PREFIXES = (
        "INSERT", "CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME",
        "ATTACH", "DETACH", "OPTIMIZE", "SYSTEM", "SET ", "USE ",
    )

    BYTE_UNITS: Dict[str, int] = {
        "B": 1, "BYTES": 1,
        "KB": 1_000, "KIB": 1_024,
        "MB": 1_000_000, "MIB": 1_048_576,
        "GB": 1_000_000_000, "GIB": 1_073_741_824,
        "TB": 1_000_000_000_000, "TIB": 1_099_511_627_776,
    }

    def __init__(self) -> None:
        self._pending: Dict[str, QueryInfo] = {}
        self.all_queries: List[QueryInfo] = []
        self.total_lines = 0
        self.skipped_lines = 0

    @staticmethod
    def _open_local(path: str):
        """Open a local plain, gzip, or bzip2 file for reading."""
        p = path.lower()
        if p.endswith(".gz"):
            return gzip.open(path, "rt", encoding="utf-8", errors="replace")
        if p.endswith((".bz2", ".bzip2")):
            return bz2.open(path, "rt", encoding="utf-8", errors="replace")
        return open(path, "r", encoding="utf-8", errors="replace")

    def _to_bytes(self, value: float, unit: str) -> float:
        return value * self.BYTE_UNITS.get(unit.upper(), 1)

    def _parse_ts(self, s: str) -> Optional[datetime.datetime]:
        try:
            return datetime.datetime.strptime(s, self.TIMESTAMP_FORMAT)
        except ValueError:
            return None

    @staticmethod
    def normalize(query: str) -> str:
        """
        Replace literal values with placeholders so that structurally
        identical queries with different parameters are grouped together.
        """
        q = re.sub(r"--[^\n]*", "", query)
        q = re.sub(r"/\*.*?\*/", "", q, flags=re.DOTALL)
        q = re.sub(r"'[^']*'", "'?'", q)
        q = re.sub(r'"[^"]*"', '"?"', q)
        q = re.sub(r"\bIN\s*\([^)]+\)", "IN (?+)", q, flags=re.IGNORECASE)
        q = re.sub(r"\b\d+(?:\.\d+)?\b", "?", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q.upper()

    def _process_line(self, line: str) -> None:
        m = self.LOG_LINE_RE.match(line)
        if not m:
            self.skipped_lines += 1
            return

        ts_str, thread_id, query_id, level, message = m.groups()
        timestamp = self._parse_ts(ts_str)

        # 1) Query start
        qs = self.QUERY_START_RE.match(message)
        if qs:
            host, port, raw_query = qs.group(1), qs.group(2), qs.group(3)
            from_end = message.find(")", message.find("(from"))
            from_block = message[message.find("(from"): from_end + 1]
            um = self.USER_RE.search(from_block)
            user = um.group(1) if um else "unknown"
            # Strip trailing (stage: ...) added by newer ClickHouse versions
            raw_query = re.sub(r"\s*\(stage:[^)]*\)\s*$", "", raw_query).strip().rstrip(";")
            # Strip leading (query N, line N) prefix added by newer ClickHouse versions
            raw_query = self.QUERY_PREFIX_RE.sub("", raw_query).strip()
            if not raw_query:
                return
            upper = raw_query.lstrip().upper()
            if any(upper.startswith(pfx) for pfx in self.SKIP_PREFIXES):
                return
            qi = QueryInfo(
                timestamp=timestamp,
                query_id=query_id,
                thread_id=thread_id,
                client_host=host.strip(),
                client_port=port.strip(),
                user=user.strip(),
                query=raw_query,
                normalized_query=self.normalize(raw_query),
            )

            for db, tbl in self.TABLE_RE.findall(raw_query):
                if db and db not in qi.databases:
                    qi.databases.append(db)
                if tbl and tbl not in qi.tables:
                    qi.tables.append(tbl)

            iq = self.INITIAL_QID_RE.search(message)
            if iq:
                qi.initial_query_id = iq.group(1)

            self._pending[query_id] = qi
            return

        # 2) Query completion (rows / bytes / duration)
        qc = self.QUERY_COMPLETE_RE.search(message)
        if qc and query_id in self._pending:
            qi = self._pending[query_id]
            rows_str, val_str, unit, dur_str = qc.groups()
            qi.rows_read = int(rows_str.replace(",", ""))
            qi.bytes_read = self._to_bytes(float(val_str), unit)
            qi.duration_sec = float(dur_str)
            qi.completed = True
            self.all_queries.append(qi)
            return

        # 3) Peak memory
        pm = self.PEAK_MEM_RE.search(message)
        if pm and query_id in self._pending:
            self._pending[query_id].peak_memory_bytes = self._to_bytes(
                float(pm.group(1)), pm.group(2)
            )
            return

        # 4) Error
        if level in ("Error", "Fatal") and query_id in self._pending:
            qi = self._pending[query_id]
            qi.has_error = True
            err = self.ERROR_RE.search(message)
            if err:
                qi.error_code = err.group(1)
                qi.error_message = err.group(2).strip()[:300]
            else:
                qi.error_message = message.strip()[:300]
            if not qi.completed:
                self.all_queries.append(qi)

    def parse_file(self, path: str, verbose: bool = False,
                   s3_client=None) -> None:
        """Parse a single log file — local path or s3:// URL."""

        if path.startswith("s3://"):
            self._parse_s3_file(path, verbose=verbose, s3_client=s3_client)
            return

        if not os.path.exists(path):
            print(f"[!] File not found: {path}", file=sys.stderr)
            return

        if verbose:
            size_mb = os.path.getsize(path) / 1e6
            print(f"    Reading: {path}  ({size_mb:.1f} MB)", file=sys.stderr)

        with self._open_local(path) as fh:
            for line in fh:
                self.total_lines += 1
                self._process_line(line.rstrip("\n"))

    def _parse_s3_file(self, s3_url: str, verbose: bool = False,
                       s3_client=None) -> None:
        """Stream and parse a single S3 object."""
        bucket, key = _parse_s3_url(s3_url)
        if verbose:
            print(f"    Reading S3: {s3_url}", file=sys.stderr)
        try:
            fh = _open_s3_object(s3_client, bucket, key)
            for line in fh:
                self.total_lines += 1
                self._process_line(line.rstrip("\n"))
        except Exception as exc:
            print(f"[!] Failed to read {s3_url}: {exc}", file=sys.stderr)

    def parse_files(self, paths: List[str], verbose: bool = False,
                    s3_client=None) -> None:
        """
        Parse a list of paths.  Each entry may be:
          - A local file path          e.g. /var/log/clickhouse-server.log
          - An s3:// object URL        e.g. s3://bucket/logs/server.log
          - An s3:// prefix (folder)   e.g. s3://bucket/logs/
            → all objects under that prefix are listed and parsed.
        """
        expanded: List[str] = []
        for p in paths:
            if p.startswith("s3://") and (p.endswith("/") or "." not in p.split("/")[-1]):
                # Treat as a prefix → list all objects beneath it
                if s3_client is None:
                    print(
                        "[!] S3 client not initialised — pass --s3-access-key or "
                        "set AWS_ACCESS_KEY_ID in your environment.",
                        file=sys.stderr,
                    )
                    continue
                bucket, prefix = _parse_s3_url(p)
                objects = _s3_list_objects(s3_client, bucket, prefix)
                if not objects:
                    print(f"[!] No objects found under: {p}", file=sys.stderr)
                else:
                    if verbose:
                        print(f"    Found {len(objects)} object(s) under {p}", file=sys.stderr)
                    expanded.extend(objects)
            else:
                expanded.append(p)

        for p in expanded:
            self.parse_file(p, verbose=verbose, s3_client=s3_client)


# ============================================================
# Grouping & Filtering
# ============================================================

def group_queries(queries: List[QueryInfo]) -> Dict[str, QueryGroup]:
    """Group queries by their normalized form."""
    groups: Dict[str, QueryGroup] = {}
    for q in queries:
        key = q.normalized_query
        if key not in groups:
            groups[key] = QueryGroup(normalized_query=key)
        groups[key].queries.append(q)
    return groups


def apply_filters(queries: List[QueryInfo], args: argparse.Namespace) -> List[QueryInfo]:
    """Apply all CLI-specified filters to the query list."""
    result = queries

    if getattr(args, "user", None):
        # In newer ClickHouse log formats the user field is not logged on the query
        # start line so it falls back to "unknown". We also check client_host so
        # that --user=127.0.0.1 (or an IP) still works as a host filter.
        def _user_match(q: QueryInfo, target: str) -> bool:
            return q.user == target or q.client_host == target
        result = [q for q in result if _user_match(q, args.user)]

    if getattr(args, "user_filter", None):
        try:
            pat = re.compile(args.user_filter, re.IGNORECASE)
            result = [q for q in result if pat.search(q.user)]
        except re.error as e:
            print(f"[!] --user-filter regex error: {e}", file=sys.stderr)

    if getattr(args, "host", None):
        result = [q for q in result if q.client_host == args.host]

    if getattr(args, "database", None):
        result = [q for q in result if args.database in q.databases]

    if getattr(args, "from_time", None):
        try:
            dt = datetime.datetime.fromisoformat(args.from_time)
            result = [q for q in result if q.timestamp and q.timestamp >= dt]
        except ValueError:
            print("[!] --from-time format invalid, ignoring.", file=sys.stderr)

    if getattr(args, "to_time", None):
        try:
            dt = datetime.datetime.fromisoformat(args.to_time)
            result = [q for q in result if q.timestamp and q.timestamp <= dt]
        except ValueError:
            print("[!] --to-time format invalid, ignoring.", file=sys.stderr)

    if getattr(args, "min_duration", None) is not None:
        result = [q for q in result if q.duration_sec >= args.min_duration]

    if getattr(args, "max_duration", None) is not None:
        result = [q for q in result if q.duration_sec <= args.max_duration]

    if getattr(args, "errors_only", False):
        result = [q for q in result if q.has_error]

    if getattr(args, "completed_only", False):
        result = [q for q in result if q.completed]

    if getattr(args, "query_filter", None):
        try:
            pat = re.compile(args.query_filter, re.IGNORECASE)
            result = [q for q in result if pat.search(q.query)]
        except re.error as e:
            print(f"[!] --query-filter regex error: {e}", file=sys.stderr)

    return result


# ============================================================
# Report Generator
# ============================================================

class ReportGenerator:
    """Renders query group statistics in multiple output formats."""

    def __init__(
        self,
        groups: List[QueryGroup],
        all_queries: List[QueryInfo],
        files: List[str],
        args: argparse.Namespace,
    ) -> None:
        self.groups = groups
        self.all_queries = all_queries
        self.files = files
        self.args = args

    def _overall(self) -> Dict[str, Any]:
        """Compute overall statistics across all filtered queries."""
        qs = self.all_queries
        ts_list = [q.timestamp for q in qs if q.timestamp]
        span = 0.0
        if len(ts_list) >= 2:
            span = (max(ts_list) - min(ts_list)).total_seconds()
        return {
            "total":   len(qs),
            "unique":  len(set(q.normalized_query for q in qs)),
            "qps":     len(qs) / span if span > 0 else 0.0,
            "ts_from": min(ts_list) if ts_list else None,
            "ts_to":   max(ts_list) if ts_list else None,
            "exec":    compute_stats([q.duration_sec for q in qs]),
            "rows":    compute_stats([float(q.rows_read) for q in qs]),
            "bytes":   compute_stats([q.bytes_read for q in qs]),
            "mem":     compute_stats([q.peak_memory_bytes for q in qs]),
            "errors":  sum(1 for q in qs if q.has_error),
        }

    @staticmethod
    def _histogram(times: List[float], width: int = 50) -> List[Tuple[str, int, str]]:
        """Build bucket counts and ASCII bars for a query time distribution."""
        buckets = [
            ("  1us", 1e-6,   10e-6),
            (" 10us", 10e-6,  100e-6),
            ("100us", 100e-6, 1e-3),
            ("  1ms", 1e-3,   10e-3),
            (" 10ms", 10e-3,  100e-3),
            ("100ms", 100e-3, 1.0),
            ("   1s", 1.0,    10.0),
            (" 10s+", 10.0,   float("inf")),
        ]
        counts = [sum(1 for t in times if lo <= t < hi) for _, lo, hi in buckets]
        mx = max(counts) if counts else 1
        return [
            (lbl, cnt, "#" * int((cnt / mx) * width) if mx else "")
            for (lbl, _, __), cnt in zip(buckets, counts)
        ]

    # ================================================================
    # TEXT REPORT
    # ================================================================
    def generate_text(self) -> str:
        buf = StringIO()
        w = buf.write
        ov = self._overall()
        now = datetime.datetime.now()
        col_w = 9

        def hdr(line: str = ""):
            w(f"# {line}\n")

        hdr(f"Current date: {now}")
        hdr(f"Hostname: {socket.gethostname()}")
        hdr("Files:")
        for f in self.files:
            hdr(f"  * {f}")
        hdr(
            f"Overall: {ov['total']:,}  |  Unique: {ov['unique']:,}  |  "
            f"QPS: {ov['qps']:.2f}  |  Errors: {ov['errors']:,}"
        )
        if ov["ts_from"] and ov["ts_to"]:
            hdr(f"Time range: {ov['ts_from']} to {ov['ts_to']}")
        hdr()

        # Overall stats table
        hdr(
            f"{'Attribute':<16} {'total':>{col_w}} {'min':>{col_w}} {'max':>{col_w}} "
            f"{'avg':>{col_w}} {'95%':>{col_w}} {'stddev':>{col_w}} {'median':>{col_w}}"
        )
        hdr(("=" * 16) + ("  " + "=" * col_w) * 7)

        def stat_row(label: str, s: Dict, formatter) -> None:
            hdr(
                f"{label:<16} {formatter(s['total']):>{col_w}} {formatter(s['min']):>{col_w}} "
                f"{formatter(s['max']):>{col_w}} {formatter(s['avg']):>{col_w}} "
                f"{formatter(s['p95']):>{col_w}} {formatter(s['stddev']):>{col_w}} "
                f"{formatter(s['median']):>{col_w}}"
            )

        stat_row("Exec time",   ov["exec"],  fmt_time)
        stat_row("Rows read",   ov["rows"],  fmt_num)
        stat_row("Bytes read",  ov["bytes"], fmt_bytes)
        stat_row("Peak Memory", ov["mem"],   fmt_bytes)
        hdr()

        # Profile table
        hdr("Profile")
        hdr(f"{'Rank':>4}  {'Response time':>14}  {'Calls':>7}  {'R/Call':>8}  Query")
        hdr(f"{'====':>4}  {'==============':>14}  {'=====':>7}  {'======':>8}  {'='*40}")

        total_exec = ov["exec"]["total"] or 1.0
        for i, grp in enumerate(self.groups, 1):
            et = compute_stats(grp.exec_times)
            pct = (et["max"] / total_exec) * 100
            sample = (grp.queries[0].query if grp.queries else grp.normalized_query)
            sample = sample.replace("\n", " ")[:65]
            hdr(
                f"{i:>4}  {fmt_time(et['max']):>8} {pct:>5.2f}%  "
                f"{grp.count:>7}  {fmt_time(et['avg']):>8}  {sample}"
            )

        w("\n")

        if not getattr(self.args, "no_detail", False):
            for i, grp in enumerate(self.groups, 1):
                self._text_query_detail(buf, i, grp)

        return buf.getvalue()

    def _text_query_detail(self, buf: StringIO, rank: int, grp: QueryGroup) -> None:
        w = buf.write
        col_w = 9
        tr = grp.time_range

        def hdr(line: str = ""):
            w(f"# {line}\n")

        hdr(f"Query {rank}  |  QPS: {grp.qps:.3f}  |  Errors: {grp.error_count}")
        if tr[0] and tr[1]:
            hdr(f"Time range: From {tr[0]} to {tr[1]}")
        hdr("=" * 72)

        hdr(
            f"{'Attribute':<14} {'total':>{col_w}} {'min':>{col_w}} {'max':>{col_w}} "
            f"{'avg':>{col_w}} {'95%':>{col_w}} {'stddev':>{col_w}} {'median':>{col_w}}"
        )
        hdr(("=" * 14) + ("  " + "=" * col_w) * 7)
        hdr(f"{'Count':<14} {fmt_num(grp.count):>{col_w}}")

        def stat_row(label: str, vals: List[float], formatter) -> None:
            s = compute_stats(vals)
            hdr(
                f"{label:<14} {formatter(s['total']):>{col_w}} {formatter(s['min']):>{col_w}} "
                f"{formatter(s['max']):>{col_w}} {formatter(s['avg']):>{col_w}} "
                f"{formatter(s['p95']):>{col_w}} {formatter(s['stddev']):>{col_w}} "
                f"{formatter(s['median']):>{col_w}}"
            )

        stat_row("Exec time",   grp.exec_times,                          fmt_time)
        stat_row("Rows read",   [float(x) for x in grp.rows_read_list],  fmt_num)
        stat_row("Bytes read",  grp.bytes_read_list,                      fmt_bytes)
        stat_row("Peak Memory", grp.peak_memory_list,                     fmt_bytes)
        hdr("=" * 72)

        users_str = "  ".join(f"{u} ({c}/{grp.count})" for u, c in grp.users.items())
        hosts_str = "  ".join(f"{h} ({c}/{grp.count})" for h, c in grp.hosts.items())
        dbs_str   = "  ".join(f"{d} ({c}/{grp.count})" for d, c in grp.databases.items())
        comp, total = grp.completion_rate

        hdr(f"Databases  {dbs_str or '(none)'}")
        hdr(f"Hosts      {hosts_str or '(none)'}")
        hdr(f"Users      {users_str or '(none)'}")
        hdr(f"Completion {comp}/{total}")
        if grp.error_count > 0:
            hdr(f"Errors     {grp.error_count}/{total}")

        hdr("Query_time distribution")
        hdr("=" * 72)
        for lbl, cnt, bar in self._histogram(grp.exec_times):
            hdr(f"{lbl}  {bar}")
        hdr("=" * 72)

        sample = grp.queries[0].query if grp.queries else grp.normalized_query
        hdr("Query")
        w(f"{sample}\n\n")

    # ================================================================
    # MARKDOWN REPORT
    # ================================================================
    def generate_markdown(self) -> str:
        buf = StringIO()
        w = buf.write
        ov = self._overall()

        w("# ClickHouse Query Profiler Report\n\n")
        w(f"**Generated:** {datetime.datetime.now()}  \n")
        w(f"**Hostname:** {socket.gethostname()}  \n")
        w(f"**Files:** {', '.join(self.files)}  \n")
        if ov["ts_from"] and ov["ts_to"]:
            w(f"**Time Range:** {ov['ts_from']} to {ov['ts_to']}  \n")
        w("\n---\n\n## Overall Statistics\n\n")
        w("| Metric | Value |\n|--------|-------|\n")
        w(f"| Total Queries | {ov['total']:,} |\n")
        w(f"| Unique Queries | {ov['unique']:,} |\n")
        w(f"| QPS | {ov['qps']:.2f} |\n")
        w(f"| Total Errors | {ov['errors']:,} |\n\n")

        def stat_row(label: str, s: Dict, formatter) -> str:
            return (
                f"| {label} | {formatter(s['total'])} | {formatter(s['min'])} | "
                f"{formatter(s['max'])} | {formatter(s['avg'])} | {formatter(s['p95'])} | "
                f"{formatter(s['stddev'])} | {formatter(s['median'])} |\n"
            )

        w("| Attribute | Total | Min | Max | Avg | 95% | StdDev | Median |\n")
        w("|-----------|-------|-----|-----|-----|-----|--------|--------|\n")
        w(stat_row("Exec Time",   ov["exec"],  fmt_time))
        w(stat_row("Rows Read",   ov["rows"],  fmt_num))
        w(stat_row("Bytes Read",  ov["bytes"], fmt_bytes))
        w(stat_row("Peak Memory", ov["mem"],   fmt_bytes))
        w("\n## Top Queries\n\n")
        w("| Rank | Response time | Calls | R/Call | Query |\n")
        w("|------|--------------|-------|--------|-------|\n")

        total_exec = ov["exec"]["total"] or 1.0
        for i, grp in enumerate(self.groups, 1):
            et = compute_stats(grp.exec_times)
            pct = (et["max"] / total_exec) * 100
            sample = (grp.queries[0].query if grp.queries else grp.normalized_query)
            sample = sample.replace("|", "\\|").replace("\n", " ")[:60]
            w(
                f"| {i} | {fmt_time(et['max'])} ({pct:.1f}%) | {grp.count:,} | "
                f"{fmt_time(et['avg'])} | `{sample}` |\n"
            )

        if not getattr(self.args, "no_detail", False):
            w("\n## Query Details\n\n")
            for i, grp in enumerate(self.groups, 1):
                tr = grp.time_range
                comp, total = grp.completion_rate
                w(
                    f"### Query {i}\n\n"
                    f"**QPS:** {grp.qps:.3f} | **Calls:** {grp.count:,} | "
                    f"**Errors:** {grp.error_count}  \n"
                )
                if tr[0] and tr[1]:
                    w(f"**Time Range:** {tr[0]} to {tr[1]}  \n\n")

                w("| Attribute | Total | Min | Max | Avg | 95% | StdDev | Median |\n")
                w("|-----------|-------|-----|-----|-----|-----|--------|--------|\n")
                w(stat_row("Exec Time",   compute_stats(grp.exec_times), fmt_time))
                w(stat_row("Rows Read",   compute_stats([float(x) for x in grp.rows_read_list]), fmt_num))
                w(stat_row("Bytes Read",  compute_stats(grp.bytes_read_list), fmt_bytes))
                w(stat_row("Peak Memory", compute_stats(grp.peak_memory_list), fmt_bytes))
                w(f"\n**Completion:** {comp}/{total}  \n")
                if grp.users:
                    w(f"**Users:** {', '.join(f'{u} ({c})' for u, c in grp.users.items())}  \n")
                if grp.hosts:
                    w(f"**Hosts:** {', '.join(f'{h} ({c})' for h, c in grp.hosts.items())}  \n")
                sample = grp.queries[0].query if grp.queries else grp.normalized_query
                w(f"\n```sql\n{sample}\n```\n\n")

        return buf.getvalue()

    # ================================================================
    # JSON REPORT
    # ================================================================
    def generate_json(self) -> str:
        ov = self._overall()
        data: Dict[str, Any] = {
            "meta": {
                "generated_at":     datetime.datetime.now().isoformat(),
                "hostname":         socket.gethostname(),
                "files":            self.files,
                "profiler_version": "1.0-python",
            },
            "overall": {
                "total_queries":     ov["total"],
                "unique_queries":    ov["unique"],
                "qps":               round(ov["qps"], 4),
                "error_count":       ov["errors"],
                "time_range": {
                    "from": ov["ts_from"].isoformat() if ov["ts_from"] else None,
                    "to":   ov["ts_to"].isoformat()   if ov["ts_to"]   else None,
                },
                "exec_time_sec":     {k: round(v, 6) for k, v in ov["exec"].items()},
                "rows_read":         {k: round(v, 2) for k, v in ov["rows"].items()},
                "bytes_read":        {k: round(v, 2) for k, v in ov["bytes"].items()},
                "peak_memory_bytes": {k: round(v, 2) for k, v in ov["mem"].items()},
            },
            "queries": [],
        }

        for i, grp in enumerate(self.groups, 1):
            tr = grp.time_range
            comp, total = grp.completion_rate
            data["queries"].append({
                "rank":             i,
                "sample_query":     (grp.queries[0].query if grp.queries else ""),
                "normalized_query": grp.normalized_query,
                "count":            grp.count,
                "qps":              round(grp.qps, 4),
                "completion":       f"{comp}/{total}",
                "error_count":      grp.error_count,
                "time_range": {
                    "from": tr[0].isoformat() if tr[0] else None,
                    "to":   tr[1].isoformat() if tr[1] else None,
                },
                "exec_time_sec":     {k: round(v, 6) for k, v in compute_stats(grp.exec_times).items()},
                "rows_read":         {k: round(v, 2) for k, v in compute_stats([float(x) for x in grp.rows_read_list]).items()},
                "bytes_read":        {k: round(v, 2) for k, v in compute_stats(grp.bytes_read_list).items()},
                "peak_memory_bytes": {k: round(v, 2) for k, v in compute_stats(grp.peak_memory_list).items()},
                "users":     grp.users,
                "hosts":     grp.hosts,
                "databases": grp.databases,
            })

        return json.dumps(data, indent=2, ensure_ascii=False)

    # ================================================================
    # CSV REPORT
    # ================================================================
    def generate_csv(self) -> str:
        buf = StringIO()
        wr = csv.writer(buf)
        wr.writerow([
            "Rank", "Count", "QPS",
            "ExecTime_Total_s", "ExecTime_Min_s", "ExecTime_Max_s",
            "ExecTime_Avg_s", "ExecTime_P95_s", "ExecTime_Median_s",
            "RowsRead_Total", "RowsRead_Max", "RowsRead_Avg",
            "BytesRead_Total", "BytesRead_Max", "BytesRead_Avg",
            "PeakMem_Max_bytes", "PeakMem_Avg_bytes",
            "ErrorCount", "Users", "Hosts", "Databases", "Query",
        ])
        for i, grp in enumerate(self.groups, 1):
            et = compute_stats(grp.exec_times)
            rr = compute_stats([float(x) for x in grp.rows_read_list])
            br = compute_stats(grp.bytes_read_list)
            pm = compute_stats(grp.peak_memory_list)
            sample = (grp.queries[0].query if grp.queries else grp.normalized_query)
            wr.writerow([
                i, grp.count, round(grp.qps, 4),
                round(et["total"], 6), round(et["min"], 6), round(et["max"], 6),
                round(et["avg"], 6), round(et["p95"], 6), round(et["median"], 6),
                int(rr["total"]), int(rr["max"]), round(rr["avg"], 2),
                round(br["total"], 2), round(br["max"], 2), round(br["avg"], 2),
                round(pm["max"], 2), round(pm["avg"], 2),
                grp.error_count,
                ";".join(grp.users.keys()),
                ";".join(grp.hosts.keys()),
                ";".join(grp.databases.keys()),
                sample.replace("\n", " "),
            ])
        return buf.getvalue()

    # ================================================================
    # HTML REPORT
    # ================================================================
    def generate_html(self) -> str:
        ov = self._overall()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hostname = socket.gethostname()
        tr_str = (
            f"{ov['ts_from']} to {ov['ts_to']}"
            if ov["ts_from"] and ov["ts_to"] else ""
        )

        total_exec = ov["exec"]["total"] or 1.0
        rows_html = []
        for i, grp in enumerate(self.groups, 1):
            et = compute_stats(grp.exec_times)
            br = compute_stats(grp.bytes_read_list)
            pm = compute_stats(grp.peak_memory_list)
            pct = (et["max"] / total_exec) * 100
            sample = (grp.queries[0].query if grp.queries else grp.normalized_query)
            s_short = sample.replace("<", "&lt;").replace(">", "&gt;")[:80]
            s_full  = sample.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "\\n")
            err_badge = (
                f'<span class="badge-err">{grp.error_count} error(s)</span>'
                if grp.error_count > 0 else "—"
            )
            rows_html.append(
                f"<tr>"
                f"<td class='rank'>{i}</td>"
                f"<td class='qtd'><code title='{s_full}'>{s_short}</code></td>"
                f"<td>{grp.count:,}</td><td>{grp.qps:.2f}</td>"
                f"<td>{fmt_time(et['max'])} <span class='pct'>({pct:.1f}%)</span></td>"
                f"<td>{fmt_time(et['avg'])}</td><td>{fmt_time(et['total'])}</td>"
                f"<td>{fmt_bytes(br['max'])}</td><td>{fmt_bytes(pm['max'])}</td>"
                f"<td>{err_badge}</td></tr>"
            )

        detail_html = []
        if not getattr(self.args, "no_detail", False):
            for i, grp in enumerate(self.groups, 1):
                et = compute_stats(grp.exec_times)
                rr = compute_stats([float(x) for x in grp.rows_read_list])
                br = compute_stats(grp.bytes_read_list)
                pm = compute_stats(grp.peak_memory_list)
                tr = grp.time_range
                comp, total = grp.completion_rate
                sample_esc = (
                    (grp.queries[0].query if grp.queries else grp.normalized_query)
                    .replace("<", "&lt;").replace(">", "&gt;")
                )
                hist_rows = "".join(
                    f"<tr><td>{lbl}</td>"
                    f"<td><div class='hbar' style='width:{len(bar)*100//50 if bar else 0}%'></div></td>"
                    f"<td>{cnt:,}</td></tr>"
                    for lbl, cnt, bar in self._histogram(grp.exec_times)
                )
                tr_info = (
                    f"<p class='tr-info'>Time range: {tr[0]} to {tr[1]}</p>"
                    if tr[0] else ""
                )

                def trow(label, s, f):
                    return (
                        f"<tr><td>{label}</td>"
                        f"<td>{f(s['total'])}</td><td>{f(s['min'])}</td><td>{f(s['max'])}</td>"
                        f"<td>{f(s['avg'])}</td><td>{f(s['p95'])}</td>"
                        f"<td>{f(s['stddev'])}</td><td>{f(s['median'])}</td></tr>"
                    )

                detail_html.append(f"""
                <details class='qd'>
                  <summary>Query {i} &nbsp;<span class='dim'>({grp.count:,} calls | QPS: {grp.qps:.3f})</span></summary>
                  <div class='db'>
                    {tr_info}
                    <table class='st'>
                      <tr><th>Attribute</th><th>Total</th><th>Min</th><th>Max</th><th>Avg</th><th>95%</th><th>StdDev</th><th>Median</th></tr>
                      {trow('Exec Time', et, fmt_time)}
                      {trow('Rows Read', rr, fmt_num)}
                      {trow('Bytes Read', br, fmt_bytes)}
                      {trow('Peak Memory', pm, fmt_bytes)}
                    </table>
                    <p><b>Completion:</b> {comp}/{total} &nbsp;|&nbsp;
                       <b>Errors:</b> {grp.error_count} &nbsp;|&nbsp;
                       <b>Users:</b> {', '.join(grp.users.keys()) or '—'} &nbsp;|&nbsp;
                       <b>Hosts:</b> {', '.join(grp.hosts.keys()) or '—'}</p>
                    <table class='hist'>{hist_rows}</table>
                    <pre class='sql'>{sample_esc}</pre>
                  </div>
                </details>""")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ClickHouse Query Profiler</title>
<style>
:root{{--bg:#0f172a;--card:#1e293b;--border:#334155;--accent:#60a5fa;--text:#e2e8f0;--dim:#64748b;--red:#f87171;--code:#a5f3fc}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;padding:24px}}
.wrap{{max-width:1400px;margin:0 auto}}
h1{{font-size:1.8rem;color:var(--accent);margin-bottom:4px}}
.sub{{color:var(--dim);font-size:.9rem;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:14px;margin-bottom:28px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px}}
.card .lbl{{color:var(--dim);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
.card .val{{color:var(--accent);font-size:1.6rem;font-weight:700;margin:2px 0}}
.card .sub2{{color:var(--dim);font-size:.78rem}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden;margin-bottom:28px}}
th{{background:#0a1120;color:var(--accent);padding:10px 14px;text-align:left;font-size:.8rem;text-transform:uppercase}}
td{{padding:9px 14px;border-bottom:1px solid var(--border);font-size:.88rem}}
tr:hover td{{background:#1a2a40}}
.rank{{color:var(--dim);width:36px;text-align:center;font-weight:700}}
.qtd{{max-width:400px}}
.qtd code{{font-family:monospace;color:var(--code);font-size:.82rem}}
.pct{{color:var(--dim);font-size:.8rem}}
.badge-err{{background:#450a0a;color:var(--red);padding:2px 8px;border-radius:99px;font-size:.75rem}}
details.qd{{background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:10px}}
summary{{padding:12px 16px;cursor:pointer;font-weight:600;color:var(--accent);list-style:none}}
summary::-webkit-details-marker{{display:none}}
summary::before{{content:"▶ ";font-size:.8em}}
details[open] summary::before{{content:"▼ "}}
.dim{{color:var(--dim);font-weight:400;font-size:.85rem}}
.db{{padding:16px;border-top:1px solid var(--border)}}
.st th,.st td{{font-size:.82rem;padding:6px 10px}}
.st{{margin:12px 0}}
.tr-info{{color:var(--dim);font-size:.82rem;margin-bottom:8px}}
.hist{{width:auto;margin:12px 0}}
.hist td{{padding:3px 8px;font-family:monospace;font-size:.8rem;border:none}}
.hbar{{background:var(--accent);height:12px;border-radius:2px;min-width:2px}}
pre.sql{{background:#0a1120;border:1px solid var(--border);border-radius:6px;padding:12px;font-family:monospace;font-size:.82rem;color:var(--code);white-space:pre-wrap;word-break:break-all;margin-top:12px}}
h2{{color:var(--accent);margin:24px 0 14px;font-size:1.2rem}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔍 ClickHouse Query Profiler</h1>
  <p class="sub">Generated: {now} &nbsp;|&nbsp; Host: {hostname} &nbsp;|&nbsp; Files: {', '.join(self.files)}{(' &nbsp;|&nbsp; ' + tr_str) if tr_str else ''}</p>

  <div class="cards">
    <div class="card"><div class="lbl">Total Queries</div><div class="val">{fmt_num(ov['total'])}</div><div class="sub2">{ov['unique']:,} unique</div></div>
    <div class="card"><div class="lbl">QPS</div><div class="val">{ov['qps']:.2f}</div><div class="sub2">queries / second</div></div>
    <div class="card"><div class="lbl">Max Exec Time</div><div class="val">{fmt_time(ov['exec']['max'])}</div><div class="sub2">avg: {fmt_time(ov['exec']['avg'])}</div></div>
    <div class="card"><div class="lbl">Total Rows Read</div><div class="val">{fmt_num(ov['rows']['total'])}</div><div class="sub2">max: {fmt_num(ov['rows']['max'])}</div></div>
    <div class="card"><div class="lbl">Total Bytes Read</div><div class="val">{fmt_bytes(ov['bytes']['total'])}</div><div class="sub2">max: {fmt_bytes(ov['bytes']['max'])}</div></div>
    <div class="card"><div class="lbl">Peak Memory (max)</div><div class="val">{fmt_bytes(ov['mem']['max'])}</div><div class="sub2">avg: {fmt_bytes(ov['mem']['avg'])}</div></div>
    <div class="card"><div class="lbl">Errors</div><div class="val" style="color:var(--red)">{ov['errors']:,}</div><div class="sub2">total query errors</div></div>
  </div>

  <h2>Top Queries</h2>
  <table>
    <thead><tr><th>#</th><th>Query</th><th>Calls</th><th>QPS</th><th>Max Time</th><th>Avg Time</th><th>Total Time</th><th>Max Bytes</th><th>Max Memory</th><th>Errors</th></tr></thead>
    <tbody>{"".join(rows_html)}</tbody>
  </table>

  <h2>Query Details</h2>
  {"".join(detail_html) if detail_html else "<p style='color:var(--dim)'>Details hidden (--no-detail flag active)</p>"}
</div>
</body>
</html>"""


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clickhouse_profiler",
        description="ClickHouse Log Profiler — analyze ClickHouse server log files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local files
  python clickhouse_profiler.py clickhouse-server.log
  python clickhouse_profiler.py -n 20 -r html -o report.html *.log.gz
  python clickhouse_profiler.py --sort-field=RowsRead --sort-order=desc server.log

  # S3 — single object
  python clickhouse_profiler.py s3://my-bucket/logs/clickhouse-server.log
  python clickhouse_profiler.py s3://my-bucket/logs/clickhouse-server.log.gz -r html -o out.html

  # S3 — entire folder (all objects under the prefix)
  python clickhouse_profiler.py s3://my-bucket/clickhouse/logs/

  # S3 — explicit credentials
  python clickhouse_profiler.py s3://my-bucket/logs/ \\
      --s3-access-key AKIA... --s3-secret-key wJalr...

  # S3 — MinIO or other compatible stores
  python clickhouse_profiler.py s3://my-bucket/logs/ \\
      --s3-endpoint http://minio:9000 --s3-region us-east-1

  # Mix local and S3 files
  python clickhouse_profiler.py server.log s3://my-bucket/archive/server.log.gz
        """,
    )

    p.add_argument("files", nargs="+", metavar="FILE",
                   help="Log file paths or s3:// URLs (.gz and .bz2 supported)")

    g = p.add_argument_group("Output")
    g.add_argument("-n", "--top-query-count", type=int, default=10, metavar="N",
                   help="Number of top queries to display (default: 10)")
    g.add_argument("-r", "--report-type", default="text",
                   choices=["text", "md", "json", "csv", "html"],
                   help="Report format (default: text)")
    g.add_argument("-o", "--output", metavar="FILE",
                   help="Write report to a file instead of stdout")
    g.add_argument("--no-detail", action="store_true",
                   help="Skip the per-query detail section")

    g = p.add_argument_group("Filters")
    g.add_argument("-c", "--minimum-query-call-count", type=int, default=1, metavar="N",
                   help="Minimum executions to include a query group (default: 1)")
    g.add_argument("--user",        metavar="USER",    help="Filter by exact user name")
    g.add_argument("--user-filter", metavar="REGEX",   help="Filter by user name regex")
    g.add_argument("--host",        metavar="HOST",    help="Filter by client host address")
    g.add_argument("--database",    metavar="DB",      help="Filter by database name")
    g.add_argument("--from-time",   metavar="DATETIME",
                   help="Start of time window (ISO: 2024-01-01 00:00:00)")
    g.add_argument("--to-time",     metavar="DATETIME",
                   help="End of time window (ISO: 2024-12-31 23:59:59)")
    g.add_argument("--min-duration", type=float, metavar="SEC",
                   help="Minimum query duration in seconds")
    g.add_argument("--max-duration", type=float, metavar="SEC",
                   help="Maximum query duration in seconds")
    g.add_argument("--errors-only",    action="store_true",
                   help="Show only queries that produced an error")
    g.add_argument("--completed-only", action="store_true",
                   help="Show only successfully completed queries")
    g.add_argument("--query-filter", metavar="REGEX",
                   help="Filter query text by regex pattern")

    g = p.add_argument_group("Sorting")
    g.add_argument("--sort-field", default="ExecTime",
                   choices=["ExecTime", "RowsRead", "BytesRead", "PeakMemory", "QPS", "QueryCount"],
                   help="Field to sort by (default: ExecTime)")
    g.add_argument("--sort-field-operation", default="max",
                   choices=["sum", "min", "max", "avg", "per95", "stdDev", "median"],
                   help="Aggregation for sort field (default: max)")
    g.add_argument("--sort-order", default="desc", choices=["asc", "desc"],
                   help="Sort direction (default: desc)")

    g = p.add_argument_group("Misc")
    g.add_argument("--database-name",    default="clickhouse", metavar="NAME")
    g.add_argument("--database-version", default="0",          metavar="VERSION")
    g.add_argument("--log-level", default="error",
                   choices=["panic", "fatal", "error", "warn", "info", "debug", "trace"],
                   help="Verbosity level (default: error)")
    g.add_argument("--version", action="version", version="clickhouse-profiler 1.1.0 (Python)")

    # S3 options
    g = p.add_argument_group("S3 options  (requires: pip install boto3)")
    g.add_argument("--s3-access-key", metavar="KEY",
                   help="AWS access key ID (overrides env / ~/.aws/credentials)")
    g.add_argument("--s3-secret-key", metavar="SECRET",
                   help="AWS secret access key")
    g.add_argument("--s3-session-token", metavar="TOKEN",
                   help="AWS session token (for temporary credentials)")
    g.add_argument("--s3-endpoint", metavar="URL",
                   help="Custom endpoint URL for MinIO or S3-compatible stores "
                        "(e.g. http://minio:9000)")
    g.add_argument("--s3-region", metavar="REGION", default=None,
                   help="AWS region (default: read from env / config)")

    return p


def main() -> int:
    args = build_parser().parse_args()
    verbose = args.log_level in ("info", "debug", "trace")

    # Build S3 client if any file looks like an S3 URL
    s3_client = None
    if any(f.startswith("s3://") for f in args.files):
        s3_client = _s3_client(
            aws_access_key=getattr(args, "s3_access_key", None),
            aws_secret_key=getattr(args, "s3_secret_key", None),
            aws_session_token=getattr(args, "s3_session_token", None),
            endpoint_url=getattr(args, "s3_endpoint", None),
            region=getattr(args, "s3_region", None),
        )
        if verbose:
            endpoint = getattr(args, "s3_endpoint", None) or "AWS"
            print(f"[*] S3 client initialised ({endpoint})", file=sys.stderr)

    if verbose:
        print(f"[*] Parsing {len(args.files)} path(s)...", file=sys.stderr)

    # 1) Parse
    lp = ClickHouseLogParser()
    lp.parse_files(args.files, verbose=verbose, s3_client=s3_client)

    if verbose:
        print(
            f"[*] {lp.total_lines:,} lines read | "
            f"{len(lp.all_queries):,} queries found | "
            f"{lp.skipped_lines:,} lines skipped",
            file=sys.stderr,
        )

    if not lp.all_queries:
        print(
            "[!] No queries found.\n"
            "    Make sure you are pointing at a valid ClickHouse server log.\n"
            "    Default location: /var/log/clickhouse-server/clickhouse-server.log",
            file=sys.stderr,
        )
        return 1

    # 2) Filter
    filtered = apply_filters(lp.all_queries, args)
    if verbose:
        print(f"[*] After filters: {len(filtered):,} queries", file=sys.stderr)

    # 3) Group
    groups = group_queries(filtered)
    group_list = [g for g in groups.values() if g.count >= args.minimum_query_call_count]

    # 4) Sort
    sort_op = {"stdDev": "stddev"}.get(args.sort_field_operation, args.sort_field_operation)
    group_list.sort(
        key=lambda g: g.get_sort_value(args.sort_field, sort_op),
        reverse=(args.sort_order == "desc"),
    )

    # 5) Limit
    top_groups = group_list[: args.top_query_count]
    if not top_groups:
        print("[!] No query groups matched the given filter criteria.", file=sys.stderr)
        return 1

    # 6) Generate report
    gen = ReportGenerator(groups=top_groups, all_queries=filtered, files=args.files, args=args)
    generators = {
        "text": gen.generate_text,
        "md":   gen.generate_markdown,
        "json": gen.generate_json,
        "csv":  gen.generate_csv,
        "html": gen.generate_html,
    }
    report = generators[args.report_type]()

    # 7) Output
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"[✓] Report saved to: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())