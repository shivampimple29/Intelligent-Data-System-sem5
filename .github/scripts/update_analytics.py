#!/usr/bin/env python3
"""
Fully Automated GitHub Repository Analytics Dashboard
Repository: shivampimple29/Intelligent-Data-System-sem5

Premium, minimal, production-ready analytics engine.
Collects official GitHub traffic, community growth, and verified supporters;
maintains version-controlled historical JSON storage; generates theme-adaptive
SVG metric cards & compact trendlines; and safely updates README.md.

Zero external dependencies required (uses standard library only).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Timezone: Indian Standard Time (IST, UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

START_MARKER = "<!-- REPOSITORY_ANALYTICS_START -->"
END_MARKER = "<!-- REPOSITORY_ANALYTICS_END -->"


# -----------------------------------------------------------------------------
# Atomic File Utilities
# -----------------------------------------------------------------------------

def atomic_write_text(filepath: Path, content: str, encoding: str = "utf-8") -> None:
    """Safely write text to file using an atomic replacement."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path = filepath.with_suffix(f".tmp-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        with open(temp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def atomic_write_json(filepath: Path, data: Any, indent: int = 2) -> None:
    """Safely write JSON to file using atomic replacement."""
    content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(filepath, content)


def safe_load_json(filepath: Path, default_factory) -> Any:
    """Load JSON from file. If file does not exist or is corrupted, return default."""
    if not filepath.exists():
        return default_factory()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        sys.stderr.write(f"[WARNING] Failed to parse JSON from {filepath}: {exc}\n")
        backup = filepath.with_name(f"{filepath.name}.corrupt-{int(time.time())}")
        try:
            filepath.rename(backup)
            sys.stderr.write(f"[INFO] Moved corrupted file to {backup}\n")
        except OSError:
            pass
        return default_factory()


# -----------------------------------------------------------------------------
# Date & Timezone Formatting
# -----------------------------------------------------------------------------

def parse_iso_datetime(iso_str: str) -> Optional[datetime]:
    """Parse ISO 8601 string into a UTC datetime object."""
    if not iso_str:
        return None
    cleaned = iso_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def format_ist(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S IST") -> str:
    """Convert a datetime to IST formatted string."""
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST).strftime(fmt)


def get_current_timestamps() -> Tuple[str, str]:
    """Return current timestamp in (ISO UTC, IST formatted string)."""
    now_utc = datetime.now(UTC)
    now_ist_str = format_ist(now_utc)
    return now_utc.isoformat(), now_ist_str


def get_git_credential_token() -> Optional[str]:
    """Retrieve GitHub token from local Git Credential Manager as fallback."""
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            text=True,
            capture_output=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    except Exception:
        pass
    return None


# -----------------------------------------------------------------------------
# GitHub REST API Client
# -----------------------------------------------------------------------------

class GitHubAPIClient:
    """Robust GitHub REST API client with bounded retries and exponential backoff."""

    def __init__(self, traffic_token: Optional[str] = None, fallback_token: Optional[str] = None):
        self.traffic_token = traffic_token or fallback_token
        self.general_token = fallback_token or traffic_token
        self.api_base = "https://api.github.com"
        self.user_agent = "IntelligentDataSystems-AnalyticsBot/2.0"

    def request(
        self,
        endpoint: str,
        token: Optional[str] = None,
        accept: Optional[str] = None,
        max_retries: int = 3,
        backoff_sec: float = 2.0,
    ) -> Tuple[int, Optional[Any], Dict[str, str]]:
        url = endpoint if endpoint.startswith("http") else f"{self.api_base}/{endpoint.lstrip('/')}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)

        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
                    resp_headers = dict(resp.headers.items())
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body) if raw_body else None
                    return status, data, resp_headers
            except urllib.error.HTTPError as exc:
                status = exc.code
                resp_headers = dict(exc.headers.items()) if exc.headers else {}
                try:
                    err_body = exc.read().decode("utf-8")
                    err_json = json.loads(err_body)
                except Exception:
                    err_json = {"raw": str(exc)}

                if status in (401, 403, 404):
                    return status, err_json, resp_headers

                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    retry_after = resp_headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else (backoff_sec * (2 ** (attempt - 1)))
                    sys.stderr.write(
                        f"[HTTP {status}] Retrying {endpoint} in {sleep_time:.1f}s (attempt {attempt}/{max_retries})...\n"
                    )
                    time.sleep(sleep_time)
                    continue

                return status, err_json, resp_headers

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < max_retries:
                    sleep_time = backoff_sec * (2 ** (attempt - 1))
                    sys.stderr.write(
                        f"[Network Error] {exc} for {endpoint}. Retrying in {sleep_time:.1f}s (attempt {attempt}/{max_retries})...\n"
                    )
                    time.sleep(sleep_time)
                    continue
                return 0, {"error": str(exc)}, {}

        return 0, {"error": "Max retries exceeded"}, {}


# -----------------------------------------------------------------------------
# Historical Traffic Storage & Processing
# -----------------------------------------------------------------------------

def initialize_traffic_history() -> Dict[str, Any]:
    """Default structure for traffic-history.json."""
    return {
        "schemaVersion": 1,
        "collectionStartedAt": None,
        "lastSuccessfulUpdate": None,
        "lastUpdateStatus": "uninitialized",
        "notes": (
            "Daily buckets follow GitHub API UTC 24h reporting periods. "
            "Summing daily uniques across multiple days does not equal distinct individuals across the period."
        ),
        "daily": {},
    }


def merge_traffic_data(
    history: Dict[str, Any],
    clones_payload: Optional[Dict[str, Any]],
    views_payload: Optional[Dict[str, Any]],
    collection_time_iso: str,
) -> bool:
    """Merge incoming traffic records into persistent history without overwriting historical dates."""
    if "daily" not in history or not isinstance(history["daily"], dict):
        history["daily"] = {}

    if not history.get("collectionStartedAt"):
        history["collectionStartedAt"] = collection_time_iso

    updated_any = False

    if clones_payload and isinstance(clones_payload.get("clones"), list):
        for item in clones_payload["clones"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]
            if date_key not in history["daily"]:
                history["daily"][date_key] = {
                    "clones": 0,
                    "uniqueCloners": 0,
                    "views": 0,
                    "uniqueVisitors": 0,
                    "collectedAt": collection_time_iso,
                }
            history["daily"][date_key]["clones"] = int(item.get("count", 0))
            history["daily"][date_key]["uniqueCloners"] = int(item.get("uniques", 0))
            history["daily"][date_key]["collectedAt"] = collection_time_iso
            updated_any = True

    if views_payload and isinstance(views_payload.get("views"), list):
        for item in views_payload["views"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]
            if date_key not in history["daily"]:
                history["daily"][date_key] = {
                    "clones": 0,
                    "uniqueCloners": 0,
                    "views": 0,
                    "uniqueVisitors": 0,
                    "collectedAt": collection_time_iso,
                }
            history["daily"][date_key]["views"] = int(item.get("count", 0))
            history["daily"][date_key]["uniqueVisitors"] = int(item.get("uniques", 0))
            history["daily"][date_key]["collectedAt"] = collection_time_iso
            updated_any = True

    if updated_any:
        history["lastSuccessfulUpdate"] = collection_time_iso
        history["lastUpdateStatus"] = "success"

    return updated_any


def compute_traffic_metrics(history: Dict[str, Any]) -> Dict[str, Any]:
    """Compute rolling 5-day metrics, comparisons, and lifetime cumulative totals."""
    daily = history.get("daily", {})
    sorted_dates = sorted(daily.keys())

    cumulative_clones = sum(d.get("clones", 0) for d in daily.values())
    cumulative_views = sum(d.get("views", 0) for d in daily.values())

    current_5_dates = sorted_dates[-5:] if sorted_dates else []
    current_records: List[Dict[str, Any]] = []

    total_5d_clones = 0
    total_5d_views = 0
    sum_unique_cloners = 0
    sum_unique_visitors = 0

    for d in current_5_dates:
        rec = daily[d]
        clones = rec.get("clones", 0)
        u_cloners = rec.get("uniqueCloners", 0)
        views = rec.get("views", 0)
        u_visitors = rec.get("uniqueVisitors", 0)

        total_5d_clones += clones
        total_5d_views += views
        sum_unique_cloners += u_cloners
        sum_unique_visitors += u_visitors

        current_records.append({
            "date": d,
            "clones": clones,
            "uniqueCloners": u_cloners,
            "views": views,
            "uniqueVisitors": u_visitors,
            "status": "Recorded",
        })

    num_days = len(current_records)
    avg_unique_cloners = round(sum_unique_cloners / num_days, 1) if num_days > 0 else 0.0
    avg_unique_visitors = round(sum_unique_visitors / num_days, 1) if num_days > 0 else 0.0

    prev_5_dates = sorted_dates[-10:-5] if len(sorted_dates) >= 10 else []
    prev_5d_clones: Optional[int] = None
    prev_5d_views: Optional[int] = None
    clone_growth_pct: Optional[float] = None
    view_growth_pct: Optional[float] = None
    clone_diff: Optional[int] = None
    view_diff: Optional[int] = None

    if len(prev_5_dates) == 5:
        prev_5d_clones = sum(daily[d].get("clones", 0) for d in prev_5_dates)
        prev_5d_views = sum(daily[d].get("views", 0) for d in prev_5_dates)
        clone_diff = total_5d_clones - prev_5d_clones
        view_diff = total_5d_views - prev_5d_views

        if prev_5d_clones > 0:
            clone_growth_pct = round((clone_diff / prev_5d_clones) * 100, 1)
        if prev_5d_views > 0:
            view_growth_pct = round((view_diff / prev_5d_views) * 100, 1)

    return {
        "availableDaysCount": num_days,
        "isWindowComplete": (num_days >= 5),
        "windowStartDate": current_5_dates[0] if current_5_dates else None,
        "windowEndDate": current_5_dates[-1] if current_5_dates else None,
        "last5DaysRecords": current_records,
        "last5DaysTotalClones": total_5d_clones,
        "last5DaysTotalViews": total_5d_views,
        "last5DaysSumUniqueCloners": sum_unique_cloners,
        "last5DaysSumUniqueVisitors": sum_unique_visitors,
        "last5DaysAvgUniqueClonersPerDay": avg_unique_cloners,
        "last5DaysAvgUniqueVisitorsPerDay": avg_unique_visitors,
        "previous5DaysDates": prev_5_dates,
        "previous5DaysTotalClones": prev_5d_clones,
        "previous5DaysTotalViews": prev_5d_views,
        "cloneDifference": clone_diff,
        "viewDifference": view_diff,
        "cloneGrowthPercent": clone_growth_pct,
        "viewGrowthPercent": view_growth_pct,
        "recordedCumulativeClones": cumulative_clones,
        "recordedCumulativeViews": cumulative_views,
        "collectionStartedAt": history.get("collectionStartedAt"),
        "lastSuccessfulUpdate": history.get("lastSuccessfulUpdate"),
    }


# -----------------------------------------------------------------------------
# Activity History Processing & Deduplication
# -----------------------------------------------------------------------------

def initialize_activity_history() -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "lastUpdated": None,
        "events": [],
    }


def merge_activity_events(
    activity_store: Dict[str, Any],
    new_events: List[Dict[str, Any]],
    max_history: int = 50,
) -> List[Dict[str, Any]]:
    existing_events = activity_store.get("events", [])
    events_map: Dict[str, Dict[str, Any]] = {}

    for ev in existing_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    for ev in new_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    all_events = list(events_map.values())
    all_events.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    all_events = all_events[:max_history]

    activity_store["events"] = all_events
    activity_store["lastUpdated"] = datetime.now(UTC).isoformat()
    return all_events


# -----------------------------------------------------------------------------
# Premium Visualizations (Theme-Adaptive SVG)
# -----------------------------------------------------------------------------

def generate_metrics_card_svg(
    metrics: Dict[str, Any],
    forks_count: int,
    traffic_status: str,
    output_filepath: Path,
) -> None:
    """
    Generate an ultra-crisp, editorial SVG card displaying the 5 core metrics:
    1. Total Clones (recorded cumulative)
    2. Unique Cloners (5-day aggregate)
    3. Total Forks (current total)
    4. Total Views (recorded cumulative)
    5. Unique Visitors (5-day aggregate)
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    has_traffic = (
        metrics.get("recordedCumulativeClones", 0) > 0
        or metrics.get("recordedCumulativeViews", 0) > 0
        or metrics.get("availableDaysCount", 0) > 0
    )

    # Values & subtitles
    if has_traffic:
        clones_val = f"{metrics.get('recordedCumulativeClones', 0):,}"
        clones_sub = "Recorded cumulative"
        clones_class = "metric-val"

        u_cloners_val = f"{metrics.get('last5DaysSumUniqueCloners', 0):,}"
        u_cloners_sub = "5-day aggregate"
        u_cloners_class = "metric-val"

        views_val = f"{metrics.get('recordedCumulativeViews', 0):,}"
        views_sub = "Recorded cumulative"
        views_class = "metric-val"

        u_visitors_val = f"{metrics.get('last5DaysSumUniqueVisitors', 0):,}"
        u_visitors_sub = "5-day aggregate"
        u_visitors_class = "metric-val"
    else:
        clones_val = "—"
        clones_sub = "Collecting data"
        clones_class = "metric-val-dim"

        u_cloners_val = "—"
        u_cloners_sub = "Collecting data"
        u_cloners_class = "metric-val-dim"

        views_val = "—"
        views_sub = "Collecting data"
        views_class = "metric-val-dim"

        u_visitors_val = "—"
        u_visitors_sub = "Collecting data"
        u_visitors_class = "metric-val-dim"

    forks_val = f"{forks_count:,}"
    forks_sub = "Public community"
    forks_class = "metric-val"

    columns = [
        ("TOTAL CLONES", clones_val, clones_sub, clones_class),
        ("UNIQUE CLONERS", u_cloners_val, u_cloners_sub, u_cloners_class),
        ("TOTAL FORKS", forks_val, forks_sub, forks_class),
        ("TOTAL VIEWS", views_val, views_sub, views_class),
        ("UNIQUE VISITORS", u_visitors_val, u_visitors_sub, u_visitors_class),
    ]

    cols_svg = []
    dividers_svg = []
    col_width = 820.0 / len(columns)

    for idx, (label, val, sub, vclass) in enumerate(columns):
        col_center = (idx * col_width) + (col_width / 2.0)
        cols_svg.append(f"""    <g>
      <text x="{col_center:.1f}" y="28" text-anchor="middle" class="metric-label">{label}</text>
      <text x="{col_center:.1f}" y="59" text-anchor="middle" class="{vclass}">{val}</text>
      <text x="{col_center:.1f}" y="79" text-anchor="middle" class="metric-sub">{sub}</text>
    </g>""")
        if idx > 0:
            div_x = idx * col_width
            dividers_svg.append(f'<line x1="{div_x:.1f}" y1="18" x2="{div_x:.1f}" y2="78" class="divider"/>')

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 96" width="100%" height="auto">
  <defs>
    <style>
      .card-bg {{ fill: #ffffff; stroke: #d0d7de; stroke-width: 1; }}
      .divider {{ stroke: #eaecef; stroke-width: 1; }}
      .metric-val {{ font: 700 22px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #1f2328; }}
      .metric-val-dim {{ font: 600 20px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .metric-label {{ font: 600 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; letter-spacing: 0.5px; }}
      .metric-sub {{ font: 400 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      @media (prefers-color-scheme: dark) {{
        .card-bg {{ fill: #0d1117; stroke: #30363d; }}
        .divider {{ stroke: #21262d; }}
        .metric-val {{ fill: #f0f6fc; }}
        .metric-val-dim {{ fill: #6e7681; }}
        .metric-label {{ fill: #8b949e; }}
        .metric-sub {{ fill: #6e7681; }}
      }}
    </style>
  </defs>

  <rect width="820" height="96" rx="8" class="card-bg"/>
  {''.join(dividers_svg)}
  {''.join(cols_svg)}
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


def generate_svg_chart(
    records: List[Dict[str, Any]],
    output_filepath: Path,
    width: int = 820,
    height: int = 125,
) -> None:
    """
    Generate a compact, refined, theme-adaptive 5-day traffic trend visualizer.
    Communicates the relationship between clones and page views with clean restrained styling.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    has_records = len(records) > 0
    max_val = max([r.get("clones", 0) for r in records] + [r.get("views", 0) for r in records] + [0]) if has_records else 0

    elements_svg = []

    if not has_records or max_val == 0:
        # Elegant minimal baseline when awaiting traffic data
        elements_svg.append(
            '<text x="410" y="66" text-anchor="middle" class="empty-text">5-day rolling traffic trendline will display here as daily activity occurs</text>'
        )
    else:
        y_max = max(5, int(math.ceil(max_val * 1.25)))
        plot_bottom = 86.0
        plot_top = 40.0
        avail_h = plot_bottom - plot_top

        n_days = len(records)
        slot_w = 720.0 / n_days
        bar_w = min(16.0, (slot_w - 24.0) / 2.0)

        for i, rec in enumerate(records):
            center_x = 50.0 + (i * slot_w) + (slot_w / 2.0)
            date_str = rec.get("date", "")
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                short_date = dt_obj.strftime("%b %d")
            except Exception:
                short_date = date_str

            clones = rec.get("clones", 0)
            views = rec.get("views", 0)

            v_h = (views / y_max) * avail_h if y_max > 0 else 0
            v_x = center_x - bar_w - 2
            v_y = plot_bottom - v_h

            c_h = (clones / y_max) * avail_h if y_max > 0 else 0
            c_x = center_x + 2
            c_y = plot_bottom - c_h

            # Views bar (Blue)
            elements_svg.append(
                f'<rect x="{v_x:.1f}" y="{v_y:.1f}" width="{bar_w:.1f}" height="{v_h:.1f}" rx="2" fill="#0969da" opacity="0.9">'
                f'<title>{date_str}: {views} views</title></rect>'
            )
            if views > 0:
                elements_svg.append(
                    f'<text x="{v_x + (bar_w/2):.1f}" y="{max(plot_top + 8, v_y - 3):.1f}" '
                    f'class="count-label" text-anchor="middle">{views}</text>'
                )

            # Clones bar (Purple)
            elements_svg.append(
                f'<rect x="{c_x:.1f}" y="{c_y:.1f}" width="{bar_w:.1f}" height="{c_h:.1f}" rx="2" fill="#8250df" opacity="0.9">'
                f'<title>{date_str}: {clones} clones</title></rect>'
            )
            if clones > 0:
                elements_svg.append(
                    f'<text x="{c_x + (bar_w/2):.1f}" y="{max(plot_top + 8, c_y - 3):.1f}" '
                    f'class="count-label" text-anchor="middle">{clones}</text>'
                )

            # Date label below baseline
            elements_svg.append(
                f'<text x="{center_x:.1f}" y="106" class="axis-label" text-anchor="middle">{short_date}</text>'
            )

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto">
  <defs>
    <style>
      .card-bg {{ fill: #ffffff; stroke: #d0d7de; stroke-width: 1; }}
      .title-text {{ font: 600 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; letter-spacing: 0.5px; }}
      .legend-text {{ font: 500 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; }}
      .axis-label {{ font: 500 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .count-label {{ font: 600 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #1f2328; }}
      .empty-text {{ font: 400 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .baseline {{ stroke: #eaecef; stroke-width: 1; }}
      @media (prefers-color-scheme: dark) {{
        .card-bg {{ fill: #0d1117; stroke: #30363d; }}
        .title-text {{ fill: #8b949e; }}
        .legend-text {{ fill: #8b949e; }}
        .axis-label {{ fill: #6e7681; }}
        .count-label {{ fill: #f0f6fc; }}
        .empty-text {{ fill: #6e7681; }}
        .baseline {{ stroke: #21262d; }}
      }}
    </style>
  </defs>

  <rect width="{width}" height="{height}" rx="8" class="card-bg"/>

  <text x="24" y="24" class="title-text">5-DAY TRAFFIC TREND</text>

  <g>
    <circle cx="710" cy="20" r="4" fill="#0969da"/>
    <text x="720" y="24" class="legend-text">Views</text>
    <circle cx="765" cy="20" r="4" fill="#8250df"/>
    <text x="775" y="24" class="legend-text">Clones</text>
  </g>

  <line x1="24" y1="88" x2="796" y2="88" class="baseline"/>

  <g>
    {''.join(elements_svg)}
  </g>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


# -----------------------------------------------------------------------------
# Shields.io Endpoint Badges Generation (Preserved for API consumers)
# -----------------------------------------------------------------------------

def generate_shields_badges(
    metrics: Dict[str, Any],
    shields_dir: Path,
) -> None:
    """Generate Shields.io endpoint badge JSON files for API consumers."""
    shields_dir.mkdir(parents=True, exist_ok=True)

    badges = {
        "total-clones.json": {
            "schemaVersion": 1,
            "label": "total clones",
            "message": str(metrics.get("recordedCumulativeClones", 0)),
            "color": "blue",
        },
        "daily-unique-cloners.json": {
            "schemaVersion": 1,
            "label": "5-day unique cloners",
            "message": str(metrics.get("last5DaysSumUniqueCloners", 0)),
            "color": "informational",
        },
        "total-views.json": {
            "schemaVersion": 1,
            "label": "total views",
            "message": str(metrics.get("recordedCumulativeViews", 0)),
            "color": "brightgreen",
        },
        "daily-unique-visitors.json": {
            "schemaVersion": 1,
            "label": "5-day unique visitors",
            "message": str(metrics.get("last5DaysSumUniqueVisitors", 0)),
            "color": "informational",
        },
        "cumulative-clones.json": {
            "schemaVersion": 1,
            "label": "recorded clones",
            "message": str(metrics.get("recordedCumulativeClones", 0)),
            "color": "blue",
        },
        "cumulative-views.json": {
            "schemaVersion": 1,
            "label": "recorded views",
            "message": str(metrics.get("recordedCumulativeViews", 0)),
            "color": "brightgreen",
        },
    }

    for filename, badge_data in badges.items():
        atomic_write_json(shields_dir / filename, badge_data)


# -----------------------------------------------------------------------------
# Markdown Dashboard Rendering & README Integration
# -----------------------------------------------------------------------------

def render_hall_of_appreciation(supporters: List[Dict[str, str]]) -> str:
    """
    Render the Hall of Appreciation recognition gallery.
    Displays verified public supporters with avatar, username, and role.
    Respects strict privacy: never associates anyone with cloning/viewing activity.
    """
    if not supporters:
        return "<p align=\"center\"><em>Public forks and community stars will be recognized here.</em></p>"

    cells = []
    for s in supporters[:10]:
        u = s.get("username", "")
        p_url = s.get("profileUrl", f"https://github.com/{u}")
        a_url = s.get("avatarUrl", f"https://github.com/{u}.png?size=96")
        role = s.get("role", "Community Supporter")

        cell = (
            f'    <td align="center" width="160">\n'
            f'      <a href="{p_url}">\n'
            f'        <img src="{a_url}" width="48" height="48" alt="{u}" /><br />\n'
            f'        <b>@{u}</b>\n'
            f'      </a><br />\n'
            f'      <sub>{role}</sub>\n'
            f'    </td>'
        )
        cells.append(cell)

    return f'<table align="center">\n  <tr>\n' + "\n".join(cells) + "\n  </tr>\n</table>"


def render_dashboard_markdown(
    repo_name: str,
    default_branch: str,
    traffic_metrics: Dict[str, Any],
    forks_count: int,
    supporters: List[Dict[str, str]],
) -> str:
    """
    Build the refined, minimal public dashboard.
    Shows only the metrics that matter, clean SVG cards, compact trendline,
    and the Hall of Appreciation recognition wall.
    """
    hall_markup = render_hall_of_appreciation(supporters)

    section = f"""{START_MARKER}
### Repository Insights

<p align="center">
  <img src=".github/analytics/metrics.svg" alt="Repository Metrics" width="100%">
</p>

<p align="center">
  <img src=".github/analytics/traffic-trend.svg" alt="5-Day Traffic Trend" width="100%">
</p>

### Hall of Appreciation

{hall_markup}
{END_MARKER}"""

    return section


def update_readme_content(original_readme: str, new_analytics_section: str) -> str:
    """
    Safely replace the analytics section between START_MARKER and END_MARKER.
    Removes any duplicated Copyright section completely (covered by LICENSE).
    Preserves and refines the Support the Project section.
    """
    cleaned = original_readme

    # 1. Remove Copyright section completely if present
    copyright_pattern = re.compile(
        r"## Copyright\s+Copyright © \d{4}.*?(?=(###|<!--|$))",
        re.DOTALL | re.IGNORECASE,
    )
    cleaned = copyright_pattern.sub("", cleaned)

    # Clean up any leftover duplicate copyright statements
    cleaned = re.sub(
        r"## Copyright\s+.*?(?=(###|<!--|$))",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2. Refine Support the Project section
    support_cleaned = (
        "\n\n---\n\n"
        "### Support the Project\n\n"
        "If you find this repository helpful, consider starring the repository "
        "and following [@shivampimple29](https://github.com/shivampimple29) on GitHub."
    )

    # Strip existing Support section and any preceding dividers
    cleaned = re.sub(
        r"(\n\s*---\s*)*\n*###\s*⭐?\s*Support the Project.*",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"(\n\s*---\s*)+\Z", "", cleaned.rstrip())

    # 3. Replace or insert analytics block
    marker_pattern = re.compile(f"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
    if marker_pattern.search(cleaned):
        cleaned = marker_pattern.sub(new_analytics_section, cleaned)
    else:
        # Prepend after title or at top
        if cleaned.strip().startswith("#"):
            lines = cleaned.strip().split("\n")
            title = lines[0]
            rest = "\n".join(lines[1:]).strip()
            cleaned = f"{title}\n\n{new_analytics_section}\n\n{rest}"
        else:
            cleaned = f"# Intelligent Data Systems\n\n{new_analytics_section}\n\n{cleaned}"

    # Ensure clean trailing support section
    cleaned = cleaned.strip()
    if "### Support the Project" not in cleaned:
        cleaned = f"{cleaned}\n\n{support_cleaned}"

    # Normalize double linebreaks
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned) + "\n"
    return cleaned


# -----------------------------------------------------------------------------
# Main Analytics Collection Pipeline
# -----------------------------------------------------------------------------

def run_pipeline() -> int:
    """Execute the full analytics collection and dashboard generation pipeline."""
    workspace_root = Path(os.environ.get("GITHUB_WORKSPACE") or Path(__file__).resolve().parents[2])
    repo_name = os.environ.get("GITHUB_REPOSITORY") or "shivampimple29/Intelligent-Data-System-sem5"
    default_branch = os.environ.get("GITHUB_REF_NAME") or "main"
    if default_branch.startswith("refs/heads/"):
        default_branch = default_branch.replace("refs/heads/", "")

    gh_traffic_token = os.environ.get("GH_TRAFFIC_STATS_TOKEN")
    github_token = os.environ.get("GITHUB_TOKEN")
    if not gh_traffic_token and not github_token:
        cred_token = get_git_credential_token()
        if cred_token:
            gh_traffic_token = cred_token
            github_token = cred_token

    client = GitHubAPIClient(traffic_token=gh_traffic_token, fallback_token=github_token)

    analytics_dir = workspace_root / ".github" / "analytics"
    shields_dir = analytics_dir / "shields"
    readme_path = workspace_root / "README.md"
    traffic_history_file = analytics_dir / "traffic-history.json"
    activity_history_file = analytics_dir / "activity-history.json"
    dashboard_file = analytics_dir / "dashboard.json"
    metrics_svg_file = analytics_dir / "metrics.svg"
    trend_svg_file = analytics_dir / "traffic-trend.svg"
    chart_file = analytics_dir / "traffic-chart.svg"  # Preserved for backward compatibility

    now_utc_iso, now_ist_str = get_current_timestamps()
    print(f"=== Starting Redesigned Analytics Pipeline ===")
    print(f"Repository: {repo_name} (Branch: {default_branch})")
    print(f"Timestamp:  {now_utc_iso} ({now_ist_str})")

    # 1. Fetch Repository Metadata
    print("\n[1/5] Fetching repository metadata...")
    status, repo_data, _ = client.request(f"repos/{repo_name}", token=client.general_token)
    if status != 200 or not repo_data:
        print(f"[ERROR] Failed to fetch repository metadata (Status {status}): {repo_data}")
        repo_data = {}

    forks_count = int(repo_data.get("forks_count", 0))

    # 2. Fetch Traffic Data (Clones & Views)
    print("[2/5] Fetching repository traffic data...")
    traffic_status = "success"
    clones_payload = None
    views_payload = None

    c_status, c_data, _ = client.request(f"repos/{repo_name}/traffic/clones?per=day", token=client.traffic_token)
    if c_status == 200:
        clones_payload = c_data
    elif c_status in (401, 403):
        print(f"[NOTICE] Traffic API requires GH_TRAFFIC_STATS_TOKEN (Status {c_status}). Preserving existing history.")
        traffic_status = "token_required"
    else:
        traffic_status = "error"

    v_status, v_data, _ = client.request(f"repos/{repo_name}/traffic/views?per=day", token=client.traffic_token)
    if v_status == 200:
        views_payload = v_data
    elif v_status in (401, 403):
        traffic_status = "token_required"
    else:
        traffic_status = "error"

    # 3. Update Traffic History
    print("[3/5] Processing traffic history...")
    traffic_history = safe_load_json(traffic_history_file, initialize_traffic_history)
    merge_traffic_data(traffic_history, clones_payload, views_payload, now_utc_iso)
    atomic_write_json(traffic_history_file, traffic_history)

    traffic_metrics = compute_traffic_metrics(traffic_history)

    # 4. Fetch Verified Public Supporters for Hall of Appreciation
    print("[4/5] Identifying verified public supporters...")
    supporters: List[Dict[str, str]] = []
    seen_users = set()

    # Public Forks
    f_status, forks_data, _ = client.request(f"repos/{repo_name}/forks?sort=newest&per_page=30", token=client.general_token)
    if f_status == 200 and isinstance(forks_data, list):
        for fork in forks_data:
            owner = fork.get("owner", {})
            u_login = owner.get("login")
            if u_login and u_login not in seen_users:
                seen_users.add(u_login)
                supporters.append({
                    "username": u_login,
                    "profileUrl": owner.get("html_url", f"https://github.com/{u_login}"),
                    "avatarUrl": f"https://github.com/{u_login}.png?size=96",
                    "role": "Community Fork",
                })

    # Public Stargazers (if authenticated)
    s_status, stars_data, _ = client.request(
        f"repos/{repo_name}/stargazers",
        token=client.general_token,
        accept="application/vnd.github.star+json",
    )
    if s_status == 200 and isinstance(stars_data, list):
        for star in stars_data:
            user = star.get("user", {})
            u_login = user.get("login")
            if u_login and u_login not in seen_users:
                seen_users.add(u_login)
                supporters.append({
                    "username": u_login,
                    "profileUrl": user.get("html_url", f"https://github.com/{u_login}"),
                    "avatarUrl": f"https://github.com/{u_login}.png?size=96",
                    "role": "Repository Supporter",
                })

    # 5. Generate Visualizations, Snapshot & Update README
    print("[5/5] Generating premium visuals and updating README...")
    # Generate Metrics Card SVG
    generate_metrics_card_svg(traffic_metrics, forks_count, traffic_status, metrics_svg_file)

    # Generate Traffic Trend SVGs
    generate_svg_chart(traffic_metrics.get("last5DaysRecords", []), trend_svg_file)
    generate_svg_chart(traffic_metrics.get("last5DaysRecords", []), chart_file)

    # Generate Badges (for API backwards compatibility)
    generate_shields_badges(traffic_metrics, shields_dir)

    # Update Dashboard JSON
    dashboard_snapshot = {
        "repository": repo_name,
        "defaultBranch": default_branch,
        "updatedAtUTC": now_utc_iso,
        "updatedAtIST": now_ist_str,
        "trafficStatus": traffic_status,
        "trafficMetrics": traffic_metrics,
        "forksCount": forks_count,
        "supporters": supporters,
    }
    atomic_write_json(dashboard_file, dashboard_snapshot)

    # Update README
    existing_readme = ""
    if readme_path.exists():
        with open(readme_path, "r", encoding="utf-8") as f:
            existing_readme = f.read()

    new_section = render_dashboard_markdown(
        repo_name=repo_name,
        default_branch=default_branch,
        traffic_metrics=traffic_metrics,
        forks_count=forks_count,
        supporters=supporters,
    )

    updated_readme = update_readme_content(existing_readme, new_section)
    atomic_write_text(readme_path, updated_readme)

    print("\n=== Analytics Pipeline Completed Successfully ===")
    print(f"Metrics: {forks_count} Forks | Clones: {traffic_metrics.get('recordedCumulativeClones', 0)} | Views: {traffic_metrics.get('recordedCumulativeViews', 0)}")
    print(f"Hall of Appreciation: {len(supporters)} verified supporter(s)")

    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline())
