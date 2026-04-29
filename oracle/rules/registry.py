"""Rule registry.

Each rule lives in its own module under :mod:`oracle.rules.impl` and calls
:func:`register_rule` at import time:

.. code-block:: python

    rule: Rule = register_rule(Rule(rule_id="R-001", ...))

The registry is a module-level dict keyed by ``rule_id``. Tests that want
a clean slate should call :func:`clear_registry`; production code never
needs to.

Engine construction in production wires rules explicitly — it doesn't
read the registry — so tests can also build a :class:`RuleEngine` directly
from an ad-hoc rule list without going through the global registry.
"""

from __future__ import annotations

from oracle.types import Rule


_REGISTRY: dict[str, Rule] = {}


def register_rule(rule: Rule) -> Rule:
    """Register ``rule`` and return it unchanged.

    Re-registering the *same* :class:`Rule` instance is a no-op (common when
    a rule module is imported twice in the same process). Registering a
    *different* rule under an existing ``rule_id`` is a programmer error
    and raises :class:`ValueError`.
    """

    existing = _REGISTRY.get(rule.rule_id)
    if existing is not None:
        if existing is rule:
            return rule
        raise ValueError(
            f"duplicate rule_id {rule.rule_id!r}: already registered as "
            f"version {existing.version!r}; refusing to overwrite with "
            f"version {rule.version!r}"
        )
    _REGISTRY[rule.rule_id] = rule
    return rule


def get_all_rules() -> list[Rule]:
    """Return every registered rule, sorted by ``rule_id`` for determinism."""

    return sorted(_REGISTRY.values(), key=lambda r: r.rule_id)


def get_rule_by_id(rule_id: str) -> Rule | None:
    return _REGISTRY.get(rule_id)


def clear_registry() -> None:
    """Wipe the registry. Test-only."""

    _REGISTRY.clear()
