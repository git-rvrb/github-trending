import requests
import os
import sys
import json
import time
import re
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd
from sqlalchemy import create_engine, text
from google import genai

# Fix Windows console encoding for emoji/unicode output
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DB_PASSWORD = os.getenv('DB_PASSWORD')
USE_DB = os.getenv('USE_DB', 'true').lower() == 'true'

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
SILVER_SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'silver_layer.sql')


# ──────────────────────────────────────────────
# 1. EXTRACT — Pull trending repos from GitHub
# ──────────────────────────────────────────────
def extract_repos():
    """Fetch the top 15 most-starred Python repos from the GitHub API."""

    api_url = "https://api.github.com/search/repositories?q=language:python&sort=stars&order=desc"
    response = requests.get(api_url)
    response.raise_for_status()
    data = response.json()

    clean_repos = []
    for repo in data['items'][:15]:
        row = {
            'Repository_Name': repo['name'],
            'Description': repo.get('description', 'No description'),
            'Stars': repo['stargazers_count'],
            'Forks': repo['forks_count'],
            'Language': repo.get('language', 'N/A'),
            'Topics': ', '.join(repo.get('topics', [])),
            'URL': repo['html_url'],
            'Created_Date': repo['created_at'],
            'Last_Updated': repo['updated_at'],
            'Open_Issues': repo.get('open_issues_count', 0),
        }
        clean_repos.append(row)

    df = pd.DataFrame(clean_repos)
    print(f"✅ Extracted {len(df)} repos from GitHub API")
    return df


# ──────────────────────────────────────────────
# 2. LOAD — Bronze Layer (raw data → PostgreSQL)
# ──────────────────────────────────────────────
def get_engine():
    """Create a SQLAlchemy engine for PostgreSQL."""

    db_connection_string = f'postgresql://postgres:{DB_PASSWORD}@localhost:5432/github_db'
    return create_engine(db_connection_string)


def load_to_bronze(df):
    """Load raw extracted data into the bronze_github_repos table."""

    engine = get_engine()
    df.to_sql('bronze_github_repos', engine, if_exists='replace', index=False)
    print("✅ Bronze layer loaded")


# ──────────────────────────────────────────────
# 3. TRANSFORM — Silver Layer (SQL promotion)
# ──────────────────────────────────────────────
def promote_to_silver():
    """Execute silver_layer.sql to transform bronze → silver."""

    engine = get_engine()
    with open(SILVER_SQL_PATH, 'r') as f:
        sql = f.read()

    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()

    print("✅ Silver layer promoted")


# ──────────────────────────────────────────────
# 4. AI ANALYSIS — Gemini evaluates each repo
# ──────────────────────────────────────────────
def analyze_with_ai(df):
    """Send each repo's metadata to Gemini for analysis and summary."""

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build a single prompt with all repos for efficiency
    repos_context = ""
    for _, repo in df.iterrows():
        repos_context += f"""
---
**{repo['Repository_Name']}** ({repo['URL']})
- Description: {repo['Description']}
- Stars: {repo['Stars']:,} | Forks: {repo['Forks']:,} | Open Issues: {repo['Open_Issues']}
- Language: {repo['Language']} | Topics: {repo['Topics']}
- Created: {repo['Created_Date'][:10]} | Last Updated: {repo['Last_Updated'][:10]}
"""

    prompt = f"""You are a senior developer and tech analyst. Analyze these trending GitHub repositories.

For EACH repo, provide:
1. **Summary** (2-3 sentences): What does this project do? Who is it for?
2. **Verdict**: Is this genuinely innovative, a solid utility, or just riding hype? Be honest and specific.
3. **Signal Rating**: FIRE (genuinely exciting/innovative) | SOLID (solid/useful) | HYPE (hype wave)

Be concise, opinionated, and insightful. Don't just repeat the description — add real analysis.

Here are the repos:
{repos_context}

Format your response as a JSON array with objects containing these keys:
"repo_name", "summary", "verdict", "signal_rating"

Return ONLY the JSON array, no markdown formatting or code blocks.
"""

    print("🤖 Asking Gemini to analyze repos...")

    # Retry with adaptive waits — parse retry delay from API error when possible
    ai_response = None
    for attempt in range(4):
        try:
            ai_response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            break
        except Exception as e:
            error_msg = str(e)
            # Try to extract retry delay from the error message
            delay_match = re.search(r"retryDelay.*?'(\d+)s'", error_msg)
            if delay_match:
                wait = int(delay_match.group(1)) + 5  # Add 5s buffer
            else:
                wait = 60 * (attempt + 1)  # Fallback: 60s, 120s, 180s

            print(f"⚠️  Gemini API error (attempt {attempt + 1}/4): rate limited")
            if attempt < 3:
                print(f"   Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Gemini API failed after 4 attempts: {e}")

    # Parse the AI response
    response_text = ai_response.text.strip()

    # Strip markdown code fences if present
    if response_text.startswith('```'):
        lines = response_text.split('\n')
        lines = lines[1:]  # Remove opening ```json or ```
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]  # Remove closing ```
        response_text = '\n'.join(lines)

    ai_results = json.loads(response_text)
    print(f"✅ Gemini analyzed {len(ai_results)} repos")
    return ai_results


# ──────────────────────────────────────────────
# 5. LOAD — Gold Layer (AI summaries → PostgreSQL)
# ──────────────────────────────────────────────
def load_to_gold(ai_results):
    """Store AI analysis results in the gold_ai_summaries table."""

    engine = get_engine()
    gold_df = pd.DataFrame(ai_results)
    gold_df['analysis_date'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    gold_df.to_sql('gold_ai_summaries', engine, if_exists='replace', index=False)
    print("✅ Gold layer loaded")


# ──────────────────────────────────────────────
# 6. REPORT — Generate daily markdown report
# ──────────────────────────────────────────────
def generate_daily_report(df, ai_results):
    """Generate a formatted markdown report combining repo data + AI analysis."""

    os.makedirs(REPORTS_DIR, exist_ok=True)

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    report_path = os.path.join(REPORTS_DIR, f'{today}_trending_report.md')

    # Build the AI lookup by repo name
    ai_lookup = {r['repo_name']: r for r in ai_results}

    lines = []
    lines.append(f"# GitHub Trending Report — {today}\n")
    lines.append(f"**Top 15 Most-Starred Python Repositories**\n")
    lines.append(f"*Generated automatically by the GitHub Pipeline + Gemini AI*\n")
    lines.append("---\n")

    # Summary table
    lines.append("## Overview\n")
    lines.append("| # | Repository | Stars | Forks | Signal |")
    lines.append("|---|-----------|-------|-------|--------|")

    for i, (_, repo) in enumerate(df.iterrows(), 1):
        ai = ai_lookup.get(repo['Repository_Name'], {})
        signal = ai.get('signal_rating', '—')
        lines.append(
            f"| {i} | [{repo['Repository_Name']}]({repo['URL']}) "
            f"| {repo['Stars']:,} | {repo['Forks']:,} | {signal} |"
        )

    lines.append("\n---\n")
    lines.append("## AI Analysis\n")

    for i, (_, repo) in enumerate(df.iterrows(), 1):
        ai = ai_lookup.get(repo['Repository_Name'], {})
        summary = ai.get('summary', 'No analysis available.')
        verdict = ai.get('verdict', 'N/A')
        signal = ai.get('signal_rating', '—')

        lines.append(f"### {i}. {repo['Repository_Name']} [{signal}]\n")
        lines.append(f"**{repo['Description']}**\n")
        lines.append(f"{repo['Stars']:,} stars | {repo['Forks']:,} forks | "
                      f"Created {repo['Created_Date'][:10]}\n")
        lines.append(f"{repo['URL']}\n")
        lines.append(f"> {summary}\n")
        lines.append(f"**Verdict:** {verdict}\n")
        lines.append("---\n")

    lines.append(f"\n*Report generated on {today} at "
                 f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}*\n")

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"📝 Report saved to {report_path}")
    return report_path


# ──────────────────────────────────────────────
# ORCHESTRATOR — Run the full pipeline
# ──────────────────────────────────────────────
def main():
    print("=" * 50)
    print("GitHub Trending Pipeline — Starting")
    print("=" * 50)

    # Step 1: Extract
    df = extract_repos()

    # Step 2-3: Load to Bronze + promote to Silver (local DB only)
    if USE_DB:
        try:
            load_to_bronze(df)
            promote_to_silver()
        except Exception as e:
            print(f"⚠️  Database unavailable, skipping DB steps: {e}")
    else:
        print("Skipping DB steps (USE_DB=false)")

    # Step 4: AI Analysis
    ai_results = analyze_with_ai(df)

    # Step 5: Load AI results to Gold (local DB only)
    if USE_DB:
        try:
            load_to_gold(ai_results)
        except Exception as e:
            print(f"⚠️  Database unavailable, skipping Gold layer: {e}")

    # Step 6: Generate daily report
    report_path = generate_daily_report(df, ai_results)

    print("=" * 50)
    print("Pipeline complete!")
    print(f"Daily report: {report_path}")
    print("=" * 50)


if __name__ == '__main__':
    main()