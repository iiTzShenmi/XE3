#!/usr/bin/env python3
from __future__ import annotations

import logging

from agent.features.e3.reminder.api import refresh_all_saved_accounts


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    summary = refresh_all_saved_accounts(logging.getLogger("e3-sync-check"))
    print(f"SUMMARY total={summary['total']} ok={summary['ok']} failed={summary['failed']}")
    for row in summary["results"]:
        print(
            f"RESULT user={row['user_key']} ok={row['ok']} courses={row['course_count']} "
            f"seconds={row['duration_seconds']} warnings={row['validation_issue_count']} "
            f"endpoint_failures={row.get('endpoint_failure_count', 0)} "
            f"error={row['error']!r}"
        )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
