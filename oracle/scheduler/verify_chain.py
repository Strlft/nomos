"""CLI: re-run :meth:`AttestationStore.verify_integrity` on a live DB.

Emits a single structlog event with ``action="verify_integrity"`` and
``outcome="ok"`` or ``"corrupt"``. Exit code is ``0`` on success and
``1`` on integrity failure so cron / CI can react.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from oracle.core.store import AttestationStore
from oracle.logging_config import configure_logging, get_logger


_log = get_logger("verify_chain")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oracle-verify-chain",
        description=(
            "Verify the byte-level and hash-chain integrity of an Oracle "
            "AttestationStore SQLite database."
        ),
    )
    parser.add_argument(
        "--db-path", required=True, type=Path,
        help="Path to the SQLite attestation store.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)

    if not args.db_path.exists():
        _log.error(
            "verify_chain_db_missing",
            action="verify_integrity",
            outcome="failure",
            db_path=str(args.db_path),
            reason="db_path_does_not_exist",
        )
        return 2

    started = time.perf_counter()
    store = AttestationStore(args.db_path)
    ok, error = store.verify_integrity()
    duration_ms = int((time.perf_counter() - started) * 1000)

    if ok:
        _log.info(
            "verify_chain_ok",
            action="verify_integrity",
            outcome="ok",
            duration_ms=duration_ms,
            db_path=str(args.db_path),
        )
        return 0

    _log.error(
        "verify_chain_corrupt",
        action="verify_integrity",
        outcome="corrupt",
        duration_ms=duration_ms,
        db_path=str(args.db_path),
        error=error,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
