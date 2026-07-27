"""Manager wiring must hold for EVERY concrete tenant-scoped model, not just one.

Two silent inversions are guarded here, and neither raises an error when it happens:

* Omitting `Meta.default_manager_name` lets Django pick the first manager by
  declaration order, which is the UNSCOPED one. Everything routed through
  `_default_manager` — admin `get_queryset`, `dumpdata`, reverse-FK related managers,
  `validate_unique` — then quietly runs across every tenant.
* A concrete subclass declaring a bare `class Meta:` REPLACES the parent's Meta rather
  than merging with it, dropping both names and reinstating the same inversion.

A single-model test would not catch the second, so this iterates all subclasses and
therefore covers the business tables added in later waves automatically.
"""

import pytest
from django.apps import apps as django_apps
from django.db import models

from apps.core.managers import TenantScopedManager
from apps.core.models import TenantScopedModel
from apps.core.tests.models import ExampleTenantModel


def _model_id(model: type[models.Model]) -> str:
    return model.__name__


def _concrete_tenant_scoped_models() -> list[type[models.Model]]:
    return [
        model
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    ]


def test_there_is_something_to_check() -> None:
    # Given the installed apps
    subclasses = _concrete_tenant_scoped_models()

    # When they are enumerated
    # Then the set is non-empty, so every assertion below is exercised rather than
    # iterating nothing and passing vacuously
    assert subclasses
    assert ExampleTenantModel in subclasses


EVERY_TENANT_MODEL = pytest.mark.parametrize(
    "model",
    _concrete_tenant_scoped_models(),
    ids=_model_id,
)


@EVERY_TENANT_MODEL
def test_the_default_manager_name_is_pinned_to_the_scoped_manager(
    model: type[models.Model],
) -> None:
    # Given a concrete tenant-scoped model
    # When its Meta is read
    # Then the default manager is named explicitly, not left to declaration order
    assert model._meta.default_manager_name == "objects"


@EVERY_TENANT_MODEL
def test_the_base_manager_name_is_pinned_to_the_unscoped_manager(
    model: type[models.Model],
) -> None:
    # Given a concrete tenant-scoped model
    # When its Meta is read
    # Then the base manager is named explicitly too
    assert model._meta.base_manager_name == "all_objects"


@EVERY_TENANT_MODEL
def test_the_default_manager_is_tenant_scoped(model: type[models.Model]) -> None:
    # Given a concrete tenant-scoped model
    # When the manager Django uses by default is inspected
    # Then it filters by tenant
    assert isinstance(model._default_manager, TenantScopedManager)


@EVERY_TENANT_MODEL
def test_the_base_manager_is_exactly_a_plain_manager(
    model: type[models.Model],
) -> None:
    # Given a concrete tenant-scoped model
    # When the manager Django uses for internal traversal is inspected
    # Then it is EXACTLY models.Manager. Identity, not isinstance: TenantScopedManager
    # subclasses Manager, so an isinstance check would pass even if the base manager
    # had been scoped — which would break related-object traversal and saving.
    assert type(model._base_manager) is models.Manager


@EVERY_TENANT_MODEL
def test_the_two_managers_are_not_the_same_object(model: type[models.Model]) -> None:
    # Given a concrete tenant-scoped model
    # When both managers are compared
    # Then they are distinct, so one cannot be silently aliasing the other
    assert model._default_manager is not model._base_manager


@EVERY_TENANT_MODEL
def test_the_subclass_meta_inherits_the_shared_meta(
    model: type[models.Model],
) -> None:
    # Given a concrete tenant-scoped model
    meta = model.Meta if hasattr(model, "Meta") else None

    # When its declared Meta is checked against the shared base
    # Then it inherits it. This is the structural cause of the two assertions above;
    # checking it directly names the mistake instead of only its symptom.
    assert meta is not None, f"{model.__name__} declares no Meta"
    assert issubclass(meta, TenantScopedModel.Meta), (
        f"{model.__name__}.Meta must inherit TenantScopedModel.Meta, or both manager "
        f"names are silently dropped"
    )
