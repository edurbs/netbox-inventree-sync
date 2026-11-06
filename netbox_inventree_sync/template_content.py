"""Plugin content injected into NetBox's object detail pages.

One left-page panel summarizing an object's InvenTree sync state —
status badge, "synced X ago", a link to the remote counterpart and one
to the plugin's synced-objects list. Rendered by
``netbox/ui/panels.py``'s ``PluginContentPanel('left_page')``, which
NetBox's ``SimpleLayout`` places in the left column of every object
detail page built on it (dcim.inventoryitem included).

**Why one globally registered class, not per-content-type classes.**
NetBox's template-extension registry
(``register_template_extensions``) buckets extension classes by the
labels in the class's ``models`` attribute, with ``models = None``
meaning *every* model — the registry then consults the class for each
object page render (``utilities/templatetags/plugins.py``). Factory-
generating one subclass per mapped content type (the alternative) has
to compute that class list at import/``ready()`` time, where Django
forbids database access (the tables may not even exist yet — the same
reason ``signals.py`` defers its binding build), and the list would go
stale the moment a mapping for a new content type is created. A single
global class instead computes *at render time* — always current for
newly enabled mappings, safe at boot — and pays for it with one cheap
guard query (an ``exists()`` on the tiny mappings table) on detail
pages of models with no enabled mapping, where the panel simply
renders nothing.

The never-raise contract of ``signals.py`` applies here too: a panel
is decoration on somebody else's page, so any failure is logged and
swallowed — the object page must render regardless.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from netbox.plugins.templates import PluginTemplateExtension

from netbox_inventree_sync.models import SyncMapping, SyncedObject
from netbox_inventree_sync.tables import (
    STATUS_BADGE_COLORS, _inventree_object_label, _inventree_object_url,
)

__all__ = (
    'InvenTreeSyncPanel',
    'template_extensions',
)

logger = logging.getLogger('netbox_inventree_sync.template_content')

PANEL_TEMPLATE = 'netbox_inventree_sync/sync_panel.html'


class InvenTreeSyncPanel(PluginTemplateExtension):
    """InvenTree sync state of the object being viewed (left column).

    Rendered for every object; ``left_page()`` decides per render:
    rows exist → one line per mapping; none but an enabled mapping
    covers the model → the muted "not synced" note; no enabled mapping
    → no panel at all.
    """

    def left_page(self):
        obj = self.context.get('object')
        if obj is None or not getattr(obj, 'pk', None):
            return ''
        try:
            return self._render_panel(obj)
        except Exception:
            logger.exception(
                'netbox_inventree_sync: rendering the InvenTree panel for %r #%s failed; '
                'the object page continues without it', type(obj).__name__, obj.pk,
            )
            return ''

    def _render_panel(self, obj):
        content_type = ContentType.objects.get_for_model(obj)
        # Guard first: the mappings table is tiny, so detail pages of
        # unmapped models pay a single indexed query and stop here.
        if not SyncMapping.objects.filter(netbox_content_type=content_type, enabled=True).exists():
            return ''
        synced_rows = SyncedObject.objects.filter(
            mapping__netbox_content_type=content_type,
            netbox_object_id=obj.pk,
        ).select_related('mapping', 'mapping__connection')
        rows = [
            {
                'synced': synced,
                'label': _inventree_object_label(
                    synced.mapping.inventree_endpoint, synced.inventree_pk,
                ),
                'url': _inventree_object_url(
                    synced.mapping.connection, synced.mapping.inventree_endpoint, synced.inventree_pk,
                ),
                'badge_color': STATUS_BADGE_COLORS.get(synced.status, 'secondary'),
            }
            for synced in synced_rows
        ]
        return self.render(PANEL_TEMPLATE, {'sync_rows': rows})


# Loaded by PluginConfig.ready() through the default resource path
# (template_content.template_extensions) — no __init__.py wiring needed.
template_extensions = [InvenTreeSyncPanel]
