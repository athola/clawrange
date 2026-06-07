#!/usr/bin/env python3
"""Load the lead-crm demo leads into the CRM (FR-9.2).

Lets the lead-crm reference profile work fully offline with no real portal:
reads ``config/profiles/lead-crm/demo_leads.csv``, runs it through the same
``leads_clean`` transform the live connector uses, and upserts the rows into
the CRM adapter the profile configures (SQLite by default).

Honors ``CRM_DB_PATH`` for the SQLite location. Run via ``make seed-demo``.
"""

from __future__ import annotations

import csv
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "workflows"))

from connectors.transforms import leads_clean  # noqa: E402
from crm import get_adapter  # noqa: E402
from tenant_profile import load_profile  # noqa: E402


def main() -> int:
    profile = load_profile("lead-crm")
    connector = profile.connector("portal-leads")
    if connector is None:
        print(
            "seed-demo: lead-crm profile has no 'portal-leads' connector",
            file=sys.stderr,
        )
        return 1

    csv_path = os.path.join(_REPO, "config", "profiles", "lead-crm", "demo_leads.csv")
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    cleaned = leads_clean(rows, connector["transform"])
    crm = get_adapter(profile.crm)
    crm.init()
    upsert_key = connector["sink"].get("upsert_key", "email")
    written = crm.upsert("leads", cleaned, upsert_key)

    health = crm.health()
    print(
        f"seed-demo: wrote {written} leads "
        f"({len(rows)} read, {len(cleaned)} kept) into "
        f"{health.get('path', health.get('adapter'))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
