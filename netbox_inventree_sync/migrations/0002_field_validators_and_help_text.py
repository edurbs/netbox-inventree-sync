import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_inventree_sync', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='syncmapping',
            name='reconcile_interval_minutes',
            field=models.PositiveSmallIntegerField(default=60, validators=[django.core.validators.MinValueValidator(1)]),
        ),
    ]
