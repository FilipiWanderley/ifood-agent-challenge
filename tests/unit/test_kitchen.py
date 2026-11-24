from decimal import Decimal

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.domain.kitchen import (
    EquipmentRequirement,
    KitchenEquipment,
    KitchenProfile,
)


def test_kitchen_distinguishes_unknown_inventory_from_known_empty_inventory() -> None:
    unknown = KitchenProfile()
    known_empty = KitchenProfile(equipment=(), skills=())

    assert unknown.equipment is None
    assert known_empty.equipment == ()
    assert unknown.skills is None
    assert known_empty.skills == ()


def test_equipment_and_requirement_normalize_names_and_units() -> None:
    equipment = KitchenEquipment(
        name=" Panela de Pressão ",
        available=True,
        capacity=Decimal(5),
        capacity_unit=" L ",
    )
    requirement = EquipmentRequirement(
        name="PANELA DE PRESSÃO",
        minimum_capacity=Decimal(4),
        capacity_unit="l",
    )

    assert equipment.name == "Panela de Pressão"
    assert requirement.name == "PANELA DE PRESSÃO"
    assert equipment.capacity_unit == requirement.capacity_unit == "l"


def test_capacity_and_unit_must_be_provided_together() -> None:
    with pytest.raises(ValidationError):
        KitchenEquipment(name="Forno", available=True, capacity=Decimal(50))

    with pytest.raises(ValidationError):
        EquipmentRequirement(name="Forno", capacity_unit="l")


def test_kitchen_rejects_duplicate_equipment_and_skills() -> None:
    equipment = KitchenEquipment(name="Fogão", available=True)
    with pytest.raises(ValidationError):
        KitchenProfile(equipment=(equipment, equipment))

    with pytest.raises(ValidationError):
        KitchenProfile(skills=("galinhada", " GALINHADA "))


def test_equipment_availability_must_be_explicit() -> None:
    with pytest.raises(ValidationError):
        KitchenEquipment.model_validate({"name": "Fogão"})
