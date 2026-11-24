#!/usr/bin/env python3
"""ci/changed_services.py -- which images does this commit need to rebuild?

    python ci/changed_services.py <base-commit>     # space-separated list

* services/<name>/...                -> <name>
* services/common, requirements.txt,
  services/Dockerfile                -> every Python service (shared layers)
* no usable base commit (first build, force push) -> every service
Anything else (charts, docs, deploy) needs no image.
"""

import subprocess
import sys

PYTHON = ["api-gateway", "auth-service", "user-service", "list-service", "task-service",
          "tag-service", "comment-service", "attachment-service", "reminder-service",
          "recurrence-service", "notification-service", "search-service",
          "analytics-service", "audit-service"]
ALL = ["web-frontend", *PYTHON]
SHARED = ("services/common/", "services/requirements.txt", "services/Dockerfile")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else ""
    if not base:
        print(" ".join(ALL))
        return 0
    try:
        files = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], check=True,
                               capture_output=True, text=True).stdout.split()
    except subprocess.CalledProcessError:
        print(" ".join(ALL))  # base no longer in history
        return 0
    changed: set[str] = set()
    for f in files:
        if f.startswith(SHARED):
            changed.update(PYTHON)
        elif f.startswith("services/"):
            name = f.split("/")[1]
            if name in ALL:
                changed.add(name)
    print(" ".join(s for s in ALL if s in changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
