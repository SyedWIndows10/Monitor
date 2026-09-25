# Stock News Monitor (Finnhub free tier -> email alerts)

Checks news for a fixed list of tickers every 15 minutes, keeps only stories
from trusted sources, scores them, confirms with price action, and emails you
when a signal clears the threshold.

**This is informational only — not financial advice. No system guarantees profit.**

---

## 1. Files

| File | Purpose |
|---|---|
| `monitor.py` | Main script: fetch news, filter, score, alert |
| `tickers.csv` | Your watchlist with per-ticker thresholds |
| `sources.yaml` | Trusted-source whitelist and weights |
| `requirements.txt` | Python dependencies |
| `.github/workflows/news_check.yml` | Runs the script every 15 min on GitHub's free servers |
| `news.db` | SQLite database (created automatically on first run) |

---

## 2. One-time setup

### a) Get a Finnhub API key
Sign up free at https://finnhub.io/register and copy your API key.

### b) Create a Gmail App Password (for sending alerts)
1. Turn on 2-Step Verification on the Gmail account you'll send *from*.
2. Go to Google Account → Security → 2-Step Verification → App passwords.
3. Generate a password for "Mail" and copy the 16-character code.
   (This is not your normal Gmail password — don't use that.)

### c) Create a GitHub repo and push these files
```bash
git init
git add .
git commit -m "Initial stock news monitor"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### d) Add secrets to the repo
GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these:

| Secret name | Value |
|---|---|
| `FINNHUB_API_KEY` | Your Finnhub key |
| `EMAIL_ADDRESS` | The Gmail address sending alerts |
| `EMAIL_APP_PASSWORD` | The 16-character app password from step (b) |
| `EMAIL_TO` | Where alerts should be sent (can be the same address) |

---

## 3. Edit your watchlist

Open `tickers.csv` and set your tickers and thresholds:

```csv
symbol,min_score,max_move_pct,notes
AAPL,0.75,3,
NVDA,0.80,4,earnings soon
```

- `min_score`: minimum composite score (0–1) required to send an alert.
- `max_move_pct`: skip the alert if the stock has already moved more than
  this percent today (the news is likely already priced in).

Edit `sources.yaml` if you want to add/remove trusted outlets. Run the script
once and check `news.db` (or add a `print(item["source"])` temporarily) to
confirm the exact source-name strings Finnhub returns for your tickers.

---

## 4. Run it

### Automatically (recommended): GitHub Actions
Already wired up in `.github/workflows/news_check.yml`. Once the secrets are
set and the repo is pushed, it runs automatically every 15 minutes during
`13:00–21:00 UTC, Mon–Fri` (see note on market hours below).

You can also trigger it manually: repo → **Actions** tab → **Stock News
Monitor** → **Run workflow**.

### Manually / locally (for testing)
```bash
pip install -r requirements.txt

export FINNHUB_API_KEY=your_key
export EMAIL_ADDRESS=you@gmail.com
export EMAIL_APP_PASSWORD=your_app_password
export EMAIL_TO=you@gmail.com

python monitor.py
```

---

## 5. About the schedule (cron timing)

GitHub Actions cron runs in **UTC**. The default:
```
*/15 13-21 * * 1-5
```
means every 15 minutes, 13:00–21:00 UTC, Monday–Friday.

US market hours (9:30 AM–4:00 PM ET) map to:
- **13:30–21:00 UTC** during US Daylight Saving Time (roughly March–November)
- **14:30–21:00 UTC** during US Standard Time (roughly November–March)

GitHub doesn't auto-adjust for daylight saving, so update the cron line
twice a year, or widen the window slightly (e.g. `13-22`) to cover both.

---

## 6. Notes and limits

- **Rate limit:** the script pauses ~1.2s between Finnhub calls to stay
  under the free tier's ~50 calls/minute limit.
- **Cooldown:** each ticker will only alert once per 24 hours by default
  (`COOLDOWN_HOURS` in `monitor.py`), to avoid flooding your inbox.
- **Sentiment model:** uses VADER (fast, no download) by default. For
  finance-tuned sentiment, swap in FinBERT — see the comment in
  `score_sentiment()` in `monitor.py`. This adds `transformers` + `torch`
  to `requirements.txt` and increases each run's install time.
- **Database growth:** `news.db` is committed back to the repo after every
  run so state persists between runs. For a private repo this is fine; for
  a public repo, consider moving state to a `.gitignore`d file plus an
  external store instead.
- **GitHub Actions free tier:** 2,000 minutes/month for private repos,
  unlimited for public repos — a 15-minute cron with this script fits
  comfortably.
- **Workflow can be paused by GitHub** if the repo has zero activity for
  60 days. The automatic `news.db` commits reset that clock.

---

## 7. Next steps (optional improvements)

- Add relative-volume confirmation via `yfinance`.
- Add a nightly job that fills an `outcomes` table (return 1 day / 5 days
  after each alert) so you can measure whether alerts are actually useful
  and tune `min_score` per ticker over time.
- Require two independent trusted sources to corroborate a story before
  alerting, for higher-confidence signals.
