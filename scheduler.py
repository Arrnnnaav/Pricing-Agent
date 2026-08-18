"""
Wraps main.run_pipeline() in a daily APScheduler job. Run this instead
of main.py directly if you want the pipeline to run automatically every
day rather than once on demand:

    python scheduler.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from main import run_pipeline

# Runs once a day at 6:00 AM server time -- before a typical business
# day starts, so any human approvals needed are waiting when the team
# logs in rather than arriving mid-afternoon.
DAILY_HOUR = 6
DAILY_MINUTE = 0


def scheduled_run():
    print(f"\n[scheduler] Triggering pipeline run...")
    try:
        result = run_pipeline()
        print(f"[scheduler] Run finished: {result}")
    except Exception as e:
        # A scheduled job that raises silently kills future runs in
        # some setups -- catching and logging here keeps the scheduler
        # itself alive even if one day's run fails outright.
        print(f"[scheduler] Run failed: {e}")


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(
        scheduled_run,
        trigger=CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        id="daily_pricing_run",
    )

    print(f"Scheduler started. Pipeline will run daily at "
          f"{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}. Press Ctrl+C to stop.")

    # Run once immediately on startup too, so you don't have to wait
    # until tomorrow morning to see it work.
    scheduled_run()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")
