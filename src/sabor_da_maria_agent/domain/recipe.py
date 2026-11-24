"""Structured recipe models with explicit source provenance."""

import re
import unicodedata
from decimal import Decimal
from typing import Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sabor_da_maria_agent.domain.kitchen import (
    EquipmentRequirement,
    StorageRequirement,
    UtilityKind,
    comparison_key,
    normalize_label,
)


class RecipeModel(BaseModel):
    """Immutable base model for recipe-domain entities and value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RecipeSource(RecipeModel):
    """External source supporting the recipe's ingredients or method."""

    title: str = Field(min_length=1)
    url: AnyHttpUrl
    publisher: str | None = Field(default=None, min_length=1)

    @field_validator("title", "publisher")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Source text cannot be empty.")
        return normalized


class RecipeIngredient(RecipeModel):
    """Ingredient requirement for ONE batch (the recipe's stated yield).

    Amount and unit may be unknown during discovery; unknown is never zero.
    ``unit`` is kept exactly as reported (e.g. ``dente``, ``xicara``) even
    when the pantry cannot convert it, so the original measurement remains
    auditable. ``source_text`` optionally keeps the literal source line.
    """

    name: str = Field(min_length=1)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, min_length=1)
    preparation_note: str | None = None
    source_text: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        return normalize_label(value).casefold() if value is not None else None

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("Ingredient quantity must be positive and finite.")
        return value

    @model_validator(mode="after")
    def validate_quantity_unit_pair(self) -> Self:
        if (self.quantity is None) != (self.unit is None):
            raise ValueError("Ingredient quantity and unit must be provided together.")
        return self


_RECIPE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Recipe(RecipeModel):
    """A researched recipe candidate, independent of any LLM runtime.

    Operational requirements (equipment, skills, utilities) are tri-state:

    - ``None``: not yet researched or confirmed; must not be read as "none".
    - ``()``: explicitly checked, and the recipe requires nothing in that
      category.
    - non-empty tuple: confirmed requirements.
    """

    recipe_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    servings: int = Field(gt=0, description="Yield produced by one batch.")
    ingredients: tuple[RecipeIngredient, ...] = Field(min_length=1)
    sources: tuple[RecipeSource, ...] = Field(min_length=1)
    required_equipment: tuple[EquipmentRequirement, ...] | None = Field(
        default=None,
        description="None = not yet confirmed; () = confirmed none required.",
    )
    required_skills: tuple[str, ...] | None = Field(
        default=None,
        description="None = not yet confirmed; () = confirmed none required.",
    )
    required_utilities: tuple[UtilityKind, ...] | None = Field(
        default=None,
        description="None = not yet confirmed; () = confirmed none required.",
    )
    storage_requirements: tuple[StorageRequirement, ...] | None = Field(
        default=None,
        description="None = not yet confirmed; () = confirmed no cold storage.",
    )
    total_time_minutes: int | None = Field(
        default=None,
        gt=0,
        description="Duration of ONE batch as stated by the source.",
    )
    minimum_people: int | None = Field(
        default=None,
        gt=0,
        description="None = not stated by the source nor confirmed; never 1.",
    )
    preparation_steps: tuple[str, ...] = ()

    @field_validator("recipe_id")
    @classmethod
    def validate_recipe_id(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _RECIPE_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "recipe_id must contain lowercase letters, numbers, '_' or '-'."
            )
        return normalized

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", value).split())
        if not normalized:
            raise ValueError("Recipe text cannot be empty.")
        return normalized

    @field_validator("required_skills")
    @classmethod
    def normalize_required_skills(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(normalize_label(skill) for skill in value)
        if len({comparison_key(skill) for skill in normalized}) != len(normalized):
            raise ValueError("Required skills must be unique.")
        return normalized

    @field_validator("preparation_steps")
    @classmethod
    def normalize_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(step.split()) for step in value)
        if any(not step for step in normalized):
            raise ValueError("Preparation steps cannot be empty.")
        return normalized

    @model_validator(mode="after")
    def validate_unique_requirements(self) -> Self:
        if self.required_equipment is not None:
            equipment_names = [
                comparison_key(item.name) for item in self.required_equipment
            ]
            if len(equipment_names) != len(set(equipment_names)):
                raise ValueError("Required equipment names must be unique.")

        if self.required_utilities is not None and len(self.required_utilities) != len(
            set(self.required_utilities)
        ):
            raise ValueError("Required utilities must be unique.")

        if self.storage_requirements is not None:
            kinds = [item.kind for item in self.storage_requirements]
            if len(kinds) != len(set(kinds)):
                raise ValueError("Storage requirement kinds must be unique.")
        return self

    @property
    def unquantified_ingredients(self) -> tuple[str, ...]:
        """Ingredient names that still need quantities before complete costing."""
        return tuple(item.name for item in self.ingredients if item.quantity is None)
