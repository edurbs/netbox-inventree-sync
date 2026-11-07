# Compatibility

Supported software combinations for the netbox-inventree-sync plugin:

| Component | Requirement |
|-----------|-------------|
| NetBox    | ≥ 4.7 (tested with 4.7.4) |
| InvenTree | 1.7 series (tested with 1.7.0) |
| Python    | ≥ 3.12 |

The NetBox version floor is enforced via `PluginConfig.min_version`; there is
intentionally no `max_version` — newer NetBox minors are expected to work,
so report breakage as issues.
