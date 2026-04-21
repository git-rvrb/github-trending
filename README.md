# GitHub Trending Repositories — AI-Powered Pipeline

## Overview
An end-to-end Data Engineering pipeline that extracts trending Python repositories from the GitHub API, applies a **Medallion Architecture** (Bronze → Silver → Gold), uses **Google Gemini AI** to analyze each repo, and generates a **daily markdown report**.

Runs automatically via **GitHub Actions** on a daily schedule.

## Architecture

```text
GitHub API ──→ Extract ──→ Bronze (raw repos)
                               │
                          Silver (cleaned + ranked)
                               │
                          Gemini AI ──→ Gold (AI summaries)
                               │
                          📝 Daily Report (markdown)
```

## Tech Stack
* **Language:** Python
* **Database:** PostgreSQL (local)
* **AI/LLM:** Google Gemini API (gemini-2.5-flash)
* **Scheduling:** GitHub Actions (daily cron)
* **Libraries:** `pandas`, `requests`, `sqlalchemy`, `google-generativeai`, `python-dotenv`

## Project Structure
```text
github_pipeline/
├── .env                          # Credentials (git-ignored)
├── .gitignore
├── .github/
│   └── workflows/
│       └── daily_pipeline.yml    # Daily GitHub Actions schedule
├── github_api_pipeline.py        # Main pipeline (Extract → AI → Report)
├── silver_layer.sql              # SQL: Bronze → Silver transformation
├── requirements.txt              # Python dependencies
├── reports/                      # Auto-generated daily reports
│   └── YYYY-MM-DD_trending_report.md
└── README.md
```

## Data Pipeline Workflow

### 1. Extract (Python / GitHub API)
Fetches the top 15 most-starred Python repositories with metadata: description, stars, forks, topics, language, and activity dates.

### 2. Bronze Layer (SQLAlchemy → PostgreSQL)
Loads the raw data directly into the `bronze_github_repos` table.

### 3. Silver Layer (SQL Transformation)
Runs `silver_layer.sql` to clean and standardize:
- Type casting (ISO 8601 → DATE)
- Engagement ratio (Stars / Forks)
- Dynamic RANK() window function

### 4. Gold Layer (Gemini AI Analysis)
Sends all repos to Google Gemini for evaluation. Each repo gets:
- **Summary**: What the project does
- **Verdict**: Genuinely innovative, solid utility, or hype
- **Signal Rating**: 🔥 (exciting) | ✅ (solid) | 🌊 (hype)

Results stored in `gold_ai_summaries` table.

### 5. Daily Report
Generates a formatted markdown report in `reports/YYYY-MM-DD_trending_report.md` with an overview table and detailed AI analysis for each repo.

## How to Run

### Prerequisites
- Python 3.11+
- PostgreSQL with a database named `github_db`
- A [Gemini API key](https://aistudio.google.com/apikey)

### Local Setup
```bash
git clone https://github.com/YOUR_USERNAME/github_pipeline.git
cd github_pipeline

pip install -r requirements.txt
```

Create a `.env` file:
```
DB_PASSWORD=your_password
GEMINI_API_KEY=your_gemini_key
```

Run:
```bash
python github_api_pipeline.py
```

### GitHub Actions (Automated Daily Runs)
1. Push the repo to GitHub
2. Go to **Settings → Secrets and variables → Actions**
3. Add a secret: `GEMINI_API_KEY` with your Gemini key
4. The workflow runs daily at 08:00 UTC and commits reports to the `reports/` folder
5. You can also trigger it manually from the **Actions** tab