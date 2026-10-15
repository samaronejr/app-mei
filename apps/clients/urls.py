"""Client registry routes."""

from django.urls import path

from apps.clients.views import (
    clients_detail_view,
    clients_list_view,
    export_clients_csv,
)
from apps.clients.views_import import (
    import_batch_view,
    import_commit_view,
    import_errors_csv_view,
    import_rollback_view,
    import_upload_view,
)
from apps.clients.views_onboarding import (
    assignment_add_view,
    assignment_remove_view,
    client_create_view,
    client_edit_view,
    client_setup_view,
    contact_create_view,
    contact_edit_view,
    contact_invite_view,
    engagement_create_view,
    engagement_edit_view,
    tax_profile_view,
)

urlpatterns = [
    path("clientes/", clients_list_view, name="clients-list"),
    path("clientes/exportar.csv", export_clients_csv, name="clients-export-csv"),
    # Literal paths ahead of the converter patterns, for the reason given below.
    path("clientes/novo/", client_create_view, name="client-create"),
    path("clientes/importar/", import_upload_view, name="client-import"),
    path(
        "clientes/importar/<uuid:batch_pk>/",
        import_batch_view,
        name="client-import-batch",
    ),
    path(
        "clientes/importar/<uuid:batch_pk>/confirmar/",
        import_commit_view,
        name="client-import-commit",
    ),
    path(
        "clientes/importar/<uuid:batch_pk>/desfazer/",
        import_rollback_view,
        name="client-import-rollback",
    ),
    path(
        "clientes/importar/<uuid:batch_pk>/erros.csv",
        import_errors_csv_view,
        name="client-import-errors",
    ),
    # Last, and after the literal export path. Django resolves in declaration order, so
    # a converter pattern written above `exportar.csv` would be consulted first — and
    # while `<uuid:pk>` happens not to match that filename today, ordering the specific
    # path ahead of the general one is what keeps that true if the converter is ever
    # widened. The registry's own path ends in a slash, so this one does too.
    path("clientes/<uuid:pk>/", clients_detail_view, name="client-detail"),
    path("clientes/<uuid:pk>/editar/", client_edit_view, name="client-edit"),
    path("clientes/<uuid:pk>/cadastro/", client_setup_view, name="client-setup"),
    path(
        "clientes/<uuid:pk>/servicos/",
        engagement_create_view,
        name="client-engagement-create",
    ),
    path(
        "clientes/<uuid:pk>/servicos/<uuid:engagement_pk>/",
        engagement_edit_view,
        name="client-engagement-edit",
    ),
    path(
        "clientes/<uuid:pk>/perfil-tributario/",
        tax_profile_view,
        name="client-tax-profile",
    ),
    path(
        "clientes/<uuid:pk>/contatos/",
        contact_create_view,
        name="client-contact-create",
    ),
    path(
        "clientes/<uuid:pk>/contatos/<uuid:contact_pk>/",
        contact_edit_view,
        name="client-contact-edit",
    ),
    path(
        "clientes/<uuid:pk>/contatos/<uuid:contact_pk>/convidar/",
        contact_invite_view,
        name="client-contact-invite",
    ),
    path(
        "clientes/<uuid:pk>/responsaveis/",
        assignment_add_view,
        name="client-assignment-add",
    ),
    path(
        "clientes/<uuid:pk>/responsaveis/<uuid:assignment_pk>/remover/",
        assignment_remove_view,
        name="client-assignment-remove",
    ),
]
