"""Narrow read/write bridge between the Oracle and :mod:`backend.engine`.

Exactly two public operations — the whole surface the Oracle has with the
IRS engine:

* :meth:`IRSBridge.fetch_contract_state` — read-only. Returns the
  Oracle-side view of the contract so :class:`RuleEngine` can evaluate.
* :meth:`IRSBridge.submit_trigger_event` — the only mutating call the
  Oracle ever makes against the engine. Submits a typed :class:`TriggerEvent`
  and returns a :class:`TriggerReceipt`. Never submits a command; never
  calls ``close_out()``, ``declare_bankruptcy()``, or any other engine
  method.

The bridge is constructed with a **resolver** callable that turns a
``contract_id`` into the live engine instance. In the Flask API this is
``lambda cid: backend.api._engines[cid]``. In tests it's a lambda over a
single test engine. Keeping the lookup as an injected callable means the
Oracle has no import dependency on ``backend.api``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oracle.types import TriggerEvent


_EngineResolver = Callable[[str], Any]


class IRSBridge:
    """Two public methods, no convenience wrappers."""

    def __init__(
        self,
        resolver: _EngineResolver,
        *,
        notices_provider: Callable[[str], tuple] | None = None,
    ) -> None:
        """Parameters
        ----------
        resolver
            ``contract_id -> IRSExecutionEngine``. Raises ``KeyError`` if the
            contract does not exist.
        notices_provider
            Optional. ``contract_id -> tuple[OracleNotice, ...]``. The engine
            does not track dated notices in V1, so the caller supplies them.
            Defaults to "no notices", which means R-001 will emit WARNING
            for any overdue payment rather than escalating to TRIGGER.
        """

        self._resolver = resolver
        self._notices_provider = notices_provider or (lambda _cid: ())

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def fetch_contract_state(self, contract_id: str) -> Any:
        """Return the Oracle-side read-only snapshot.

        The return type is :class:`backend.engine.OracleContractSnapshot` (a
        frozen dataclass). We return it as ``Any`` to avoid importing a
        concrete engine type at module scope — the Oracle package stays
        decoupled from ``backend``.
        """

        from backend.engine import get_oracle_contract_snapshot

        engine = self._resolver(contract_id)
        notices = self._notices_provider(contract_id)
        return get_oracle_contract_snapshot(engine, notices=notices)

    # ------------------------------------------------------------------
    # Write path (single, typed, non-commanding)
    # ------------------------------------------------------------------

    def submit_trigger_event(self, event: TriggerEvent) -> Any:
        """Forward a :class:`TriggerEvent` to the engine and return its receipt.

        This is the **only** mutating call the Oracle ever makes against the
        engine. The engine records it and does not act on it.
        """

        from backend.engine import submit_trigger_event as engine_submit

        engine = self._resolver(event.contract_id)
        return engine_submit(engine, event)
