"""`python -m utsu_portal.authsvc maintain [--force]` runs the due audit jobs (for a CronJob / Cloud Scheduler)."""
import sys

from ..maintenance import main

if len(sys.argv) > 1 and sys.argv[1] == "maintain":
    raise SystemExit(main(sys.argv[2:]))
raise SystemExit("usage: python -m utsu_portal.authsvc maintain [--force]")
