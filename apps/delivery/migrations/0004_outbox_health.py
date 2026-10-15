"""A fixed operational aggregate, not permission to read another firm's rows."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0003_outboundmessage_redacted_at_and_more")]

    operations = [
        migrations.AddIndex(
            model_name="outboundmessage",
            index=models.Index(
                fields=["tenant", "state", "created_at"],
                condition=models.Q(
                    state__in=[
                        "na_fila",
                        "falhou_temporario",
                        "recusada_pelo_provedor",
                        "resultado_incerto",
                    ]
                ),
                name="outbound_health_idx",
            ),
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION public.delivery_outbox_health()
            RETURNS TABLE(oldest_queued_at timestamptz, dead_letters bigint)
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog
            SET row_security = off
            AS $$
                SELECT
                    min(created_at) FILTER (
                        WHERE state IN ('na_fila', 'falhou_temporario')
                    ),
                    count(*) FILTER (
                        WHERE state IN ('recusada_pelo_provedor', 'resultado_incerto')
                    )
                FROM public.delivery_outboundmessage
                WHERE state IN (
                    'na_fila', 'falhou_temporario',
                    'recusada_pelo_provedor', 'resultado_incerto'
                )
            $$;
            REVOKE ALL ON FUNCTION public.delivery_outbox_health()
                FROM PUBLIC, app_portal;
            GRANT EXECUTE ON FUNCTION public.delivery_outbox_health() TO app_runtime;
            """,
            reverse_sql="DROP FUNCTION public.delivery_outbox_health();",
        ),
    ]
