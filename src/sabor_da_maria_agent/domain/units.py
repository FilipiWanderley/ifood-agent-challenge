import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class MeasureDimension(StrEnum):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


@dataclass(frozen=True, slots=True)
class NormalizedMeasure:
    quantity: Decimal
    unit: str
    dimension: MeasureDimension


_PACKAGE_PATTERN = re.compile(
    r"^(?:un|balde)\s+(?P<size>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|l|ml)$"
)


def normalize_measure(
    quantity: Decimal,
    unit_label: str,
) -> NormalizedMeasure:
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Quantity must be a positive finite decimal.")

    label = " ".join(unit_label.strip().casefold().split())

    direct_units = {
        "kg": NormalizedMeasure(Decimal(1000), "g", MeasureDimension.MASS),
        "g": NormalizedMeasure(Decimal(1), "g", MeasureDimension.MASS),
        "l": NormalizedMeasure(Decimal(1000), "ml", MeasureDimension.VOLUME),
        "ml": NormalizedMeasure(Decimal(1), "ml", MeasureDimension.VOLUME),
        "un": NormalizedMeasure(Decimal(1), "un", MeasureDimension.COUNT),
    }

    if label in direct_units:
        conversion = direct_units[label]
        return NormalizedMeasure(
            quantity=quantity * conversion.quantity,
            unit=conversion.unit,
            dimension=conversion.dimension,
        )

    match = _PACKAGE_PATTERN.fullmatch(label)

    if match:
        package_size = Decimal(match.group("size").replace(",", "."))
        package_unit = match.group("unit")
        return normalize_measure(quantity * package_size, package_unit)

    raise ValueError(f"Unsupported unit: {unit_label!r}")