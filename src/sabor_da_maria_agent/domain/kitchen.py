"""Kitchen capabilities and operational constraints for recipe validation."""

import unicodedata
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KitchenModel(BaseModel):
    """Immutable base model for kitchen-domain value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def normalize_label(value: str) -> str:
    """Normalize a human-entered label for stable domain comparisons."""
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("Value cannot be empty.")
    return normalized


def comparison_key(value: str) -> str:
    """Return a case-insensitive key without changing the display label."""
    return normalize_label(value).casefold()


class UtilityKind(StrEnum):
    GAS = "gas"
    ELECTRICITY = "electricity"
    WATER = "water"


class EquipmentAvailability(StrEnum):
    """Knowledge state of one equipment item in a kitchen.

    ``UNKNOWN`` is never collapsed into ``UNAVAILABLE``: an item that is
    absent from an inventory not confirmed complete is still unknown.
    """

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class StorageKind(StrEnum):
    REFRIGERATOR = "refrigerator"
    FREEZER = "freezer"


class StorageRequirement(KitchenModel):
    """Cold storage a recipe needs, as stated by a source or by Dona Maria.

    ``volume_liters_per_batch`` is the free space one batch occupies. ``None``
    means the volume is unknown; it is never derived from the serving count.
    """

    kind: StorageKind
    volume_liters_per_batch: Decimal | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("volume_liters_per_batch")
    @classmethod
    def validate_volume(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("Storage volume must be positive and finite.")
        return value

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        return normalize_label(value) if value is not None else None


class EquipmentRequirement(KitchenModel):
    """Minimum equipment capability required for one recipe batch.

    Names are matched exactly (case/whitespace-insensitive). No substitution
    is ever assumed: a blender does not satisfy a food-processor requirement
    and a pressure cooker does not satisfy a requirement for another pan.
    """

    name: str = Field(min_length=1)
    minimum_quantity: int = Field(default=1, gt=0)
    minimum_capacity: Decimal | None = Field(default=None, gt=0)
    capacity_unit: str | None = Field(default=None, min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("capacity_unit")
    @classmethod
    def normalize_capacity_unit(cls, value: str | None) -> str | None:
        return normalize_label(value).casefold() if value is not None else None

    @field_validator("minimum_capacity")
    @classmethod
    def validate_capacity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("Minimum capacity must be positive and finite.")
        return value

    @model_validator(mode="after")
    def validate_capacity_pair(self) -> "EquipmentRequirement":
        if (self.minimum_capacity is None) != (self.capacity_unit is None):
            raise ValueError(
                "minimum_capacity and capacity_unit must be provided together."
            )
        return self


class KitchenEquipment(KitchenModel):
    """An equipment item reported as part of the kitchen inventory."""

    name: str = Field(min_length=1)
    quantity: int = Field(default=1, gt=0)
    available: bool | None
    capacity: Decimal | None = Field(default=None, gt=0)
    capacity_unit: str | None = Field(default=None, min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("capacity_unit")
    @classmethod
    def normalize_capacity_unit(cls, value: str | None) -> str | None:
        return normalize_label(value).casefold() if value is not None else None

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("Equipment capacity must be positive and finite.")
        return value

    @model_validator(mode="after")
    def validate_capacity_pair(self) -> "KitchenEquipment":
        if (self.capacity is None) != (self.capacity_unit is None):
            raise ValueError("capacity and capacity_unit must be provided together.")
        return self

    @property
    def availability(self) -> EquipmentAvailability:
        if self.available is None:
            return EquipmentAvailability.UNKNOWN
        return (
            EquipmentAvailability.AVAILABLE
            if self.available
            else EquipmentAvailability.UNAVAILABLE
        )


class KitchenProfile(KitchenModel):
    """Known kitchen facts; ``None`` means not yet elicited from the user.

    Equipment knowledge is explicit:

    - ``equipment is None``: nothing is known about the inventory.
    - an item listed with ``available`` true/false/null: AVAILABLE,
      UNAVAILABLE or UNKNOWN respectively.
    - an item *not listed*: UNKNOWN, unless ``equipment_inventory_complete``
      is ``True``, in which case it is UNAVAILABLE.

    Cold storage capacity is the *free* space in liters; ``None`` means
    unknown and is never inferred from servings.
    """

    equipment: tuple[KitchenEquipment, ...] | None = None
    equipment_inventory_complete: bool | None = Field(
        default=None,
        description=(
            "True only when Dona Maria confirmed the equipment list is "
            "exhaustive. None/False: unlisted items are unknown."
        ),
    )
    skills: tuple[str, ...] | None = None
    skills_inventory_complete: bool | None = Field(
        default=None,
        description=(
            "True only when Dona Maria confirms the reported skills form an "
            "exhaustive list; unmentioned skills otherwise remain unknown."
        ),
    )
    daily_available_minutes: int | None = Field(default=None, gt=0)
    target_servings_per_day: int | None = Field(default=None, gt=0)
    available_people: int | None = Field(default=None, gt=0)
    gas_available: bool | None = None
    electricity_available: bool | None = None
    water_available: bool | None = None
    refrigerator_available: bool | None = None
    refrigerator_free_capacity_liters: Decimal | None = Field(default=None, ge=0)
    freezer_available: bool | None = None
    freezer_free_capacity_liters: Decimal | None = Field(default=None, ge=0)

    @field_validator(
        "refrigerator_free_capacity_liters", "freezer_free_capacity_liters"
    )
    @classmethod
    def validate_storage_capacity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("Storage capacity must be finite and non-negative.")
        return value

    @model_validator(mode="after")
    def validate_knowledge_consistency(self) -> "KitchenProfile":
        if self.equipment_inventory_complete and self.equipment is None:
            raise ValueError(
                "equipment_inventory_complete=true requires an equipment list."
            )
        if self.skills_inventory_complete and self.skills is None:
            raise ValueError("skills_inventory_complete=true requires a skills list.")
        for kind in StorageKind:
            available, capacity = self.storage(kind)
            if available is False and capacity:
                raise ValueError(
                    f"{kind.value} is unavailable but has free capacity reported."
                )
        return self

    def storage(self, kind: StorageKind) -> tuple[bool | None, Decimal | None]:
        """Return ``(available, free_capacity_liters)`` for a storage kind."""
        if kind == StorageKind.REFRIGERATOR:
            return self.refrigerator_available, self.refrigerator_free_capacity_liters
        return self.freezer_available, self.freezer_free_capacity_liters

    @field_validator("skills")
    @classmethod
    def normalize_skills(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(normalize_label(skill) for skill in value)
        if len({comparison_key(skill) for skill in normalized}) != len(normalized):
            raise ValueError("Kitchen skills must be unique.")
        return normalized

    @model_validator(mode="after")
    def validate_equipment_inventory(self) -> "KitchenProfile":
        if self.equipment is not None:
            names = [comparison_key(item.name) for item in self.equipment]
            if len(names) != len(set(names)):
                raise ValueError("Kitchen equipment names must be unique.")
        return self

    def equipment_by_name(self) -> dict[str, KitchenEquipment]:
        """Return equipment indexed by its normalized name."""
        return {comparison_key(item.name): item for item in self.equipment or ()}

    def equipment_availability(self, name: str) -> EquipmentAvailability:
        """Explicit knowledge state for one equipment name (exact match only)."""
        if self.equipment is None:
            return EquipmentAvailability.UNKNOWN
        item = self.equipment_by_name().get(comparison_key(name))
        if item is not None:
            return item.availability
        if self.equipment_inventory_complete:
            return EquipmentAvailability.UNAVAILABLE
        return EquipmentAvailability.UNKNOWN

    def has_skill(self, skill: str) -> bool | None:
        """Return ``None`` when skill knowledge has not been elicited."""
        if self.skills is None:
            return None
        return comparison_key(skill) in {comparison_key(item) for item in self.skills}
