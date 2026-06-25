"""
monitor.py — Entry point for the Pareeksha Bhavan monitor.

This file is intentionally thin: it wires together the sub-modules
from `src/` and drives a single monitoring run.

Architecture (to be filled in phase by phase):

    config      → load settings from env / .env
    scraper     → fetch notification list from the website
    pdf         → download + extract text from any PDF links
    matcher     → test page/PDF content against configured keywords
    store       → check for duplicates, persist seen notification IDs
    notifier    → send Telegram + email alerts

Called by:
    - GitHub Actions (monitor.yml) on a 6-hour cron schedule
    - Locally:  python monitor.py
"""

import sys
import logging

# ---------------------------------------------------------------------------
# Logging — plain console output for now; will switch to structlog in Phase 2
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run one monitoring cycle.

    Returns:
        0 on success, 1 on unrecoverable error.
    """
    logger.info("Pareeksha Bhavan Monitor — starting run")

    # Sub-module calls will be added here in subsequent phases:
    #   settings  = load_settings()
    #   items     = scrape(settings)
    #   new_items = filter_new(items, settings.last_seen_path)
    #   matches   = match_keywords(new_items, settings.keywords)
    #   notify(matches, settings)
    #   save_seen(new_items, settings.last_seen_path)

    logger.info("Phase 1 scaffold — no scraping implemented yet")
    logger.info("Monitor run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
