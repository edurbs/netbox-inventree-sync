"""Navigation menu: an "InvenTree Sync" submenu with connections, mappings
and the two read-only status lists (synced objects, sync log)."""

from django.utils.translation import gettext_lazy as _

from netbox.choices import ButtonColorChoices
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label=_('InvenTree Sync'),
    groups=(
        (_('Sync'), (
            PluginMenuItem(
                link='plugins:netbox_inventree_sync:inventreeinstance_list',
                link_text=_('Connections'),
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_inventree_sync:inventreeinstance_add',
                        title=_('Add'),
                        icon_class='mdi mdi-plus-thick',
                        color=ButtonColorChoices.GREEN,
                    ),
                ),
            ),
            PluginMenuItem(
                link='plugins:netbox_inventree_sync:syncmapping_list',
                link_text=_('Mappings'),
                buttons=(
                    PluginMenuButton(
                        link='plugins:netbox_inventree_sync:syncmapping_add',
                        title=_('Add'),
                        icon_class='mdi mdi-plus-thick',
                        color=ButtonColorChoices.GREEN,
                    ),
                ),
            ),
        )),
        (_('Status'), (
            # Read-only lists: no add buttons.
            PluginMenuItem(
                link='plugins:netbox_inventree_sync:syncedobject_list',
                link_text=_('Synced objects'),
            ),
            PluginMenuItem(
                link='plugins:netbox_inventree_sync:synclogentry_list',
                link_text=_('Sync log'),
            ),
        )),
    ),
    icon_class='mdi mdi-file-tree',
)
