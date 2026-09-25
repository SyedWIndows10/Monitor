"""
Ticker-driven news monitor (Finnhub free tier) -> email alerts.

For each ticker in tickers.csv:
  1. Fetch today's company news from Finnhub
  2. Keep only items from whitelisted sources (sources.yaml)
  3. Skip items already seen (SQLite)
  4. Score sentiment + materiality
  5. Confirm with live price move
  6. If composite score clears the per-ticker threshold and the
     ticker isn't in cooldown, send an email alert

Run this on a schedule (GitHub Actions cron or a system crontab).
See README.md for setup.
"""

import os
import csv
import sqlite3
import smtplib
import time
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText

import yaml
import finnhub
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "news.db")
TICKERS_PATH = os.path.join(BASE_DIR, "tickers.csv")
SOURCES_PATH = os.path.join(BASE_DIR, "sources.yaml")

COOLDOWN_HOURS = 24          # minimum gap between alerts for the same ticker
REQUEST_PAUSE_SECONDS = 1.2  # ~50 calls/min, safely under Finnhub's free limit

POSITIVE_KEYWORDS = [
    "upgrade", "beats", "beat estimates", "raises guidance", "raised guidance",
    "approval", "approved", "buyback", "record revenue", "strong demand",
    "price target raised", "outperform",
]
NEGATIVE_KEYWORDS = [
    "downgrade", "misses", "miss estimates", "cuts guidance", "lowered guidance",
    "probe", "investigation", "lawsuit", "recall", "layoffs", "guidance cut",
    "price target lowered", "underperform", "fraud", "delisting",
]
MATERIAL_KEYWORDS = [
    "earnings", "guidance", "acquisition", "merger", "acquire", "fda",
    "regulatory", "lawsuit", "settlement", "recall", "ceo", "cfo resign",
    "bankruptcy", "buyback", "dividend", "spinoff", "sec investigation",
]

analyzer = SentimentIntensityAnalyzer()


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            finnhub_id INTEGER PRIMARY KEY,
            ticker TEXT,
            headline TEXT,
            source TEXT,
            url TEXT,
            published_at TEXT,
            sentiment REAL,
            materiality REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            finnhub_id INTEGER,
            score REAL,
            price_at_alert REAL,
            move_pct REAL,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def load_tickers():
    with open(TICKERS_PATH, newline="") as f:
        return list(csv.DictReader(f))


def load_sources():
    with open(SOURCES_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    weights = {}
    for name in cfg.get("tier1", []):
        weights[name.strip().lower()] = 1.0
    for name in cfg.get("tier2", []):
        weights[name.strip().lower()] = 0.7
    blocked = {name.strip().lower() for name in cfg.get("blocked", [])}
    return weights, blocked


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_sentiment(text: str) -> float:
    """Returns a value roughly in [-1, 1] using VADER (fast, no ML download).

    Swap this out for a FinBERT pipeline if you want finance-tuned sentiment:
        from transformers import pipeline
        finbert = pipeline("text-classification", model="ProsusAI/finbert")
    """
    return analyzer.polarity_scores(text)["compound"]


def score_materiality(text: str) -> float:
    text_lower = text.lower()
    hits = sum(1 for kw in MATERIAL_KEYWORDS if kw in text_lower)
    return min(1.0, hits / 3)  # 3+ material keywords = fully material


def keyword_bias(text: str) -> float:
    """Small nudge toward positive/negative based on explicit event words."""
    text_lower = text.lower()
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)
    if pos == 0 and neg == 0:
        return 0.0
    return (pos - neg) / max(1, pos + neg)


# --------------------------------------------------------------------------
# Core cycle
# --------------------------------------------------------------------------

def already_seen(conn, finnhub_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM news WHERE finnhub_id = ?", (finnhub_id,)
    ).fetchone()
    return row is not None


def in_cooldown(conn, ticker: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM signals WHERE ticker = ? AND created_at > ? LIMIT 1",
        (ticker, cutoff),
    ).fetchone()
    return row is not None


def save_news(conn, item, sentiment, materiality, ticker):
    conn.execute(
        """INSERT OR IGNORE INTO news
           (finnhub_id, ticker, headline, source, url, published_at, sentiment, materiality)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item["id"],
            ticker,
            item.get("headline", ""),
            item.get("source", ""),
            item.get("url", ""),
            datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc).isoformat(),
            sentiment,
            materiality,
        ),
    )
    conn.commit()


def save_signal(conn, ticker, finnhub_id, score, price, move_pct):
    conn.execute(
        """INSERT INTO signals (ticker, finnhub_id, score, price_at_alert, move_pct, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ticker, finnhub_id, score, price, move_pct, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def format_alert(ticker, item, score, quote, move_pct, materiality_score):
    return (
        f"Ticker: {ticker}\n"
        f"Score: {score:.2f}\n"
        f"Source: {item.get('source')}\n"
        f"Headline: {item.get('headline')}\n"
        f"Summary: {item.get('summary', '')}\n"
        f"Price: {quote.get('c')} ({move_pct:+.2f}% today)\n"
        f"Materiality: {materiality_score:.2f}\n"
        f"Link: {item.get('url')}\n\n"
        f"Informational only — not financial advice."
    )


def send_email(subject: str, body: str):
    sender = os.environ["EMAIL_ADDRESS"]
    password = os.environ["EMAIL_APP_PASSWORD"]
    recipient = os.environ["EMAIL_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


def run_cycle():
    client = finnhub.Client(api_key=os.environ["FINNHUB_API_KEY"])
    weights, blocked = load_sources()
    conn = init_db()
    tickers = load_tickers()

    today = date.today().isoformat()

    for row in tickers:
        symbol = row["symbol"].strip().upper()
        min_score = float(row.get("min_score", 0.75))
        max_move_pct = float(row.get("max_move_pct", 5))

        try:
            items = client.company_news(symbol, _from=today, to=today)
        except Exception as e:
            print(f"[{symbol}] fetch failed: {e}")
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        for item in items:
            source_name = (item.get("source") or "").strip().lower()
            if source_name in blocked or source_name not in weights:
                continue
            if already_seen(conn, item["id"]):
                continue

            text = f"{item.get('headline', '')} {item.get('summary', '')}"
            sentiment = score_sentiment(text)
            sentiment = 0.7 * sentiment + 0.3 * keyword_bias(text)
            materiality = score_materiality(text)

            save_news(conn, item, sentiment, materiality, symbol)

            if sentiment <= 0:
                # Only alert on net-positive signals for a "buy" watch use case.
                continue

            if in_cooldown(conn, symbol):
                continue

            try:
                quote = client.quote(symbol)
            except Exception as e:
                print(f"[{symbol}] quote failed: {e}")
                continue

            prev_close = quote.get("pc") or 0
            current = quote.get("c") or 0
            move_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0

            if abs(move_pct) > max_move_pct:
                # Likely already priced in.
                continue

            source_weight = weights.get(source_name, 0)
            score = (0.5 * sentiment * source_weight) + (0.3 * materiality) + 0.2

            if score >= min_score:
                body = format_alert(symbol, item, score, quote, move_pct, materiality)
                subject = f"Signal: {symbol} | score {score:.2f}"
                try:
                    send_email(subject, body)
                    print(f"[{symbol}] alert sent (score {score:.2f})")
                except Exception as e:
                    print(f"[{symbol}] email failed: {e}")
                save_signal(conn, symbol, item["id"], score, current, move_pct)

        time.sleep(REQUEST_PAUSE_SECONDS)

    conn.close()


if __name__ == "__main__":
    run_cycle()
