"""Optional: give the demo manager the extra ERPNext roles the non-sales demo prompts need (docs/prompts.csv).

This widens what the assistant can read and change for that user, so it is not part of setup_erpnext.py or run.sh.
Run it yourself, only on a demo site:

    venv/bin/python scripts/grant_demo_roles.py            # shows what it would add
    venv/bin/python scripts/grant_demo_roles.py --yes      # adds the roles to manager@example.com
"""
from __future__ import annotations

import sys

from _common import AdminSession  # noqa: E402  (also sets sys.path)

USER = "manager@example.com"
ROLES = ["Stock User", "Stock Manager", "Purchase User", "HR User", "Projects User", "Accounts User", "Sales Master Manager"]


def main():
    admin = AdminSession()
    doc = admin.request("GET", f"/api/resource/User/{USER}")["data"]
    have = {r["role"] for r in doc["roles"]}
    missing = [r for r in ROLES if r not in have]
    print(f"{USER} has: {sorted(have)}\nwould add: {missing or 'nothing'}")
    if not missing or "--yes" not in sys.argv:
        return
    admin.request("PUT", f"/api/resource/User/{USER}", json={"roles": [{"role": r} for r in sorted(have | set(missing))]})
    print("Roles added.")


if __name__ == "__main__":
    main()
