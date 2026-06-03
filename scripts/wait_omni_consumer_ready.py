#!/usr/bin/env python3
"""Poll Omni analyst/worker /readyz until HTTP 200 (in-cluster Job or local lab).

Env:
  OMNI_WORKER_READY_URL   default http://127.0.0.1:8090/readyz
  OMNI_VERIFY_READY_TIMEOUT_SEC  default 120
  OMNI_VERIFY_READY_INTERVAL_SEC default 3
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request


def main() -> None:
    url = os.environ.get("OMNI_WORKER_READY_URL", "http://127.0.0.1:8090/readyz").strip()
    timeout = float(os.environ.get("OMNI_VERIFY_READY_TIMEOUT_SEC", "120"))
    interval = float(os.environ.get("OMNI_VERIFY_READY_INTERVAL_SEC", "3"))
    deadline = time.monotonic() + max(5.0, timeout)

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                if getattr(r, "status", 200) == 200:
                    print(f"[wait_omni_consumer_ready] ok {url}", flush=True)
                    return
        except urllib.error.HTTPError as e:
            print(f"[wait_omni_consumer_ready] http {e.code} {url}", flush=True)
        except Exception as e:
            print(f"[wait_omni_consumer_ready] retry {url}: {e}", flush=True)
        time.sleep(max(0.5, interval))

    print(f"[wait_omni_consumer_ready] TIMEOUT {url}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
