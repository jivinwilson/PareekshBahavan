# Pareeksha Bhavan Monitor

An automated monitoring system for the [University of Calicut Pareeksha Bhavan](https://pareekshabhavan.uoc.ac.in/) website. It runs every 6 hours via GitHub Actions, detects new Special Examination notifications (including inside PDFs), and immediately alerts you via **Telegram** and **Email**.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Folder Structure](#folder-structure)
4. [Local Setup](#local-setup)
5. [Telegram Setup](#telegram-setup)
6. [Email Setup](#email-setup)
7. [GitHub Secrets](#github-secrets)
8. [GitHub Actions](#github-actions)
9. [Configuring Keywords](#configuring-keywords)
10. [Troubleshooting](#troubleshooting)
11. [Future Improvements](#future-improvements)

---

## Features

- Monitors **Notifications**, **Time Table**, and **Latest News** pages
- Downloads linked PDFs and searches their full text
- Configurable keyword list (Special Examination, CBCSS, B.Sc Computer Science, etc.)
- Duplicate suppression — never sends the same alert twice
- Dual notifications: **Telegram** (primary) + **Email** (secondary)
- Runs every **6 hours** automatically; also supports manual dispatch
- Clean structured logging for easy debugging

---

## Architecture

```
GitHub Actions (cron: every 6 hours)
          │
          ▼
     monitor.py          ← orchestrator / entry point
          │
    ┌─────┴──────────────────────────────────────┐
    │                                            │
  src/config.py          src/store.py           │
  (load settings)        (last_seen.json)       │
    │                                            │
  src/scraper.py ──────► src/pdf.py             │
  (fetch pages)          (download + extract)   │
    │                                            │
  src/matcher.py ◄────── keyword list           │
  (keyword search)                              │
    │                                            │
  src/notifier.py ──────► Telegram + Email      │
    └────────────────────────────────────────────┘
```

Each sub-module has a single responsibility and depends only on the ones above it in the stack.

---

## Folder Structure

```
PareekshaBhavan/
├── .github/
│   └── workflows/
│       └── monitor.yml         # GitHub Actions schedule
├── src/
│   ├── __init__.py
│   ├── config.py               # Pydantic settings (Phase 2)
│   ├── scraper.py              # HTTP scraper (Phase 2)
│   ├── pdf.py                  # PDF download + extraction (Phase 3)
│   ├── matcher.py              # Keyword matching (Phase 3)
│   ├── store.py                # Duplicate tracking (Phase 4)
│   └── notifier.py             # Telegram + email (Phase 4)
├── tests/
│   ├── __init__.py
│   ├── test_scraper.py         # (Phase 2)
│   ├── test_pdf.py             # (Phase 3)
│   ├── test_matcher.py         # (Phase 3)
│   └── test_notifier.py        # (Phase 4)
├── data/
│   ├── last_seen.json          # Persisted notification IDs
│   └── pdfs/                   # Transient PDF downloads (git-ignored)
├── monitor.py                  # Entry point
├── requirements.txt
├── .env.example                # Template — copy to .env locally
├── .gitignore
└── README.md
```

---

## Local Setup

### Prerequisites

- Python 3.12+
- Git

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/jivinwilson/PareekshBahavan.git
cd PareekshBahavan

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your local environment file
cp .env.example .env
# → edit .env and fill in your BOT_TOKEN, CHAT_ID, email credentials, etc.

# 5. Run the monitor once
python monitor.py
```

---

## Telegram Setup

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts to create a bot.
3. Copy the **HTTP API token** — this is your `BOT_TOKEN`.
4. Start a conversation with your bot (send it any message).
5. Visit `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` in your browser.
6. Find `"chat":{"id":...}` in the response — this is your `CHAT_ID`.
7. Add both values to `.env` (locally) and to GitHub Secrets (for CI).

---

## Email Setup

The monitor uses SMTP with STARTTLS. For Gmail:

1. Enable **2-Step Verification** on your Google account.
2. Go to **Google Account → Security → App Passwords**.
3. Create an App Password for "Mail".
4. Use `smtp.gmail.com`, port `587`, your Gmail address, and the App Password.

For other providers, adjust `EMAIL_HOST` and `EMAIL_PORT` accordingly.

---

## GitHub Secrets

Navigate to your repository → **Settings → Secrets and variables → Actions → New repository secret** and add each of the following:

| Secret name       | Description                             |
|-------------------|-----------------------------------------|
| `BOT_TOKEN`       | Telegram bot HTTP API token             |
| `CHAT_ID`         | Telegram chat/user ID                   |
| `EMAIL_HOST`      | SMTP server hostname (e.g. smtp.gmail.com) |
| `EMAIL_PORT`      | SMTP port (587 for STARTTLS)            |
| `EMAIL_USERNAME`  | SMTP login / sender address             |
| `EMAIL_PASSWORD`  | SMTP password or App Password           |
| `EMAIL_TO`        | Recipient address for alerts            |

> **Never** put real credentials in `.env.example`, `monitor.yml`, or any committed file.

---

## GitHub Actions

The workflow file is at `.github/workflows/monitor.yml`.

| Trigger | Schedule |
|---------|----------|
| Automatic | Every 6 hours (`0 */6 * * *`) |
| Manual | **Actions → Run workflow** button |

The workflow:
1. Checks out the repository (including `data/last_seen.json`)
2. Installs Python 3.12 and dependencies
3. Runs `python monitor.py`
4. Commits and pushes any changes to `data/last_seen.json` back to `main` (to persist seen-state between runs)

---

## Configuring Keywords

Keywords are read from the `KEYWORDS` environment variable (comma-separated). The default set covers:

```
Special Examination, Special Exam, One Time Supplementary,
One Time Regular Supplementary, Exhausted Chances, CBCSS,
2020 Admission, B.Sc, Computer Science, Third Semester
```

To add or change keywords, update `KEYWORDS` in your `.env` (locally) or add/edit a `KEYWORDS` Actions variable in GitHub (**Settings → Variables**).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No Telegram message received | Wrong `BOT_TOKEN` or `CHAT_ID` | Verify with `getUpdates` endpoint |
| `SMTPAuthenticationError` | Wrong password or app password not created | Re-generate App Password |
| `ConnectionError` on scrape | Website temporarily down | Retries are automatic; check logs |
| Duplicate alerts sent | `data/last_seen.json` not committed back | Ensure the workflow push step runs |
| PDF text empty | Scanned/image PDF | Phase 3 adds OCR fallback |

---

## Future Improvements

- OCR support for scanned PDFs (Tesseract / AWS Textract)
- WhatsApp notification channel
- Web dashboard showing notification history
- Slack integration
- Configurable notification quiet hours
- Multi-university support
