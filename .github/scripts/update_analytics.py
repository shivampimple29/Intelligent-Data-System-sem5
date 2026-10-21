#!/usr/bin/env python3
"""
Fully Automated GitHub Repository Analytics Dashboard
Repository: shivampimple29/Intelligent-Data-System-sem5

This script collects GitHub repository traffic, community growth, and public
activity, maintains version-controlled historical JSON storage, calculates
rolling 5-day metrics and comparisons, generates Shields.io badge files and
an SVG traffic chart, and safely updates the README.md dashboard section.

Zero external dependencies required (uses standard library only).
"""

from __future__ import annotations

import json
import math
import os
import re
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
        # Backup corrupted file for inspection
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


# -----------------------------------------------------------------------------
# GitHub REST API Client
# -----------------------------------------------------------------------------

class GitHubAPIClient:
    """Robust GitHub REST API client with bounded retries and exponential backoff."""

    def __init__(self, traffic_token: Optional[str] = None, fallback_token: Optional[str] = None):
        self.traffic_token = traffic_token or fallback_token
        self.general_token = fallback_token or traffic_token
        self.api_base = "https://api.github.com"
        self.user_agent = "IntelligentDataSystems-AnalyticsBot/1.0"

    def request(
        self,
        endpoint: str,
        token: Optional[str] = None,
        accept: Optional[str] = None,
        max_retries: int = 3,
        backoff_sec: float = 2.0,
    ) -> Tuple[int, Optional[Any], Dict[str, str]]:
        """
        Perform a GET request to GitHub API.
        Returns: (status_code, json_data, headers_dict)
        """
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

                # Authorization / Forbidden / Not Found should not retry indefinitely
                if status in (401, 403, 404):
                    return status, err_json, resp_headers

                # Rate limiting or transient server error (429, 500, 502, 503, 504)
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
    """
    Merge incoming traffic records into persistent history without overwriting
    historical dates outside GitHub's rolling ~14-day window.
    Returns True if at least one traffic source was merged successfully.
    """
    if "daily" not in history or not isinstance(history["daily"], dict):
        history["daily"] = {}

    if not history.get("collectionStartedAt"):
        history["collectionStartedAt"] = collection_time_iso

    updated_any = False

    # Process Clones
    if clones_payload and isinstance(clones_payload.get("clones"), list):
        for item in clones_payload["clones"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]  # YYYY-MM-DD
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

    # Process Views
    if views_payload and isinstance(views_payload.get("views"), list):
        for item in views_payload["views"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]  # YYYY-MM-DD
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
    """
    Compute rolling 5-day metrics, previous 5-day comparison, and lifetime cumulative totals.
    """
    daily = history.get("daily", {})
    sorted_dates = sorted(daily.keys())

    # Lifetime Cumulative Totals
    cumulative_clones = sum(d.get("clones", 0) for d in daily.values())
    cumulative_views = sum(d.get("views", 0) for d in daily.values())

    # Current rolling 5-day window
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

    # Previous 5-day comparison window (days -10 to -5)
    prev_5_dates = sorted_dates[-10:-5] if len(sorted_dates) >= 10 else []
    prev_records: List[Dict[str, Any]] = []
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

        for d in prev_5_dates:
            prev_records.append({"date": d, **daily[d]})

    return {
        "availableDaysCount": num_days,
        "isWindowComplete": (num_days >= 5),
        "windowStartDate": current_5_dates[0] if current_5_dates else None,
        "windowEndDate": current_5_dates[-1] if current_5_dates else None,
        "last5DaysRecords": current_records,
        "last5DaysTotalClones": total_5d_clones,
        "last5DaysTotalViews": total_5d_views,
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
    """Default structure for activity-history.json."""
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
    """
    Merge incoming verified activity events with existing history using stable IDs.
    Returns the sorted combined events list.
    """
    existing_events = activity_store.get("events", [])
    events_map: Dict[str, Dict[str, Any]] = {}

    # Load existing
    for ev in existing_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    # Insert or update with incoming
    for ev in new_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    # Sort descending by ISO timestamp
    all_events = list(events_map.values())
    all_events.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    all_events = all_events[:max_history]

    activity_store["events"] = all_events
    activity_store["lastUpdated"] = datetime.now(UTC).isoformat()
    return all_events


# -----------------------------------------------------------------------------
# SVG Traffic Chart Generation
# -----------------------------------------------------------------------------

def generate_svg_chart(
    records: List[Dict[str, Any]],
    output_filepath: Path,
    width: int = 760,
    height: int = 280,
) -> None:
    """
    Generate a clean, responsive, dark/light theme friendly SVG chart.
    Displays daily clones and views side by side for each of the rolling 5 days.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    pad_left = 60
    pad_right = 30
    pad_top = 60
    pad_bottom = 50

    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom

    # Find max value for Y-axis scaling
    max_val = 0
    for r in records:
        max_val = max(max_val, r.get("clones", 0), r.get("views", 0))

    # Clean upper bound
    if max_val <= 5:
        y_max = 5
    elif max_val <= 10:
        y_max = 10
    elif max_val <= 25:
        y_max = 25
    elif max_val <= 50:
        y_max = 50
    elif max_val <= 100:
        y_max = 100
    else:
        y_max = int(math.ceil(max_val / 20.0) * 20)

    # Grid lines (0, 50%, 100%)
    grid_steps = [0, y_max / 2, y_max]
    grid_svg = []
    for gval in grid_steps:
        y_pos = pad_top + plot_height - ((gval / y_max) * plot_height)
        glabel = f"{int(gval)}" if gval == int(gval) else f"{gval:.1f}"
        grid_svg.append(
            f'<line x1="{pad_left}" y1="{y_pos:.1f}" x2="{width - pad_right}" y2="{y_pos:.1f}" '
            f'stroke="#e2e8f0" stroke-dasharray="4" stroke-width="1"/>'
        )
        grid_svg.append(
            f'<text x="{pad_left - 10}" y="{y_pos + 4:.1f}" font-size="11" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
            f'fill="#64748b" text-anchor="end">{glabel}</text>'
        )

    # Day columns
    n_days = len(records)
    bars_svg = []
    empty_notice = ""

    if n_days == 0:
        empty_notice = (
            f'<text x="{width / 2}" y="{pad_top + (plot_height / 2)}" text-anchor="middle" '
            f'font-size="14" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
            f'fill="#94a3b8">Awaiting initial traffic data points from GitHub API</text>'
        )
    else:
        slot_width = plot_width / n_days
        bar_width = min(28.0, (slot_width - 24.0) / 2.0)

        for i, rec in enumerate(records):
            slot_center = pad_left + (i * slot_width) + (slot_width / 2.0)
            date_str = rec.get("date", "")
            # Format date as e.g. "Oct 20" or "MM-DD"
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                short_date = dt_obj.strftime("%b %d")
            except Exception:
                short_date = date_str

            clones = rec.get("clones", 0)
            views = rec.get("views", 0)

            # Views bar (Left, Blue)
            v_height = (views / y_max) * plot_height if y_max > 0 else 0
            v_x = slot_center - bar_width - 2
            v_y = pad_top + plot_height - v_height

            # Clones bar (Right, Purple)
            c_height = (clones / y_max) * plot_height if y_max > 0 else 0
            c_x = slot_center + 2
            c_y = pad_top + plot_height - c_height

            # Render Views bar
            bars_svg.append(
                f'<rect x="{v_x:.1f}" y="{v_y:.1f}" width="{bar_width:.1f}" height="{v_height:.1f}" '
                f'rx="3" fill="#2563eb" opacity="0.9">'
                f'<title>{date_str}: {views} views</title></rect>'
            )
            if views > 0:
                bars_svg.append(
                    f'<text x="{v_x + (bar_width / 2):.1f}" y="{max(pad_top + 12, v_y - 4):.1f}" '
                    f'font-size="10" font-weight="600" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
                    f'fill="#1d4ed8" text-anchor="middle">{views}</text>'
                )

            # Render Clones bar
            bars_svg.append(
                f'<rect x="{c_x:.1f}" y="{c_y:.1f}" width="{bar_width:.1f}" height="{c_height:.1f}" '
                f'rx="3" fill="#8b5cf6" opacity="0.9">'
                f'<title>{date_str}: {clones} clones</title></rect>'
            )
            if clones > 0:
                bars_svg.append(
                    f'<text x="{c_x + (bar_width / 2):.1f}" y="{max(pad_top + 12, c_y - 4):.1f}" '
                    f'font-size="10" font-weight="600" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
                    f'fill="#6d28d9" text-anchor="middle">{clones}</text>'
                )

            # Date label below axis
            bars_svg.append(
                f'<text x="{slot_center:.1f}" y="{height - pad_bottom + 22}" font-size="12" '
                f'font-weight="500" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
                f'fill="#475569" text-anchor="middle">{short_date}</text>'
            )

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto">
  <defs>
    <style>
      .title {{ font: 600 15px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; fill: #1e293b; }}
      .subtitle {{ font: 400 11px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; fill: #64748b; }}
      .legend-text {{ font: 500 12px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; fill: #334155; }}
    </style>
  </defs>

  <!-- Card Background -->
  <rect width="{width}" height="{height}" rx="10" fill="#ffffff" stroke="#e2e8f0" stroke-width="1.5"/>

  <!-- Chart Header -->
  <text x="24" y="32" class="title">Daily Traffic Trends (Rolling 5-Day Window)</text>
  <text x="24" y="48" class="subtitle">Official GitHub Traffic API (UTC 24h Buckets)</text>

  <!-- Legend -->
  <g transform="translate({width - 240}, 24)">
    <rect x="0" y="2" width="12" height="12" rx="2" fill="#2563eb"/>
    <text x="18" y="12" class="legend-text">Page Views</text>
    <rect x="105" y="2" width="12" height="12" rx="2" fill="#8b5cf6"/>
    <text x="123" y="12" class="legend-text">Git Clones</text>
  </g>

  <!-- Grid & Axes -->
  <g>
    {''.join(grid_svg)}
    <!-- Base Axis -->
    <line x1="{pad_left}" y1="{pad_top + plot_height}" x2="{width - pad_right}" y2="{pad_top + plot_height}" stroke="#cbd5e1" stroke-width="1.5"/>
  </g>

  <!-- Bars & Labels -->
  <g>
    {''.join(bars_svg)}
    {empty_notice}
  </g>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


# -----------------------------------------------------------------------------
# Shields.io Endpoint Badges Generation
# -----------------------------------------------------------------------------

def generate_shields_badges(
    metrics: Dict[str, Any],
    shields_dir: Path,
) -> None:
    """Generate Shields.io endpoint badge JSON files."""
    shields_dir.mkdir(parents=True, exist_ok=True)

    badges = {
        "total-clones.json": {
            "schemaVersion": 1,
            "label": "5-day clones",
            "message": str(metrics.get("last5DaysTotalClones", 0)),
            "color": "blue",
        },
        "daily-unique-cloners.json": {
            "schemaVersion": 1,
            "label": "avg daily cloners",
            "message": f"{metrics.get('last5DaysAvgUniqueClonersPerDay', 0)}/day",
            "color": "informational",
        },
        "total-views.json": {
            "schemaVersion": 1,
            "label": "5-day views",
            "message": str(metrics.get("last5DaysTotalViews", 0)),
            "color": "brightgreen",
        },
        "daily-unique-visitors.json": {
            "schemaVersion": 1,
            "label": "avg daily visitors",
            "message": f"{metrics.get('last5DaysAvgUniqueVisitorsPerDay', 0)}/day",
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

def render_dashboard_markdown(
    repo_name: str,
    default_branch: str,
    traffic_metrics: Dict[str, Any],
    growth_metrics: Dict[str, Any],
    recent_events: List[Dict[str, Any]],
    update_time_ist: str,
    traffic_status: str,
) -> str:
    """Build the complete GitHub-flavored Markdown analytics section."""
    repo_url = f"https://github.com/{repo_name}"
    raw_base = f"https://raw.githubusercontent.com/{repo_name}/{default_branch}"

    def shield_url(badge_filename: str) -> str:
        endpoint = f"{raw_base}/.github/analytics/shields/{badge_filename}"
        encoded = urllib.parse.quote(endpoint, safe="")
        return f"https://img.shields.io/endpoint?url={encoded}"

    # 5-Day Traffic Table Rows
    table_rows = []
    records = traffic_metrics.get("last5DaysRecords", [])
    if records:
        for r in records:
            d = r.get("date", "N/A")
            c = r.get("clones", 0)
            uc = r.get("uniqueCloners", 0)
            v = r.get("views", 0)
            uv = r.get("uniqueVisitors", 0)
            table_rows.append(f"| {d} | {c:,} | {uc:,} | {v:,} | {uv:,} | Recorded |")
    else:
        table_rows.append("| *No records yet* | 0 | 0 | 0 | 0 | In Progress |")

    # Growth & Comparison Text
    clone_growth = traffic_metrics.get("cloneGrowthPercent")
    view_growth = traffic_metrics.get("viewGrowthPercent")
    prev_clones = traffic_metrics.get("previous5DaysTotalClones")
    prev_views = traffic_metrics.get("previous5DaysTotalViews")

    if clone_growth is not None:
        clone_growth_str = f"{clone_growth:+.1f}% (vs {prev_clones} in prev 5d)"
    elif prev_clones is not None:
        diff = traffic_metrics.get("cloneDifference", 0)
        clone_growth_str = f"{diff:+d} (prev: 0)"
    else:
        clone_growth_str = "Baseline period (accumulating 10-day history)"

    if view_growth is not None:
        view_growth_str = f"{view_growth:+.1f}% (vs {prev_views} in prev 5d)"
    elif prev_views is not None:
        diff = traffic_metrics.get("viewDifference", 0)
        view_growth_str = f"{diff:+d} (prev: 0)"
    else:
        view_growth_str = "Baseline period (accumulating 10-day history)"

    # Activity Feed Rows
    activity_rows = []
    if recent_events:
        for ev in recent_events[:6]:
            act = ev.get("action", "Event")
            actor = ev.get("actor", "user")
            actor_url = ev.get("actorUrl", f"https://github.com/{actor}")
            desc = ev.get("description", "")
            t_ist = ev.get("timestampIST", "N/A")
            activity_rows.append(f"| **{act}** | [{actor}]({actor_url}) | {desc} | {t_ist} |")
    else:
        activity_rows.append("| *No public events recorded* | - | Awaiting initial public events | - |")

    # Window info
    w_start = traffic_metrics.get("windowStartDate") or "N/A"
    w_end = traffic_metrics.get("windowEndDate") or "N/A"
    days_avail = traffic_metrics.get("availableDaysCount", 0)
    window_status_str = f"{w_start} to {w_end} (UTC)" if w_start != "N/A" else "Building history"

    traffic_notice = ""
    if traffic_status == "token_required":
        traffic_notice = (
            "\n> [!NOTE]\n"
            "> **Traffic API Authentication Setup:** GitHub requires `Administration: Read` access for private clone & view statistics. "
            "Please configure the repository secret `GH_TRAFFIC_STATS_TOKEN` with a fine-grained Personal Access Token to enable automatic daily traffic collection.\n"
        )
    elif days_avail < 5:
        traffic_notice = f"\n> [!NOTE]\n> **Rolling Window Status:** Currently tracking {days_avail} of 5 days. Full 5-day comparative trendline is accumulating.\n"

    section = f"""{START_MARKER}
# 📊 Repository Analytics & Traffic Dashboard

> **Automated Insights** for [{repo_name}]({repo_url})  
> *Last Successful Update:* `{update_time_ist}` | *Reporting Window:* `{window_status_str}` ({days_avail}/5 days tracked)
{traffic_notice}
### 🚀 Key Performance Indicators

[![Total Clones]({shield_url("total-clones.json")})](.github/analytics/dashboard.json)
[![Daily Unique Cloners]({shield_url("daily-unique-cloners.json")})](.github/analytics/dashboard.json)
[![Total Views]({shield_url("total-views.json")})](.github/analytics/dashboard.json)
[![Daily Unique Visitors]({shield_url("daily-unique-visitors.json")})](.github/analytics/dashboard.json)
[![Cumulative Clones]({shield_url("cumulative-clones.json")})](.github/analytics/traffic-history.json)
[![Cumulative Views]({shield_url("cumulative-views.json")})](.github/analytics/traffic-history.json)

---

### 📈 Daily Traffic Trends (Rolling 5-Day Window)

| Date (UTC) | Clones | Daily Unique Cloners | Views | Daily Unique Visitors | Status |
|:---|:---:|:---:|:---:|:---:|:---:|
{chr(10).join(table_rows)}

**Period Growth & Cumulative Totals:**
- **5-Day Clones Growth:** `{clone_growth_str}`
- **5-Day Views Growth:** `{view_growth_str}`
- **Recorded Cumulative Clones:** `{traffic_metrics.get('recordedCumulativeClones', 0):,}` events since tracking began
- **Recorded Cumulative Views:** `{traffic_metrics.get('recordedCumulativeViews', 0):,}` page views since tracking began

#### Daily Traffic Visualizer
<p align="center">
  <img src=".github/analytics/traffic-chart.svg" alt="Repository Daily Traffic Chart" width="100%">
</p>

---

### 🌟 Repository Growth & Community Overview

| Stars ⭐ | Forks 🍴 | Open Issues 🎯 | Open PRs 🔀 | Public Forks (Last 5 Days) | Recent Public Stars |
|:---:|:---:|:---:|:---:|:---:|:---:|
| {growth_metrics.get('stars', 0):,} | {growth_metrics.get('forks', 0):,} | {growth_metrics.get('openIssues', 0):,} | {growth_metrics.get('openPullRequests', 0):,} | {growth_metrics.get('publicForksLast5Days', 0):,} | {growth_metrics.get('publicStarsLast5Days', 0):,} |

---

### ⚡ Verifiable Public Activity Feed

| Event | Contributor | Details | Date (IST) |
|:---|:---|:---|:---|
{chr(10).join(activity_rows)}

---

### ℹ️ Data Freshness & Pipeline Governance
- **Execution Schedule:** Automatically updated daily via GitHub Actions at 01:17 UTC (~06:47 AM IST) and upon direct push to `{default_branch}`.
- **Timezone Conventions:** Daily traffic buckets correspond to GitHub API's official UTC 24-hour periods. Timestamps and display dates are presented in Indian Standard Time (`Asia/Kolkata`, UTC+05:30).
- **Metric Definitions:**
  - *Daily Unique Cloners / Visitors:* The count of distinct individual accounts/IPs reported by GitHub for that specific calendar day. GitHub's API specifications state that summing daily uniques across multiple days does **not** represent distinct individuals across the whole period.
  - *Cumulative Totals:* Persistent summation of recorded daily counts since historical collection began.
  - *Privacy Guarantee:* GitHub's traffic endpoints provide strictly aggregate, anonymous counts. Cloners and visitors are never individually identified or correlated with other public activity.
{END_MARKER}"""

    return section


def update_readme_content(original_readme: str, new_analytics_section: str) -> str:
    """
    Safely replace the analytics section between START_MARKER and END_MARKER.
    If markers are missing, insert cleanly before '## Copyright' or at top.
    """
    pattern = re.compile(f"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
    if pattern.search(original_readme):
        return pattern.sub(new_analytics_section, original_readme)

    # Markers not present yet: insert cleanly
    # If "## Copyright" exists, place before it
    if "## Copyright" in original_readme:
        parts = original_readme.split("## Copyright", 1)
        return f"{new_analytics_section}\n\n## Copyright{parts[1]}"

    # Otherwise prepend to top
    return f"{new_analytics_section}\n\n{original_readme}"


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

    client = GitHubAPIClient(traffic_token=gh_traffic_token, fallback_token=github_token)

    analytics_dir = workspace_root / ".github" / "analytics"
    shields_dir = analytics_dir / "shields"
    readme_path = workspace_root / "README.md"
    traffic_history_file = analytics_dir / "traffic-history.json"
    activity_history_file = analytics_dir / "activity-history.json"
    dashboard_file = analytics_dir / "dashboard.json"
    chart_file = analytics_dir / "traffic-chart.svg"

    now_utc_iso, now_ist_str = get_current_timestamps()
    print(f"=== Starting Analytics Pipeline ===")
    print(f"Repository: {repo_name} (Branch: {default_branch})")
    print(f"Timestamp:  {now_utc_iso} ({now_ist_str})")

    # 1. Fetch Repository Metadata
    print("\n[1/6] Fetching repository metadata...")
    status, repo_data, _ = client.request(f"repos/{repo_name}", token=client.general_token)
    if status != 200 or not repo_data:
        print(f"[ERROR] Failed to fetch repository metadata (Status {status}): {repo_data}")
        repo_data = {}

    stars_count = int(repo_data.get("stargazers_count", 0))
    forks_count = int(repo_data.get("forks_count", 0))
    total_open_issues_and_prs = int(repo_data.get("open_issues_count", 0))

    # 2. Fetch Open Pull Requests
    print("[2/6] Fetching open pull requests...")
    status, pulls_data, _ = client.request(f"repos/{repo_name}/pulls?state=open&per_page=100", token=client.general_token)
    open_prs_count = len(pulls_data) if (status == 200 and isinstance(pulls_data, list)) else 0
    actual_open_issues = max(0, total_open_issues_and_prs - open_prs_count)

    # 3. Fetch Traffic Data (Clones & Views)
    print("[3/6] Fetching repository traffic data...")
    traffic_status = "success"
    clones_payload = None
    views_payload = None

    # Clones
    c_status, c_data, _ = client.request(f"repos/{repo_name}/traffic/clones", token=client.traffic_token)
    if c_status == 200:
        clones_payload = c_data
    elif c_status in (401, 403):
        print(f"[WARNING] Clones API returned HTTP {c_status}. Requires Administration: Read permission.")
        traffic_status = "token_required"
    else:
        print(f"[WARNING] Clones API returned HTTP {c_status}: {c_data}")
        traffic_status = "error"

    # Views
    v_status, v_data, _ = client.request(f"repos/{repo_name}/traffic/views", token=client.traffic_token)
    if v_status == 200:
        views_payload = v_data
    elif v_status in (401, 403):
        print(f"[WARNING] Views API returned HTTP {v_status}. Requires Administration: Read permission.")
        traffic_status = "token_required"
    else:
        print(f"[WARNING] Views API returned HTTP {v_status}: {v_data}")
        traffic_status = "error"

    # 4. Update Traffic History
    print("[4/6] Processing traffic history...")
    traffic_history = safe_load_json(traffic_history_file, initialize_traffic_history)
    merge_traffic_data(traffic_history, clones_payload, views_payload, now_utc_iso)
    atomic_write_json(traffic_history_file, traffic_history)

    traffic_metrics = compute_traffic_metrics(traffic_history)

    # 5. Fetch Public Events & Growth Details
    print("[5/6] Fetching public community activity...")
    new_verified_events: List[Dict[str, Any]] = []

    # 5a. Forks
    f_status, forks_data, _ = client.request(f"repos/{repo_name}/forks?sort=newest&per_page=30", token=client.general_token)
    public_forks_last_5d = 0
    five_days_ago_utc = datetime.now(UTC) - timedelta(days=5)

    if f_status == 200 and isinstance(forks_data, list):
        for fork in forks_data:
            created_at = fork.get("created_at")
            dt_created = parse_iso_datetime(created_at)
            if dt_created and dt_created >= five_days_ago_utc:
                public_forks_last_5d += 1

            fid = f"fork-{fork.get('id')}"
            owner_info = fork.get("owner", {})
            owner_login = owner_info.get("login", "unknown")
            fork_full_name = fork.get("full_name", "")
            new_verified_events.append({
                "id": fid,
                "type": "ForkEvent",
                "action": "Public Fork 🍴",
                "actor": owner_login,
                "actorUrl": owner_info.get("html_url", f"https://github.com/{owner_login}"),
                "timestamp": created_at,
                "timestampIST": format_ist(dt_created, "%Y-%m-%d %H:%M IST"),
                "description": f"Forked repository to [{fork_full_name}]({fork.get('html_url')})",
                "targetUrl": fork.get("html_url"),
            })

    # 5b. Stargazers (with timestamps if authenticated)
    s_status, stars_data, _ = client.request(
        f"repos/{repo_name}/stargazers",
        token=client.general_token,
        accept="application/vnd.github.star+json",
    )
    public_stars_last_5d = 0
    if s_status == 200 and isinstance(stars_data, list):
        for star in stars_data:
            starred_at = star.get("starred_at")
            dt_starred = parse_iso_datetime(starred_at)
            if dt_starred and dt_starred >= five_days_ago_utc:
                public_stars_last_5d += 1
            user_info = star.get("user", {})
            u_login = user_info.get("login", "unknown")
            new_verified_events.append({
                "id": f"star-{u_login}",
                "type": "WatchEvent",
                "action": "Public Star ⭐",
                "actor": u_login,
                "actorUrl": user_info.get("html_url", f"https://github.com/{u_login}"),
                "timestamp": starred_at,
                "timestampIST": format_ist(dt_starred, "%Y-%m-%d %H:%M IST"),
                "description": f"Starred [{repo_name}]({repo_data.get('html_url')})",
                "targetUrl": user_info.get("html_url"),
            })

    # 5c. Commits
    cm_status, commits_data, _ = client.request(f"repos/{repo_name}/commits?per_page=10", token=client.general_token)
    if cm_status == 200 and isinstance(commits_data, list):
        for cm in commits_data:
            sha = cm.get("sha", "")
            short_sha = sha[:7]
            commit_info = cm.get("commit", {})
            author_info = cm.get("author") or {}
            login = author_info.get("login") or commit_info.get("author", {}).get("name", "committer")
            actor_url = author_info.get("html_url") or f"https://github.com/{login}"
            date_str = commit_info.get("author", {}).get("date")
            dt_commit = parse_iso_datetime(date_str)
            raw_msg = commit_info.get("message", "").split("\n")[0]
            # Ignore automated analytics commits in the public activity feed
            if "chore(analytics)" in raw_msg:
                continue
            new_verified_events.append({
                "id": f"commit-{sha}",
                "type": "PushEvent",
                "action": "Commit 📦",
                "actor": login,
                "actorUrl": actor_url,
                "timestamp": date_str,
                "timestampIST": format_ist(dt_commit, "%Y-%m-%d %H:%M IST"),
                "description": f"[`{short_sha}`]({cm.get('html_url')}) {raw_msg}",
                "targetUrl": cm.get("html_url"),
            })

    # 5d. Releases
    rel_status, rel_data, _ = client.request(f"repos/{repo_name}/releases?per_page=5", token=client.general_token)
    if rel_status == 200 and isinstance(rel_data, list):
        for rel in rel_data:
            rid = f"release-{rel.get('id')}"
            author_info = rel.get("author", {})
            u_login = author_info.get("login", "releaser")
            published_at = rel.get("published_at")
            dt_rel = parse_iso_datetime(published_at)
            new_verified_events.append({
                "id": rid,
                "type": "ReleaseEvent",
                "action": "Release 🚀",
                "actor": u_login,
                "actorUrl": author_info.get("html_url", f"https://github.com/{u_login}"),
                "timestamp": published_at,
                "timestampIST": format_ist(dt_rel, "%Y-%m-%d %H:%M IST"),
                "description": f"Published release [{rel.get('name') or rel.get('tag_name')}]({rel.get('html_url')})",
                "targetUrl": rel.get("html_url"),
            })

    # Update Activity History
    activity_history = safe_load_json(activity_history_file, initialize_activity_history)
    combined_events = merge_activity_events(activity_history, new_verified_events)
    atomic_write_json(activity_history_file, activity_history)

    # 6. Generate Badges, SVG Chart, Dashboard Snapshot, and Update README
    print("[6/6] Generating visualizations and updating dashboard...")
    growth_metrics = {
        "stars": stars_count,
        "forks": forks_count,
        "openIssues": actual_open_issues,
        "openPullRequests": open_prs_count,
        "publicForksLast5Days": public_forks_last_5d,
        "publicStarsLast5Days": public_stars_last_5d,
    }

    # Generate Badges
    generate_shields_badges(traffic_metrics, shields_dir)

    # Generate Chart
    generate_svg_chart(traffic_metrics.get("last5DaysRecords", []), chart_file)

    # Save Dashboard Snapshot
    dashboard_snapshot = {
        "repository": repo_name,
        "defaultBranch": default_branch,
        "updatedAtUTC": now_utc_iso,
        "updatedAtIST": now_ist_str,
        "trafficStatus": traffic_status,
        "trafficMetrics": traffic_metrics,
        "growthMetrics": growth_metrics,
        "recentEvents": combined_events[:10],
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
        growth_metrics=growth_metrics,
        recent_events=combined_events,
        update_time_ist=now_ist_str,
        traffic_status=traffic_status,
    )

    updated_readme = update_readme_content(existing_readme, new_section)
    atomic_write_text(readme_path, updated_readme)

    print("\n=== Analytics Pipeline Completed Successfully ===")
    print(f"Recorded Clones (5-Day / Lifetime): {traffic_metrics.get('last5DaysTotalClones', 0)} / {traffic_metrics.get('recordedCumulativeClones', 0)}")
    print(f"Recorded Views  (5-Day / Lifetime): {traffic_metrics.get('last5DaysTotalViews', 0)} / {traffic_metrics.get('recordedCumulativeViews', 0)}")
    print(f"Community: {stars_count} Stars, {forks_count} Forks, {actual_open_issues} Open Issues, {open_prs_count} Open PRs")
    print(f"Verified Events Recorded: {len(combined_events)}")

    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline())
