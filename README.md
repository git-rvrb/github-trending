# GitHub Trending — AI-Powered Discovery Pipeline

An automated data pipeline that discovers emerging Python repositories on GitHub, tracks their growth velocity over time, runs AI analysis via Google Gemini, and publishes results to a live dashboard and weekly reports.

**[📊 Live Dashboard](https://git-rvrb.github.io/github-trending/)**

## Architecture

```text
                       ┌─────────────────────────────────────────────────┐
                       │              GitHub Actions (daily cron)        │
                       └──────────────────────┬──────────────────────────┘
                                              │
                ┌─────────────────────────────────────────────────────────┐
                │                    EXTRACT                             │
                │  GitHub Search API → Python repos created <60 days     │
                │  with >100 stars, sorted by stars descending           │
                └──────────────────────┬──────────────────────────────────┘
                                       │
              ┌────────────────────────┴────────────────────────┐
              │                                                 │
    ┌─────────▼──────────┐                          ┌───────────▼──────────┐
    │   SNAPSHOT (JSON)   │                          │   BRONZE (Postgres)  │
    │  data/snapshots/    │                          │  bronze_github_repos │
    │  daily star counts  │                          │  raw extracted data  │
    └─────────┬──────────┘                          └───────────┬──────────┘
              │                                                 │
    ┌─────────▼──────────┐                          ┌───────────▼──────────┐
    │  VELOCITY ENGINE   │                          │   SILVER (Postgres)  │
    │  7d growth calc    │                          │  silver_github_repos │
    │  trend detection   │                          │  cleaned + ranked    │
    │  composite scoring │                          └──────────────────────┘
    └─────────┬──────────┘
              │
    ┌─────────▼──────────┐
    │   DIFF DETECTION   │
    │  new / returning / │
    │  dropped repos     │
    └─────────┬──────────┘
              │
    ┌─────────▼──────────┐
    │   GEMINI AI        │
    │  analysis + signal │
    │  rating per repo   │──────────┐
    └─────────┬──────────┘          │
              │               ┌─────▼──────────┐
    ┌─────────▼──────────┐    │  GOLD (Postgres)│
    │  WEEKLY REPORT     │    │ gold_ai_summaries│
    │  reports/*.md      │    └────────────────┘
    └─────────┬──────────┘
              │
    ┌─────────▼──────────┐
    │  DASHBOARD         │
    │  docs/        │
    │  GitHub Pages      │
    └────────────────────┘
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Database | PostgreSQL (local, optional) |
| AI/LLM | Google Gemini (2.5-flash → 2.0-flash → 2.5-flash-lite fallback chain) |
| Scheduling | GitHub Actions (daily cron @ 08:00 UTC) |
| Dashboard | Vanilla HTML/JS + Chart.js, hosted on GitHub Pages |
| Libraries | `pandas`, `requests`, `sqlalchemy`, `google-genai`, `python-dotenv` |

## Project Structure

```text
github-trending/
├── github_api_pipeline.py        # Main pipeline — all 10 stages
├── silver_layer.sql              # SQL: Bronze → Silver transformation
├── requirements.txt              # Python dependencies
├── .env                          # Credentials (git-ignored)
├── .gitignore
├── .github/
│   └── workflows/
│       └── daily_pipeline.yml    # GitHub Actions — daily schedule
├── data/
│   └── snapshots/                # Daily JSON snapshots (star counts)
│       └── YYYY-MM-DD.json
├── reports/                      # Auto-generated weekly reports
│   ├── YYYY-MM-DD_trending_report.md
│   └── YYYY-MM-DD_weekly_report.md
├── docs/
│   ├── index.html                # Interactive dashboard UI
│   └── data.json                 # Dashboard data (auto-generated)
└── README.md
```

## Pipeline Stages

### 1. Extract
Queries the GitHub Search API for Python repos created in the last 60 days with >100 stars. Calculates `Stars_Per_Day` (total stars ÷ repo age) as an initial growth metric.

### 2. Snapshot
Saves each day's extracted data as a JSON file in `data/snapshots/`. These accumulate over time to build a historical record of star counts.

### 3. Velocity Engine
Loads the last 14 days of snapshots and calculates:
- **7-day velocity**: stars gained in the last 7 days
- **Velocity trend**: comparing this week's growth to last week's (accelerating / steady / decelerating)

### 4. Composite Ranking
Ranks repos using a blended score: 40% normalized star count + 60% normalized 7-day velocity. This surfaces fast-rising newcomers over established repos.

### 5. Diff Detection
Compares today's top 15 against last week's top 15 to identify:
- **New**: repos entering the leaderboard for the first time
- **Returning**: repos that were there last week too
- **Dropped**: repos that fell off

### 6. Bronze Layer (PostgreSQL)
Loads the raw extracted data into `bronze_github_repos`. Skipped when running in CI (`USE_DB=false`).

### 7. Silver Layer (SQL)
Runs `silver_layer.sql` to transform bronze data:
- ISO 8601 → DATE casting
- Engagement ratio (stars / forks)
- `RANK()` window function

### 8. AI Analysis (Gemini)
Sends the top 15 repos to Google Gemini with a structured prompt. Each repo gets:
- **Summary**: what it does and who it's for
- **Verdict**: genuinely innovative, solid utility, or riding hype
- **Signal**: `FIRE` / `SOLID` / `HYPE`
- **Growth note**: commentary on star velocity

Uses a model fallback chain (`gemini-2.5-flash` → `gemini-2.0-flash` → `gemini-2.5-flash-lite`) with retry logic and backoff for reliability.

Only analyzes **new repos** when historical data is available — returning repos use cached analysis.

### 9. Gold Layer (PostgreSQL)
Stores AI analysis results in `gold_ai_summaries`. Skipped in CI.

### 10. Weekly Report & Dashboard
- Generates a structured markdown report with sections for new repos, fastest growing, dropped, full leaderboard, and AI deep dive.
- Exports `data.json` for the interactive dashboard with click-to-expand AI analysis.

## Run Modes

```bash
# Auto mode (default): snapshot on weekdays, full report on Mondays
python github_api_pipeline.py

# Snapshot only: quick daily data collection
python github_api_pipeline.py --mode snapshot

# Full report: extract + velocity + AI + report + dashboard
python github_api_pipeline.py --mode report
```

## Setup

### Prerequisites
- Python 3.11+
- A [Gemini API key](https://aistudio.google.com/apikey)
- PostgreSQL (optional — pipeline works without it)

### Install
```bash
git clone https://github.com/git-rvrb/github-trending.git
cd github-trending
pip install -r requirements.txt
```

### Configure
Create a `.env` file:
```
GEMINI_API_KEY=your_gemini_key
DB_PASSWORD=your_password    # optional
USE_DB=false                 # set to true if using PostgreSQL
```

### GitHub Actions (Automated)
1. Push to GitHub
2. Add `GEMINI_API_KEY` in **Settings → Secrets → Actions**
3. The workflow runs daily at 08:00 UTC
4. Monday = full report + AI analysis, other days = snapshot only
5. Results auto-commit to `data/`, `reports/`, and `docs/`

### Dashboard (GitHub Pages)
1. Go to **Settings → Pages**
2. Set source to **Deploy from a branch** → `main` → `/docs`
3. Dashboard is live at `https://<username>.github.io/github-trending/`