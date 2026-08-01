"""Tests for domain validators — phones, locales, timezones, decimals, money."""

from datetime import UTC, datetime, tzinfo
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from fonely.core.validators import (
    AwareDatetime,
    E164PhoneNumber,
    FonelyLocale,
    IANATimezone,
    IndianMobileNumber,
    INRAmount,
    NonNegativeDecimal,
    PositiveDecimal,
    Quantity,
    quantize_inr,
)

# --- Helpers: Pydantic models wrapping annotated types ---


class PhoneModel(BaseModel):
    mobile: IndianMobileNumber


class E164Model(BaseModel):
    phone: E164PhoneNumber


class LocaleModel(BaseModel):
    locale: FonelyLocale


class TzModel(BaseModel):
    tz: IANATimezone


class MoneyModel(BaseModel):
    amount: INRAmount


class QtyModel(BaseModel):
    qty: NonNegativeDecimal


class PosQtyModel(BaseModel):
    qty: PositiveDecimal


class PersistedQtyModel(BaseModel):
    qty: Quantity


class DtModel(BaseModel):
    ts: AwareDatetime


# === IndianMobileNumber ===


class TestIndianMobileNumber:
    def test_valid_with_plus91(self) -> None:
        m = PhoneModel(mobile="+919876543210")
        assert m.mobile == "+919876543210"

    def test_valid_without_plus(self) -> None:
        m = PhoneModel(mobile="9876543210")
        assert m.mobile == "+919876543210"

    def test_valid_with_leading_zero(self) -> None:
        m = PhoneModel(mobile="09876543210")
        assert m.mobile == "+919876543210"

    def test_valid_with_91_prefix(self) -> None:
        m = PhoneModel(mobile="919876543210")
        assert m.mobile == "+919876543210"

    def test_valid_with_spaces(self) -> None:
        m = PhoneModel(mobile="+91 98765 43210")
        assert m.mobile == "+919876543210"

    def test_reject_too_short(self) -> None:
        with pytest.raises(ValidationError):
            PhoneModel(mobile="12345")

    def test_reject_starts_with_5(self) -> None:
        with pytest.raises(ValidationError):
            PhoneModel(mobile="+915876543210")

    def test_reject_starts_with_0(self) -> None:
        with pytest.raises(ValidationError):
            PhoneModel(mobile="+910876543210")

    def test_reject_too_long(self) -> None:
        with pytest.raises(ValidationError):
            PhoneModel(mobile="+9198765432100")

    def test_reject_non_numeric(self) -> None:
        with pytest.raises(ValidationError):
            PhoneModel(mobile="abcdefghij")


# === E164PhoneNumber ===


class TestE164PhoneNumber:
    def test_valid_indian(self) -> None:
        m = E164Model(phone="+919876543210")
        assert m.phone == "+919876543210"

    def test_valid_us(self) -> None:
        m = E164Model(phone="+12025551234")
        assert m.phone == "+12025551234"

    def test_valid_uk(self) -> None:
        m = E164Model(phone="+447911123456")
        assert m.phone == "+447911123456"

    def test_reject_without_plus(self) -> None:
        with pytest.raises(ValidationError):
            E164Model(phone="919876543210")

    def test_reject_country_code_zero(self) -> None:
        with pytest.raises(ValidationError):
            E164Model(phone="+0123456789")

    def test_reject_too_short(self) -> None:
        with pytest.raises(ValidationError):
            E164Model(phone="+12345")

    def test_reject_too_long(self) -> None:
        with pytest.raises(ValidationError):
            E164Model(phone="+1234567890123456")

    def test_reject_local_number(self) -> None:
        with pytest.raises(ValidationError):
            E164Model(phone="9876543210")


# === FonelyLocale ===


class TestFonelyLocale:
    def test_valid_tamil(self) -> None:
        m = LocaleModel(locale="ta-IN")
        assert m.locale == "ta-IN"

    def test_valid_odia_canonical(self) -> None:
        m = LocaleModel(locale="or-IN")
        assert m.locale == "or-IN"

    def test_reject_sarvam_odia(self) -> None:
        with pytest.raises(ValidationError):
            LocaleModel(locale="od-IN")

    def test_reject_unknown(self) -> None:
        with pytest.raises(ValidationError):
            LocaleModel(locale="unknown")

    def test_reject_arbitrary(self) -> None:
        with pytest.raises(ValidationError):
            LocaleModel(locale="xx-YY")

    def test_all_supported_locales(self) -> None:
        for loc in [
            "ta-IN",
            "hi-IN",
            "te-IN",
            "kn-IN",
            "ml-IN",
            "bn-IN",
            "mr-IN",
            "gu-IN",
            "pa-IN",
            "or-IN",
            "en-IN",
            "as-IN",
            "ur-IN",
        ]:
            m = LocaleModel(locale=loc)
            assert m.locale == loc


# === IANATimezone ===


class TestIANATimezone:
    def test_valid_kolkata(self) -> None:
        m = TzModel(tz="Asia/Kolkata")
        assert m.tz == "Asia/Kolkata"

    def test_valid_utc(self) -> None:
        m = TzModel(tz="UTC")
        assert m.tz == "UTC"

    def test_reject_invalid(self) -> None:
        with pytest.raises(ValidationError):
            TzModel(tz="India/Chennai")

    def test_reject_empty(self) -> None:
        with pytest.raises(ValidationError):
            TzModel(tz="")

    @pytest.mark.parametrize(
        "timezone_key", ["localtime", "Factory", "posixrules", "posix/UTC", "right/UTC"]
    )
    def test_reject_unstable_special_keys(self, timezone_key: str) -> None:
        with pytest.raises(ValidationError, match="Invalid timezone"):
            TzModel(tz=timezone_key)


# === INRAmount ===


class TestINRAmount:
    def test_valid_integer(self) -> None:
        m = MoneyModel(amount=500)
        assert m.amount == Decimal("500")

    def test_valid_string(self) -> None:
        m = MoneyModel(amount="499.50")  # type: ignore[arg-type]
        assert m.amount == Decimal("499.50")

    def test_valid_decimal(self) -> None:
        m = MoneyModel(amount=Decimal("100.00"))
        assert m.amount == Decimal("100.00")

    def test_valid_zero(self) -> None:
        m = MoneyModel(amount=0)
        assert m.amount == Decimal("0")

    def test_reject_negative(self) -> None:
        with pytest.raises(ValidationError):
            MoneyModel(amount=Decimal("-1"))

    def test_reject_float(self) -> None:
        with pytest.raises(ValidationError):
            MoneyModel(amount=99.99)  # type: ignore[arg-type]

    def test_reject_excess_precision(self) -> None:
        with pytest.raises(ValidationError):
            MoneyModel(amount=Decimal("100.123"))

    def test_accept_exact_two_places(self) -> None:
        m = MoneyModel(amount=Decimal("99.99"))
        assert m.amount == Decimal("99.99")

    def test_accept_one_place(self) -> None:
        m = MoneyModel(amount=Decimal("99.9"))
        assert m.amount == Decimal("99.9")

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_reject_non_finite(self, value: str) -> None:
        with pytest.raises(ValidationError):
            MoneyModel(amount=Decimal(value))

    def test_reject_float_expression(self) -> None:
        with pytest.raises(ValidationError):
            MoneyModel(amount=0.1 + 0.2)  # type: ignore[arg-type]


# === NonNegativeDecimal ===


class TestNonNegativeDecimal:
    def test_valid_zero(self) -> None:
        m = QtyModel(qty=0)
        assert m.qty == Decimal("0")

    def test_valid_positive(self) -> None:
        m = QtyModel(qty=Decimal("5.5"))
        assert m.qty == Decimal("5.5")

    def test_reject_negative(self) -> None:
        with pytest.raises(ValidationError):
            QtyModel(qty=Decimal("-0.01"))

    def test_reject_float(self) -> None:
        with pytest.raises(ValidationError):
            QtyModel(qty=1.5)  # type: ignore[arg-type]

    def test_accept_string(self) -> None:
        m = QtyModel(qty="3.75")  # type: ignore[arg-type]
        assert m.qty == Decimal("3.75")

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_reject_non_finite(self, value: str) -> None:
        with pytest.raises(ValidationError):
            QtyModel(qty=Decimal(value))


# === PositiveDecimal ===


class TestQuantity:
    def test_accept_two_decimal_places(self) -> None:
        m = PersistedQtyModel(qty=Decimal("0.01"))
        assert m.qty == Decimal("0.01")

    def test_reject_more_than_two_decimal_places(self) -> None:
        with pytest.raises(ValidationError):
            PersistedQtyModel(qty=Decimal("0.001"))

    def test_reject_tiny_value_that_would_store_as_zero(self) -> None:
        with pytest.raises(ValidationError):
            PersistedQtyModel(qty=Decimal("0.000000000000001"))

    def test_reject_zero(self) -> None:
        with pytest.raises(ValidationError):
            PersistedQtyModel(qty=Decimal("0.00"))


# === PositiveDecimal ===


class TestPositiveDecimal:
    def test_reject_zero(self) -> None:
        with pytest.raises(ValidationError):
            PosQtyModel(qty=0)

    def test_accept_positive(self) -> None:
        m = PosQtyModel(qty=Decimal("0.01"))
        assert m.qty == Decimal("0.01")


# === AwareDatetime ===


class IneffectiveTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "Ineffective"


class TestAwareDatetime:
    def test_valid_utc(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        m = DtModel(ts=dt)
        assert m.ts == dt

    def test_reject_naive(self) -> None:
        with pytest.raises(ValidationError):
            DtModel(ts=datetime(2026, 1, 1))

    def test_reject_tzinfo_without_utc_offset(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            DtModel(ts=datetime(2026, 1, 1, tzinfo=IneffectiveTimezone()))


# === quantize_inr ===


class TestBooleanDecimalRejection:
    @pytest.mark.parametrize("value", [True, False])
    def test_inr_rejects_boolean(self, value: bool) -> None:
        with pytest.raises(ValidationError, match="Boolean"):
            MoneyModel(amount=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, False])
    def test_nonnegative_decimal_rejects_boolean(self, value: bool) -> None:
        with pytest.raises(ValidationError, match="Boolean"):
            QtyModel(qty=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, False])
    def test_positive_decimal_rejects_boolean(self, value: bool) -> None:
        with pytest.raises(ValidationError, match="Boolean"):
            PosQtyModel(qty=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [True, False])
    def test_quantity_rejects_boolean(self, value: bool) -> None:
        with pytest.raises(ValidationError, match="Boolean"):
            PersistedQtyModel(qty=value)  # type: ignore[arg-type]


class TestQuantizeINR:
    def test_rounds_half_up(self) -> None:
        assert quantize_inr(Decimal("10.125")) == Decimal("10.13")

    def test_preserves_exact(self) -> None:
        assert quantize_inr(Decimal("10.12")) == Decimal("10.12")

    def test_rounds_excess_precision_half_up(self) -> None:
        assert quantize_inr(Decimal("10.1249")) == Decimal("10.12")

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_reject_non_finite(self, value: str) -> None:
        with pytest.raises(ValueError, match="must be finite"):
            quantize_inr(Decimal(value))
