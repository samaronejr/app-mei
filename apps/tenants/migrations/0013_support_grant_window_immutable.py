from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tenants", "0012_support_grant")]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION tenants_support_grant_window_guard()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.approved_by_owner_id IS NULL AND
                       (NEW.starts_at IS NOT NULL OR NEW.expires_at IS NOT NULL)
                    THEN
                        RAISE EXCEPTION 'support window requires owner approval'
                            USING ERRCODE = 'check_violation',
                            CONSTRAINT = 'support_grant_window_requires_owner_approval';
                    END IF;
                    IF TG_OP = 'UPDATE' AND OLD.approved_by_owner_id IS NOT NULL AND (
                        NEW.starts_at IS DISTINCT FROM OLD.starts_at OR
                        NEW.expires_at IS DISTINCT FROM OLD.expires_at OR
                        NEW.approved_at IS DISTINCT FROM OLD.approved_at OR
                        NEW.approved_by_owner_id IS DISTINCT FROM OLD.approved_by_owner_id
                    ) THEN
                        RAISE EXCEPTION 'approved support window is immutable'
                            USING ERRCODE = 'check_violation',
                            CONSTRAINT = 'support_grant_approved_window_immutable';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                CREATE TRIGGER tenants_support_grant_window_guard
                    BEFORE INSERT OR UPDATE ON tenants_supportgrant
                    FOR EACH ROW EXECUTE FUNCTION tenants_support_grant_window_guard();
            """,
            reverse_sql="""
                DROP TRIGGER tenants_support_grant_window_guard ON tenants_supportgrant;
                DROP FUNCTION tenants_support_grant_window_guard();
            """,
        ),
    ]
