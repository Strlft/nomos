"""
Regression test for P1-6: OIS discount curve for close-out MTM calculation.

Before fix: calculate_indicative_mtm() used a flat EURIBOR simple-interest
discount factor: DF = 1/(1 + EURIBOR * t).

Problems:
1. Used EURIBOR as the discount rate instead of OIS (€STR). Post-2022 market
   standard is OIS discounting; using EURIBOR over-discounts future cash flows.
2. Used simple discounting (not continuous/compound). Simple discounting
   under-prices discounting for longer maturities.

After fix:
- OIS (€STR) discount rate = max(EURIBOR - ISDA_2021_spread, 0) ≈ €STR proxy.
- Continuous compounding: DF = exp(-r * t) with ACT/365 day count.
- EURIBOR still used for forward rate projection (unchanged).

Regression: OIS-discounted PV must be numerically different from (larger than)
the EURIBOR-simple-discounted PV for positive rates — OIS < EURIBOR, so
OIS discount factors are larger (less discounting), and the MTM value
changes accordingly.
"""

import sys, os
import pytest
import math
from decimal import Decimal
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


class TestOISDiscountFactor:

    def test_ois_discount_factor_method_exists(self):
        """CloseOutModule must have _ois_discount_factor() method."""
        from engine import CloseOutModule, SwapParameters, PartyDetails, CalculationEngine
        import inspect
        assert hasattr(CloseOutModule, "_ois_discount_factor"), (
            "P1-6: CloseOutModule._ois_discount_factor() not found."
        )

    def test_ois_discount_factor_correct_value(self):
        """
        For r=3.0% and t=365 days (1 year), continuous DF = exp(-0.03) ≈ 0.97045.
        Simple DF = 1/(1+0.03) ≈ 0.97087.
        They should differ — continuous is slightly lower.
        """
        from engine import (CloseOutModule, SwapParameters, PartyDetails,
                            CalculationEngine, ScheduleElections)

        params = SwapParameters(
            contract_id="P1-6-DF",
            party_a=PartyDetails("Alpha", "Alpha", "fixed_payer", "FR"),
            party_b=PartyDetails("Beta", "Beta", "floating_payer", "GB"),
            notional=Decimal("10000000"), fixed_rate=Decimal("0.035"),
            effective_date=date(2026, 5, 1), termination_date=date(2028, 5, 1),
        )
        calc = CalculationEngine(params)
        mod = CloseOutModule(params, calc)

        r = Decimal("0.03")
        ois_df = mod._ois_discount_factor(r, 365)
        expected = Decimal(str(math.exp(-0.03 * 1.0)))

        assert abs(ois_df - expected) < Decimal("0.00001"), (
            f"P1-6: _ois_discount_factor({r}, 365) = {ois_df}, "
            f"expected ≈ {expected} (exp(-r*t))"
        )

        # Must be different from simple discount factor
        simple_df = Decimal("1") / (Decimal("1") + r * Decimal("1"))
        assert ois_df != simple_df, (
            "P1-6: OIS discount factor is identical to simple discount factor. "
            "Continuous compounding not implemented."
        )

    def test_ois_df_less_than_1_for_positive_rate(self):
        """Discount factor must be between 0 and 1 for any positive rate."""
        from engine import (CloseOutModule, SwapParameters, PartyDetails,
                            CalculationEngine)
        params = SwapParameters(
            contract_id="P1-6-DF2",
            party_a=PartyDetails("A", "A", "fixed_payer", "FR"),
            party_b=PartyDetails("B", "B", "floating_payer", "GB"),
            notional=Decimal("1000000"), fixed_rate=Decimal("0.03"),
            effective_date=date(2026, 5, 1), termination_date=date(2028, 5, 1),
        )
        mod = CloseOutModule(params, CalculationEngine(params))
        for days in [90, 180, 365, 730]:
            df = mod._ois_discount_factor(Decimal("0.04"), days)
            assert Decimal("0") < df < Decimal("1"), (
                f"P1-6: discount factor {df} not in (0,1) for {days} days."
            )


class TestOISRateUsed:

    def test_ois_rate_is_below_euribor(self):
        """
        The OIS rate used in MTM must be strictly below EURIBOR 3M (for positive
        rates), because OIS = EURIBOR − ISDA_2021_spread.
        """
        from engine import CloseOutModule
        # The spread constant must be positive
        assert CloseOutModule._ESTR_EURIBOR_SPREAD > Decimal("0"), (
            "P1-6: _ESTR_EURIBOR_SPREAD must be positive."
        )
        # For EURIBOR = 3.875%, OIS = 3.875% - 0.0959% = 3.7791%
        euribor = Decimal("0.03875")
        ois = max(euribor - CloseOutModule._ESTR_EURIBOR_SPREAD, Decimal("0"))
        assert ois < euribor, (
            f"P1-6: OIS rate {ois} is not below EURIBOR {euribor}."
        )

    def test_estr_spread_constant_value(self):
        """
        ISDA 2021 EURIBOR 3M fallback spread = 0.0959% (9.59 bps).
        The constant must match this published value.
        """
        from engine import CloseOutModule
        expected = Decimal("0.000959")
        assert CloseOutModule._ESTR_EURIBOR_SPREAD == expected, (
            f"P1-6: _ESTR_EURIBOR_SPREAD = {CloseOutModule._ESTR_EURIBOR_SPREAD}, "
            f"expected {expected} (ISDA 2021 EURIBOR 3M fallback spread)."
        )


class TestMTMNumericalDifference:

    def test_mtm_uses_ois_not_euribor_flat(self):
        """
        For a contract with EURIBOR > 0, the indicative MTM using OIS discounting
        must be numerically different from what a EURIBOR flat-curve simple-interest
        approach would produce.

        This test validates the fix by manually computing the old approach and
        verifying the new approach gives a different result.
        """
        from engine import (CloseOutModule, SwapParameters, PartyDetails,
                            CalculationEngine, OracleReading, OracleStatus,
                            ContractInitiation, ScheduleElections,
                            IRSExecutionEngine)
        from datetime import timezone, datetime

        params = SwapParameters(
            contract_id="P1-6-MTM",
            party_a=PartyDetails("Alpha", "Alpha", "fixed_payer", "FR"),
            party_b=PartyDetails("Beta", "Beta", "floating_payer", "GB"),
            notional=Decimal("15000000"),
            fixed_rate=Decimal("0.035"),
            effective_date=date(2026, 5, 1),
            termination_date=date(2028, 5, 1),
        )
        eng = IRSExecutionEngine(
            params, ScheduleElections("S-P1-6"), ContractInitiation("t"))
        eng.initialise()

        oracle = OracleReading(
            rate=Decimal("0.03875"),
            status=OracleStatus.RATE_OVERRIDE,
            source="TEST",
            fetch_timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )

        mod = CloseOutModule(params, eng.calc)
        etd = date(2026, 8, 3)
        remaining = [p for p in eng.periods if p.payment_date > etd]

        # New approach (OIS continuous)
        ois_mtm = mod.calculate_indicative_mtm(etd, remaining, oracle, "PARTY_A")

        # Old approach (EURIBOR simple flat): manually compute for comparison
        old_pv = Decimal("0")
        euribor = oracle.rate
        for p in remaining:
            fixed_amt = eng.calc.calculate_fixed_amount(p)
            float_amt = eng.calc.calculate_floating_amount(p, oracle)
            from engine import NetPayer
            net, payer = eng.calc.apply_netting(fixed_amt, float_amt)
            days = max((p.payment_date - etd).days, 0)
            dcf = Decimal(str(days)) / Decimal("365")
            old_df = Decimal("1") / (Decimal("1") + euribor * dcf)
            old_pv_period = net * old_df
            if payer == NetPayer.PARTY_A:
                old_pv_period = -old_pv_period
            old_pv += old_pv_period

        assert ois_mtm != old_pv, (
            "P1-6: OIS MTM and EURIBOR-flat MTM are identical — "
            "the discount curve was not changed."
        )
