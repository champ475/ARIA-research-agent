"""
scripts/make_admin.py — give (or take away) admin access to an account (OBS-05).

    python scripts/make_admin.py you@example.com            grant
    python scripts/make_admin.py you@example.com --revoke   take it away

Run it where the app's DATABASE_URL points. The change is written to the audit
log with no actor (it was made from the command line). Admins then see the
Admin tab after their next page load.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email")
    ap.add_argument("--revoke", action="store_true")
    args = ap.parse_args(argv)

    from config import check_boot
    from utils.audit import audit
    from utils.db import user_admin_view, user_set_admin
    from utils.logging_setup import configure_logging

    configure_logging()
    check_boot(serving=False)
    user = user_admin_view(args.email)
    if user is None:
        print(f"No account with email {args.email!r}.")
        return 1
    user_set_admin(user["id"], not args.revoke)
    audit(
        "admin.revoke" if args.revoke else "admin.grant",
        actor_user_id=None,
        target=str(user["id"]),
        details={"via": "make_admin.py"},
    )
    print(
        f"{'Removed admin access from' if args.revoke else 'Granted admin access to'} {user['email']} (id {user['id']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
