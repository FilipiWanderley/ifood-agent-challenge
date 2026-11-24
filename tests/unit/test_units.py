from decimal import Decimal

import pytest

from sabor_da_maria_agent.domain.units import (
    MeasureDimension,
    normalize_measure,
)


@pytest.mark.parametrize(
    (
        "quantity",
        "unit_label",
        "expected_quantity",
        "expected_unit",
        "expected_dimension",
    ),
    [
        (Decimal(5), "kg", Decimal(5000), "g", MeasureDimension.MASS),
        (Decimal("0.5"), "kg", Decimal(500), "g", MeasureDimension.MASS),
        (Decimal(2), "L", Decimal(2000), "ml", MeasureDimension.VOLUME),
        (Decimal(1), "balde 2kg", Decimal(2000), "g", MeasureDimension.MASS),
        (Decimal(1), "un 500g", Decimal(500), "g", MeasureDimension.MASS),
        (Decimal(1), "un 400g", Decimal(400), "g", MeasureDimension.MASS),
        (Decimal(1), "un 500ml", Decimal(500), "ml", MeasureDimension.VOLUME),
        (Decimal(1), "un 100ml", Decimal(100), "ml", MeasureDimension.VOLUME),
        (Decimal(30), "un", Decimal(30), "un", MeasureDimension.COUNT),
    ],
)
def test_normalize_measure(
    quantity: Decimal,
    unit_label: str,
    expected_quantity: Decimal,
    expected_unit: str,
    expected_dimension: MeasureDimension,
) -> None:
    result = normalize_measure(quantity, unit_label)

    assert result.quantity == expected_quantity
    assert result.unit == expected_unit
    assert result.dimension == expected_dimension


def test_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="positive finite decimal"):
        normalize_measure(Decimal(0), "kg")


def test_rejects_unsupported_unit() -> None:
    with pytest.raises(ValueError, match="Unsupported unit"):
        normalize_measure(Decimal(1), "package")
