"""
scripts/rotate_secret_key.py — re-encrypt stored BYO LLM keys with a new
SECRET_KEY (SEC-11).

Procedure:
  1. In the environment, move the current value to SECRET_KEY_PREVIOUS and
     set a new SECRET_KEY (≥ 32 random characters). Restart the app: it
     encrypts with the new key and still decrypts rows made with the old one.
  2. Run:  python scripts/rotate_secret_key.py            (re-encrypts rows)
           python scripts/rotate_secret_key.py --dry-run  (counts only)
  3. When it reports 0 failures, remove SECRET_KEY_PREVIOUS and restart.

Rows no configured key can read are reported, never deleted.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("rotate_secret_key")


def rotate_all(dry_run: bool = False) -> dict[str, int]:
    """Re-encrypt every stored key with the current SECRET_KEY."""
    from utils import crypto
    from utils.db import llm_config_ciphertexts, llm_config_rewrap

    counts = {"rows": 0, "rotated": 0, "unreadable": 0, "changed_meanwhile": 0}
    for row in llm_config_ciphertexts():
        counts["rows"] += 1
        old = row["api_key_encrypted"]
        try:
            new = crypto.rotate(old)
        except ValueError:
            counts["unreadable"] += 1
            log.warning(
                "user_id=%s: stored key is unreadable with every configured SECRET_KEY", row["user_id"]
            )
            continue
        if dry_run:
            counts["rotated"] += 1
            continue
        if llm_config_rewrap(row["user_id"], old, new):
            counts["rotated"] += 1
        else:
            counts["changed_meanwhile"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    from config import check_boot
    from utils.logging_setup import configure_logging

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="count rows without writing")
    args = parser.parse_args(argv)
    configure_logging()
    check_boot(serving=False)
    import config

    problems = config.secret_key_problems(config.SECRET_KEY)
    if problems:
        sys.stderr.write("\n".join(problems) + "\n")
        return 2
    counts = rotate_all(dry_run=args.dry_run)
    log.info("rotation %s: %s", "dry run" if args.dry_run else "done", counts)
    return 1 if counts["unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
