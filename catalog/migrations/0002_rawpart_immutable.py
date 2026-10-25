from django.db import migrations

CREATE = """
CREATE FUNCTION catalog_rawpart_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'catalog_rawpart rows are append only: % refused', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER rawpart_append_only
    BEFORE UPDATE OR DELETE ON catalog_rawpart
    FOR EACH ROW EXECUTE FUNCTION catalog_rawpart_immutable();
"""

DROP = """
DROP TRIGGER rawpart_append_only ON catalog_rawpart;
DROP FUNCTION catalog_rawpart_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunSQL(CREATE, DROP)]
