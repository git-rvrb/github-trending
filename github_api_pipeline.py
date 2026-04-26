import requests
import os
import sys
import json
import time
import re
import argparse
import glob
from datetime import datetime, timezone, timedelta
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')
SNAPSHOTS_DIR = os.path.join(BASE_DIR, 'data', 'snapshots')
SILVER_SQL_PATH = os.path.join(BASE_DIR, 'silver_layer.sql')

TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')


# ──────────────────────────────────────────────
# 1. EXTRACT — Pull trending repos from GitHub
# ──────────────────────────────────────────────
def extract_repos(days_back=60, min_stars=100, max_repos=30):
    """Fetch the top trending Python repos created in the last N days."""

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')

    query = f"language:python created:>{cutoff_date} stars:>{min_stars}"
    api_url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page={max_repos}"

    print(f"🔍 Searching for Python repos created after {cutoff_date} with >{min_stars} stars...")

    response = requests.get(api_url)
    response.raise_for_status()
    data = response.json()

    clean_repos = []
    for repo in data['items'][:max_repos]:
        created = datetime.fromisoformat(repo['created_at'].replace('Z', '+00:00'))
        age_days = max((datetime.now(timezone.utc) - created).days, 1)  # Avoid div by zero

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
            'Age_Days': age_days,
            'Stars_Per_Day': round(repo['stargazers_count'] / age_days, 1),
        }
        clean_repos.append(row)

    df = pd.DataFrame(clean_repos)
    print(f"✅ Extracted {len(df)} trending repos (created in last {days_back} days)")
    return df


# ──────────────────────────────────────────────
# 2. SNAPSHOTS — Save & load daily data
# ──────────────────────────────────────────────
def save_snapshot(df):
    """Save today's extracted data as a JSON snapshot."""

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    snapshot_path = os.path.join(SNAPSHOTS_DIR, f'{TODAY}.json')

    snapshot = {
        'date': TODAY,
        'repos': df.to_dict(orient='records'),
    }

    with open(snapshot_path, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print(f"📸 Snapshot saved: {snapshot_path}")
    return snapshot_path


def load_snapshots(days=14):
    """Load recent snapshot files, newest first."""

    if not os.path.exists(SNAPSHOTS_DIR):
        return []

    snapshot_files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, '*.json')), reverse=True)
    snapshots = []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')

    for filepath in snapshot_files:
        filename = os.path.basename(filepath)
        date_str = filename.replace('.json', '')

        if date_str < cutoff:
            break

        with open(filepath, 'r', encoding='utf-8') as f:
            snapshots.append(json.load(f))

    print(f"📂 Loaded {len(snapshots)} snapshots (last {days} days)")
    return snapshots


# ──────────────────────────────────────────────
# 3. VELOCITY — Calculate growth metrics
# ──────────────────────────────────────────────
def calculate_velocity(df, snapshots):
    """Enrich the dataframe with velocity metrics using historical snapshots."""

    if len(snapshots) < 2:
        print("⚠️  Not enough snapshots for velocity calc — using stars_per_day only")
        df['Velocity_7d'] = None
        df['Velocity_Trend'] = '—'
        return df

    # Build lookup: repo_name -> {date: stars}
    history = {}
    for snap in snapshots:
        snap_date = snap['date']
        for repo in snap['repos']:
            name = repo['Repository_Name']
            if name not in history:
                history[name] = {}
            history[name][snap_date] = repo['Stars']

    # Find the snapshot closest to 7 days ago
    dates_available = sorted(set(d for repo_hist in history.values() for d in repo_hist.keys()))
    target_7d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
    target_14d = (datetime.now(timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%d')

    # Find closest available date to 7-day and 14-day targets
    def find_closest_date(target, available):
        before = [d for d in available if d <= target]
        return before[-1] if before else None

    date_7d = find_closest_date(target_7d, dates_available)
    date_14d = find_closest_date(target_14d, dates_available)

    velocities_7d = []
    trends = []

    for _, row in df.iterrows():
        name = row['Repository_Name']
        current_stars = row['Stars']
        repo_hist = history.get(name, {})

        # 7-day velocity
        if date_7d and date_7d in repo_hist:
            v7 = current_stars - repo_hist[date_7d]
        else:
            v7 = None
        velocities_7d.append(v7)

        # Trend: compare this week's velocity to last week's
        if date_7d and date_14d and date_7d in repo_hist and date_14d in repo_hist:
            v_prev_week = repo_hist[date_7d] - repo_hist[date_14d]
            v_this_week = current_stars - repo_hist[date_7d]
            if v_this_week > v_prev_week * 1.1:
                trends.append('🔼 Accelerating')
            elif v_this_week < v_prev_week * 0.9:
                trends.append('🔽 Decelerating')
            else:
                trends.append('➡️ Steady')
        else:
            trends.append('—')

    df['Velocity_7d'] = velocities_7d
    df['Velocity_Trend'] = trends

    print("📊 Velocity metrics calculated")
    return df


def rank_by_composite_score(df):
    """Rank repos using a composite score blending stars and velocity."""

    # Use stars_per_day as the velocity metric (always available)
    # If we have 7d velocity, blend it in
    has_7d = df['Velocity_7d'].notna().any()

    if has_7d:
        # Normalize both metrics to 0-1 range
        max_stars = df['Stars'].max()
        max_v7 = df['Velocity_7d'].dropna().max()

        if max_stars > 0 and max_v7 and max_v7 > 0:
            df['_norm_stars'] = df['Stars'] / max_stars
            df['_norm_velocity'] = df['Velocity_7d'].fillna(0) / max_v7
            df['Composite_Score'] = (0.4 * df['_norm_stars']) + (0.6 * df['_norm_velocity'])
        else:
            df['Composite_Score'] = df['Stars_Per_Day']
    else:
        # Fallback: use stars_per_day as the composite score
        max_spd = df['Stars_Per_Day'].max()
        df['Composite_Score'] = df['Stars_Per_Day'] / max_spd if max_spd > 0 else 0

    df = df.sort_values('Composite_Score', ascending=False).reset_index(drop=True)

    # Clean up temp columns
    df = df.drop(columns=['_norm_stars', '_norm_velocity'], errors='ignore')

    print("🏆 Repos ranked by composite score (stars × velocity)")
    return df


# ──────────────────────────────────────────────
# 4. DIFF DETECTION — What changed since last report?
# ──────────────────────────────────────────────
def detect_changes(current_df, snapshots, top_n=15):
    """Compare current top N against previous week's top N to find new/dropped repos."""

    current_names = set(current_df.head(top_n)['Repository_Name'])

    # Find the snapshot closest to 7 days ago for the "last week" comparison
    target_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')

    previous_names = set()
    for snap in snapshots:
        if snap['date'] <= target_date:
            # Use repo names from this snapshot, sorted by stars to get the top N
            prev_repos = sorted(snap['repos'], key=lambda r: r['Stars'], reverse=True)[:top_n]
            previous_names = set(r['Repository_Name'] for r in prev_repos)
            break

    if not previous_names:
        # No previous data — everything is "new"
        return {
            'new': list(current_names),
            'returning': [],
            'dropped': [],
            'has_previous': False,
        }

    new_repos = current_names - previous_names
    dropped_repos = previous_names - current_names
    returning_repos = current_names & previous_names

    changes = {
        'new': list(new_repos),
        'returning': list(returning_repos),
        'dropped': list(dropped_repos),
        'has_previous': True,
    }

    print(f"🔄 Changes: {len(new_repos)} new, {len(returning_repos)} returning, {len(dropped_repos)} dropped")
    return changes


# ──────────────────────────────────────────────
# 5. LOAD — Bronze Layer (raw data → PostgreSQL)
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
# 6. TRANSFORM — Silver Layer (SQL promotion)
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
# 7. AI ANALYSIS — Gemini evaluates repos
# ──────────────────────────────────────────────
def analyze_with_ai(df, changes=None):
    """Send repos to Gemini for analysis. Only analyze new/changed repos if changes provided."""

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Determine which repos need AI analysis
    if changes and changes['has_previous']:
        # Only analyze new entries + repos that need re-analysis
        repos_to_analyze = df[df['Repository_Name'].isin(changes['new'])]
        repos_context_only = df[~df['Repository_Name'].isin(changes['new'])]
        print(f"🤖 Analyzing {len(repos_to_analyze)} NEW repos (skipping {len(repos_context_only)} returning)")

        if len(repos_to_analyze) == 0:
            print("   No new repos to analyze — generating brief update summaries")
            # Still generate a brief analysis for the full list
            repos_to_analyze = df.head(15)
    else:
        repos_to_analyze = df.head(15)
        print(f"🤖 Analyzing all {len(repos_to_analyze)} repos (first run or no history)")

    # Build prompt
    repos_context = ""
    for _, repo in repos_to_analyze.iterrows():
        velocity_info = ""
        if repo.get('Velocity_7d') is not None:
            velocity_info = f"\n- 7-Day Star Growth: {repo['Velocity_7d']:,.0f} stars"
        if repo.get('Velocity_Trend') and repo['Velocity_Trend'] != '—':
            velocity_info += f" ({repo['Velocity_Trend']})"

        repos_context += f"""
---
**{repo['Repository_Name']}** ({repo['URL']})
- Description: {repo['Description']}
- Stars: {repo['Stars']:,} | Forks: {repo['Forks']:,} | Open Issues: {repo['Open_Issues']}
- Language: {repo['Language']} | Topics: {repo['Topics']}
- Created: {repo['Created_Date'][:10]} | Last Updated: {repo['Last_Updated'][:10]}
- Age: {repo['Age_Days']} days old | Avg Growth: {repo['Stars_Per_Day']:.0f} stars/day{velocity_info}
"""

    prompt = f"""You are a senior developer and tech analyst. Analyze these NEWLY CREATED, fast-rising GitHub repositories.

These repos were all created in the last 60 days — they are emerging projects gaining rapid traction.
Pay attention to how many stars they've gained relative to their age, and their recent velocity trends.

For EACH repo, provide:
1. **Summary** (2-3 sentences): What does this project do? Who is it for?
2. **Verdict**: Is this genuinely innovative, a solid utility, or just riding hype? Be honest and specific.
3. **Signal Rating**: FIRE (genuinely exciting/innovative) | SOLID (solid/useful) | HYPE (mostly riding a trend)
4. **Growth Note** (1 sentence): How impressive is the star growth given the repo's age? Comment on acceleration/deceleration if velocity data is available.

Be concise, opinionated, and insightful. Don't just repeat the description — add real analysis.

Here are the repos:
{repos_context}

Format your response as a JSON array with objects containing these keys:
"repo_name", "summary", "verdict", "signal_rating", "growth_note"

Return ONLY the JSON array, no markdown formatting or code blocks.
"""

    # Model fallback chain — try each model before giving up
    models = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.5-flash-lite']
    ai_response = None
    last_error = None

    for model_name in models:
        print(f"   Trying model: {model_name}")
        success = False
        for attempt in range(3):
            try:
                ai_response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                print(f"   ✅ Success with {model_name}")
                success = True
                break
            except Exception as e:
                last_error = str(e)
                # Try to extract retry delay from the error message
                delay_match = re.search(r"retryDelay.*?'(\d+)s'", last_error)
                if delay_match:
                    wait = int(delay_match.group(1)) + 5
                else:
                    wait = 30 * (attempt + 1)  # 30s, 60s, 90s

                is_capacity = '503' in last_error or 'UNAVAILABLE' in last_error
                is_quota = '429' in last_error or 'RESOURCE_EXHAUSTED' in last_error
                error_type = "capacity" if is_capacity else "quota" if is_quota else "error"

                print(f"   ⚠️  {model_name} attempt {attempt + 1}/3: {error_type}")
                if is_quota:
                    print(f"   → Quota exhausted, skipping to next model")
                    break  # Don't retry this model, try the next one
                if attempt < 2:
                    print(f"   → Retrying in {wait}s...")
                    time.sleep(wait)

        if success:
            break

    if ai_response is None:
        raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

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
# 8. LOAD — Gold Layer (AI summaries → PostgreSQL)
# ──────────────────────────────────────────────
def load_to_gold(ai_results):
    """Store AI analysis results in the gold_ai_summaries table."""

    engine = get_engine()
    gold_df = pd.DataFrame(ai_results)
    gold_df['analysis_date'] = TODAY
    gold_df.to_sql('gold_ai_summaries', engine, if_exists='replace', index=False)
    print("✅ Gold layer loaded")


# ──────────────────────────────────────────────
# 9. REPORT — Generate weekly markdown report
# ──────────────────────────────────────────────
def generate_weekly_report(df, ai_results, changes):
    """Generate a structured weekly report with diff-based sections."""

    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Determine week number for the filename
    week_num = datetime.now(timezone.utc).isocalendar()[1]
    year = datetime.now(timezone.utc).year
    report_path = os.path.join(REPORTS_DIR, f'{TODAY}_weekly_report.md')

    top_15 = df.head(15)

    # Build the AI lookup by repo name
    ai_lookup = {r['repo_name']: r for r in ai_results}

    lines = []

    # ── Header ──
    lines.append(f"# 🚀 GitHub Trending — Week {week_num}, {year}\n")
    lines.append(f"**Top Python Repositories Ranked by Growth Velocity**\n")
    lines.append(f"*Generated automatically by the GitHub Pipeline + Gemini AI*\n")
    lines.append("---\n")

    # ── New This Week ──
    if changes['has_previous'] and changes['new']:
        lines.append("## 🆕 New This Week\n")
        lines.append("*Repos that broke into the Top 15 for the first time.*\n")
        for name in changes['new']:
            repo_row = top_15[top_15['Repository_Name'] == name]
            if repo_row.empty:
                continue
            repo = repo_row.iloc[0]
            ai = ai_lookup.get(name, {})
            signal = ai.get('signal_rating', '—')
            v7 = f"+{repo['Velocity_7d']:,.0f} stars this week" if pd.notna(repo.get('Velocity_7d')) else f"~{repo['Stars_Per_Day']:.0f} stars/day avg"

            lines.append(f"### [{repo['Repository_Name']}]({repo['URL']}) [{signal}]\n")
            lines.append(f"**{repo['Description']}**\n")
            lines.append(f"⭐ {repo['Stars']:,} stars | 📅 {repo['Age_Days']} days old | 📈 {v7}\n")
            summary = ai.get('summary', '')
            if summary:
                lines.append(f"\n> {summary}\n")
            verdict = ai.get('verdict', '')
            if verdict:
                lines.append(f"**Verdict:** {verdict}\n")
            lines.append("")
        lines.append("---\n")

    # ── Fastest Growing ──
    lines.append("## 🔥 Fastest Growing\n")
    lines.append("*Ranked by growth velocity — what's HOT right now.*\n")

    velocity_col = 'Velocity_7d' if top_15['Velocity_7d'].notna().any() else 'Stars_Per_Day'
    velocity_label = '7d Growth' if velocity_col == 'Velocity_7d' else 'Stars/Day'

    fastest = top_15.sort_values(velocity_col, ascending=False, na_position='last').head(5)
    for i, (_, repo) in enumerate(fastest.iterrows(), 1):
        v = repo[velocity_col]
        v_str = f"+{v:,.0f}" if pd.notna(v) else "—"
        trend = repo.get('Velocity_Trend', '')
        trend_str = f" {trend}" if trend and trend != '—' else ''
        lines.append(f"{i}. **{repo['Repository_Name']}** — {v_str} {velocity_label}{trend_str}")
    lines.append("\n---\n")

    # ── Cooling Off ──
    if changes['has_previous'] and changes['dropped']:
        lines.append("## 📉 Dropped Out\n")
        lines.append("*Repos that fell out of the Top 15 this week.*\n")
        for name in changes['dropped']:
            lines.append(f"- ~~{name}~~")
        lines.append("\n---\n")

    # ── Full Leaderboard ──
    lines.append("## 📊 Full Leaderboard\n")

    has_7d = top_15['Velocity_7d'].notna().any()
    if has_7d:
        lines.append("| # | Repository | Stars | Age | Stars/Day | 7d Growth | Trend | Signal |")
        lines.append("|---|-----------|-------|-----|-----------|-----------|-------|--------|")
        for i, (_, repo) in enumerate(top_15.iterrows(), 1):
            ai = ai_lookup.get(repo['Repository_Name'], {})
            signal = ai.get('signal_rating', '—')
            v7 = f"+{repo['Velocity_7d']:,.0f}" if pd.notna(repo.get('Velocity_7d')) else "—"
            trend = repo.get('Velocity_Trend', '—')
            lines.append(
                f"| {i} | [{repo['Repository_Name']}]({repo['URL']}) "
                f"| {repo['Stars']:,} | {repo['Age_Days']}d "
                f"| {repo['Stars_Per_Day']:.0f}/d | {v7} | {trend} | {signal} |"
            )
    else:
        lines.append("| # | Repository | Stars | Age | Stars/Day | Signal |")
        lines.append("|---|-----------|-------|-----|-----------|--------|")
        for i, (_, repo) in enumerate(top_15.iterrows(), 1):
            ai = ai_lookup.get(repo['Repository_Name'], {})
            signal = ai.get('signal_rating', '—')
            lines.append(
                f"| {i} | [{repo['Repository_Name']}]({repo['URL']}) "
                f"| {repo['Stars']:,} | {repo['Age_Days']}d "
                f"| {repo['Stars_Per_Day']:.0f}/d | {signal} |"
            )

    lines.append("\n---\n")

    # ── AI Deep Dive ──
    lines.append("## 🤖 AI Analysis\n")

    for i, (_, repo) in enumerate(top_15.iterrows(), 1):
        ai = ai_lookup.get(repo['Repository_Name'], {})
        summary = ai.get('summary', 'No analysis available.')
        verdict = ai.get('verdict', 'N/A')
        signal = ai.get('signal_rating', '—')
        growth = ai.get('growth_note', '')

        is_new = repo['Repository_Name'] in changes.get('new', [])
        new_badge = " 🆕" if is_new else ""

        lines.append(f"### {i}. {repo['Repository_Name']} [{signal}]{new_badge}\n")
        lines.append(f"**{repo['Description']}**\n")

        metrics = f"{repo['Stars']:,} stars | {repo['Forks']:,} forks | {repo['Age_Days']} days old | {repo['Stars_Per_Day']:.0f} stars/day"
        if pd.notna(repo.get('Velocity_7d')):
            metrics += f" | 7d: +{repo['Velocity_7d']:,.0f}"
        lines.append(f"{metrics}\n")

        lines.append(f"{repo['URL']}\n")
        lines.append(f"> {summary}\n")
        lines.append(f"**Verdict:** {verdict}\n")
        if growth:
            lines.append(f"📈 *{growth}*\n")
        lines.append("---\n")

    lines.append(f"\n*Report generated on {TODAY} at "
                 f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}*\n")

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"📝 Weekly report saved to {report_path}")
    return report_path


# ──────────────────────────────────────────────
# ORCHESTRATOR — Run the full pipeline
# ──────────────────────────────────────────────
def run_snapshot():
    """Lightweight daily run: extract + save snapshot only."""
    print("=" * 50)
    print("GitHub Trending Pipeline — Snapshot Mode")
    print("=" * 50)

    df = extract_repos()
    save_snapshot(df)

    print("=" * 50)
    print("Snapshot complete!")
    print("=" * 50)


def run_report():
    """Full weekly run: extract + velocity + AI + report."""
    print("=" * 50)
    print("GitHub Trending Pipeline — Full Report Mode")
    print("=" * 50)

    # Step 1: Extract
    df = extract_repos()

    # Step 2: Save today's snapshot
    save_snapshot(df)

    # Step 3: Load historical snapshots & calculate velocity
    snapshots = load_snapshots(days=14)
    df = calculate_velocity(df, snapshots)

    # Step 4: Rank by composite score
    df = rank_by_composite_score(df)

    # Step 5: Detect changes from last week
    changes = detect_changes(df, snapshots)

    # Step 6: Load to Bronze + promote to Silver (local DB only)
    if USE_DB:
        try:
            load_to_bronze(df)
            promote_to_silver()
        except Exception as e:
            print(f"⚠️  Database unavailable, skipping DB steps: {e}")
    else:
        print("Skipping DB steps (USE_DB=false)")

    # Step 7: AI Analysis
    ai_results = analyze_with_ai(df.head(15), changes)

    # Step 8: Load AI results to Gold (local DB only)
    if USE_DB:
        try:
            load_to_gold(ai_results)
        except Exception as e:
            print(f"⚠️  Database unavailable, skipping Gold layer: {e}")

    # Step 9: Generate weekly report
    report_path = generate_weekly_report(df, ai_results, changes)

    print("=" * 50)
    print("Pipeline complete!")
    print(f"Report: {report_path}")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description='GitHub Trending Pipeline')
    parser.add_argument(
        '--mode',
        choices=['snapshot', 'report', 'auto'],
        default='auto',
        help='Run mode: snapshot (daily data only), report (full analysis), auto (snapshot on weekdays, report on Monday)'
    )
    args = parser.parse_args()

    mode = args.mode

    if mode == 'auto':
        # Monday (0) = report day, other days = snapshot
        day_of_week = datetime.now(timezone.utc).weekday()
        if day_of_week == 0:  # Monday
            mode = 'report'
            print("📅 Auto mode: Monday detected → running full report")
        else:
            mode = 'snapshot'
            day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][day_of_week]
            print(f"📅 Auto mode: {day_name} detected → running snapshot only")

    if mode == 'snapshot':
        run_snapshot()
    elif mode == 'report':
        run_report()


if __name__ == '__main__':
    main()