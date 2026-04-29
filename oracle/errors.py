"""Oracle exception hierarchy.

Every Oracle-raised exception inherits from :class:`OracleError`. Callers may
either catch the base class to reject any Oracle failure generically, or catch
a specific subclass to branch on the failure mode.
"""

from __future__ import annotations


class OracleError(Exception):
    """Base class for every exception raised by the Oracle module."""


class CollectorUnavailableError(OracleError):
    """A collector could not reach its source (network, timeout, 5xx, 429)."""


class CollectorDataError(OracleError):
    """A collector reached its source but the payload is malformed or unparseable."""


class ChainIntegrityError(OracleError):
    """The attestation chain has been tampered with or is internally inconsistent."""


class SanityBandViolation(OracleError):
    """A normalized value falls outside the configured sanity band for its metric."""


class CrossValidationFailure(OracleError):
    """Primary and secondary sources disagree by more than the configured tolerance."""


class DataUnavailableError(OracleError):
    """Required market datum is missing for the requested as_of date."""


class DataInconsistentError(OracleError):
    """Inputs are individually valid but jointly inconsistent (e.g. mixed currencies)."""
