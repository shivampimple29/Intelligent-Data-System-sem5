# Repository Analytics Setup & Maintenance Guide

This internal operational document explains the configuration, secrets, and architecture of the automated repository analytics pipeline.

---

## 1. Automated Schedule

- **Workflow File:** [`.github/workflows/repository-analytics.yml`](workflows/repository-analytics.yml)
- **Schedule:** `17 1 * * *` (01:17 UTC daily, ~06:47 AM Indian Standard Time).
- **Triggers:**
  - Automated daily cron schedule.
  - Push trigger to `main` (with path filters ignoring `.github/analytics/**` and `README.md` to prevent execution loops).
  - Manual trigger via `workflow_dispatch`.

---

## 2. Authentication & Secrets Configuration

### Background
GitHub's public REST API allows reading repository metadata, forks, stargazers, and commits with the built-in Actions `GITHUB_TOKEN`.

However, GitHub's Traffic API endpoints (`/traffic/clones` and `/traffic/views`) strictly enforce **`Administration: Read`** access on the target repository. GitHub deliberately does not grant administration privileges to the ephemeral `GITHUB_TOKEN`.

### One-Time Setup for Clone & View Tracking
To collect private clone and view counts:
1. Navigate to GitHub: **Settings** → **Developer Settings** → **Personal Access Tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Repository access:** Choose *Only select repositories* → select `Intelligent-Data-System-sem5`.
3. **Repository permissions:** Set **Administration** to **Read-only**. (Optionally, also set **Contents** to **Read-only**).
4. Generate and copy the token.
5. In repository settings: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
6. Set Name: `GH_TRAFFIC_STATS_TOKEN` and paste the token value.

*If the secret is not provided or expires, the pipeline gracefully preserves all historical data and continues updating public metrics without failing.*

---

## 3. Persistent Data Storage Architecture

GitHub's Traffic API only provides a rolling window of ~14 days. To maintain lifetime historical statistics:

- **Traffic Store:** [`.github/analytics/traffic-history.json`](analytics/traffic-history.json)
  - Merges incoming daily records with existing records by stable date key (`YYYY-MM-DD`).
  - Never overwrites historical records outside the 14-day window.
  - Recalculates cumulative recorded clones and views safely.
- **Activity Store:** [`.github/analytics/activity-history.json`](analytics/activity-history.json)
  - Records verifiable public events deduplicated by stable event ID.
- **Dashboard Snapshot:** [`.github/analytics/dashboard.json`](analytics/dashboard.json)
  - Machine-readable snapshot of the latest pipeline state.
- **Visualizations:**
  - [`.github/analytics/metrics.svg`](analytics/metrics.svg): Theme-adaptive 5-metric editorial card.
  - [`.github/analytics/traffic-trend.svg`](analytics/traffic-trend.svg): Compact 5-day traffic trend visualizer.

---

## 4. Privacy & Governance Guarantees

1. GitHub's Traffic API provides aggregate, anonymous counts only. Cloners and viewers are never individually identified.
2. The **Hall of Appreciation** in the public README recognizes only verified public actions (such as public forks and stars) and never implies that someone cloned or viewed the project.
3. Automated commits use `github-actions[bot]` with `[skip ci]` to ensure no recursive workflow runs.
