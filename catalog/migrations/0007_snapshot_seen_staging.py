from django.db import migrations, models


def mark_the_pass_in_progress(apps, schema_editor):
    """A pass that was still running when this migration ran has read its parts into rows that now mean "the last completed
    pass"; they are staging, and become the completed set when that pass completes."""
    SnapshotSeen = apps.get_model("catalog", "SnapshotSeen")
    SnapshotSeen.objects.filter(checkpoint__completed=False).update(staging=True)


def nothing(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_pass_id_on_the_checkpoint'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='snapshotseen',
            name='one_seen_per_pass',
        ),
        migrations.AddField(
            model_name='snapshotseen',
            name='staging',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(mark_the_pass_in_progress, nothing),
        migrations.AddConstraint(
            model_name='snapshotseen',
            constraint=models.UniqueConstraint(fields=('checkpoint', 'staging', 'part_number_raw', 'brand_raw'), name='one_seen_per_pass'),
        ),
    ]
