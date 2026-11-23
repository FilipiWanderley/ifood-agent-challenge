from decimal import Decimal

import pytest
from pydantic import ValidationError

from sabor_da_maria_agent.domain.kitchen import EquipmentRequirement, UtilityKind
from sabor_da_maria_agent.domain.recipe import Recipe, RecipeIngredient, RecipeSource


def make_recipe(**overrides: object) -> Recipe:
    data: dict[str, object] = {
        "recipe_id": "galinhada-15",
        "name": " Galinhada  ",
        "description": "Frango com arroz",
        "servings": 8,
        "ingredients": (
            RecipeIngredient(
                name="Peito de frango",
                quantity=Decimal(1000),
                unit="g",
            ),
            RecipeIngredient(name="Sal"),
        ),
        "sources": (
            RecipeSource.model_validate(
                {
                    "title": "Receita de galinhada",
                    "url": "https://example.com/galinhada",
                    "publisher": "Example Kitchen",
                }
            ),
        ),
        "required_equipment": (EquipmentRequirement(name="Panela de pressão"),),
        "required_skills": ("Cozinhar arroz",),
        "required_utilities": (UtilityKind.GAS,),
        "total_time_minutes": 60,
    }
    data.update(overrides)
    return Recipe.model_validate(data)


def test_recipe_normalizes_identity_and_exposes_unquantified_ingredients() -> None:
    recipe = make_recipe()

    assert recipe.recipe_id == "galinhada-15"
    assert recipe.name == "Galinhada"
    assert recipe.unquantified_ingredients == ("Sal",)
    assert recipe.required_skills == ("Cozinhar arroz",)


def test_recipe_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError):
        make_recipe(sources=())


def test_recipe_rejects_non_http_source() -> None:
    with pytest.raises(ValidationError):
        RecipeSource.model_validate(
            {"title": "Local file", "url": "file:///tmp/recipe.html"}
        )


def test_recipe_rejects_invalid_identifier() -> None:
    with pytest.raises(ValidationError):
        make_recipe(recipe_id="Galinhada 15")


def test_ingredient_quantity_and_unit_must_be_provided_together() -> None:
    with pytest.raises(ValidationError):
        RecipeIngredient(name="Arroz", quantity=Decimal(100))


def test_ingredient_quantity_must_be_finite_and_positive() -> None:
    with pytest.raises(ValidationError):
        RecipeIngredient(name="Arroz", quantity=Decimal("NaN"), unit="g")


def test_recipe_rejects_duplicate_required_equipment() -> None:
    requirement = EquipmentRequirement(name="Fogão")
    with pytest.raises(ValidationError):
        make_recipe(required_equipment=(requirement, requirement))


def test_recipe_is_immutable() -> None:
    recipe = make_recipe()
    with pytest.raises(ValidationError):
        recipe.name = "Outra receita"  # type: ignore[misc]


def test_recipe_rejects_duplicate_required_utilities() -> None:
    with pytest.raises(ValidationError):
        make_recipe(required_utilities=(UtilityKind.GAS, UtilityKind.GAS))


REQUIREMENT_FIELDS = ("required_equipment", "required_skills", "required_utilities")


def test_omitted_requirements_default_to_unknown_not_empty() -> None:
    payload = make_recipe().model_dump(exclude=set(REQUIREMENT_FIELDS))
    recipe = Recipe.model_validate(payload)

    assert recipe.required_equipment is None
    assert recipe.required_skills is None
    assert recipe.required_utilities is None


def test_explicitly_empty_requirements_are_preserved_as_confirmed_none() -> None:
    recipe = make_recipe(
        required_equipment=(),
        required_skills=(),
        required_utilities=(),
    )

    assert recipe.required_equipment == ()
    assert recipe.required_skills == ()
    assert recipe.required_utilities == ()


@pytest.mark.parametrize("field", REQUIREMENT_FIELDS)
def test_explicit_none_requirement_is_accepted_per_category(field: str) -> None:
    recipe = make_recipe(**{field: None})

    assert getattr(recipe, field) is None
    other_fields = [name for name in REQUIREMENT_FIELDS if name != field]
    assert all(getattr(recipe, name) for name in other_fields)


def test_requirement_tri_state_survives_json_round_trip() -> None:
    recipe = make_recipe(required_equipment=None, required_skills=())

    payload = recipe.model_dump(mode="json")
    restored = Recipe.model_validate_json(recipe.model_dump_json())

    assert payload["required_equipment"] is None
    assert payload["required_skills"] == []
    assert payload["required_utilities"] == ["gas"]
    assert restored == recipe
    assert restored.required_equipment is None
    assert restored.required_skills == ()


def test_required_skills_are_still_normalized_and_deduplicated() -> None:
    recipe = make_recipe(required_skills=("  Cozinhar   arroz ",))
    assert recipe.required_skills == ("Cozinhar arroz",)

    with pytest.raises(ValidationError):
        make_recipe(required_skills=("galinhada", " GALINHADA "))


def test_recipe_still_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        make_recipe(requirements_confirmed=True)


def test_requirement_fields_remain_immutable() -> None:
    recipe = make_recipe(required_skills=None)
    with pytest.raises(ValidationError):
        recipe.required_skills = ()  # type: ignore[misc]
