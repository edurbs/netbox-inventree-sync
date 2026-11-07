import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('extras', '0145_taggeditem_object_id_bigint'),
    ]

    operations = [
        migrations.CreateModel(
            name='InvenTreeInstance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100)),
                ('url', models.URLField()),
                ('api_token', models.CharField(max_length=255)),
                ('enabled', models.BooleanField(default=True)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'InvenTree instance',
                'verbose_name_plural': 'InvenTree instances',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SyncedObject',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('netbox_object_id', models.PositiveBigIntegerField()),
                ('inventree_pk', models.PositiveIntegerField(blank=True, null=True)),
                ('status', models.CharField(default='pending', max_length=20)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
                ('last_payload_hash', models.CharField(blank=True, max_length=64)),
                ('last_error', models.TextField(blank=True)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'synced object',
                'verbose_name_plural': 'synced objects',
                'ordering': ('mapping', 'netbox_object_id'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SyncLogEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('action', models.CharField(max_length=20)),
                ('payload', models.JSONField(default=dict)),
                ('response_status', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True)),
                ('synced', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='log_entries', to='netbox_inventree_sync.syncedobject')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'log entry',
                'verbose_name_plural': 'log entries',
                'ordering': ('-created',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SyncMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100)),
                ('inventree_endpoint', models.CharField(max_length=200)),
                ('match_netbox_field', models.CharField(blank=True, max_length=100)),
                ('match_inventree_field', models.CharField(blank=True, max_length=100)),
                ('reconcile_interval_minutes', models.PositiveSmallIntegerField(default=60)),
                ('enabled', models.BooleanField(default=False)),
                ('description', models.TextField(blank=True)),
                ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='netbox_inventree_sync.inventreeinstance')),
                ('netbox_content_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='contenttypes.contenttype')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'sync mapping',
                'verbose_name_plural': 'sync mappings',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SyncFieldPair',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('netbox_field', models.CharField(max_length=100)),
                ('netbox_subfield', models.CharField(blank=True, max_length=100)),
                ('inventree_field', models.CharField(max_length=100)),
                ('transform', models.CharField(default='none', max_length=20)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
                ('mapping', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='field_pairs', to='netbox_inventree_sync.syncmapping')),
            ],
            options={
                'verbose_name': 'field pair',
                'verbose_name_plural': 'field pairs',
                'ordering': ('netbox_field', 'inventree_field'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name='syncedobject',
            name='mapping',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='synced_objects', to='netbox_inventree_sync.syncmapping'),
        ),
        migrations.AddConstraint(
            model_name='syncmapping',
            constraint=models.UniqueConstraint(fields=('connection', 'netbox_content_type', 'inventree_endpoint'), name='netbox_inventree_sync_syncmapping_unique_endpoint'),
        ),
        migrations.AddConstraint(
            model_name='syncedobject',
            constraint=models.UniqueConstraint(fields=('mapping', 'netbox_object_id'), name='netbox_inventree_sync_syncedobject_unique_netbox_object'),
        ),
    ]
