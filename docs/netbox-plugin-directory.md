# Roadmap: listing on the official NetBox plugin directory

Goal: get **netbox-inventree-sync** listed on the official NetBox plugin
directory at <https://netboxlabs.com/plugins/>.

This document records how that directory actually works (researched
2026-11-06) and the concrete steps to get listed. Treat it as the plan for
future development — work through the phases in order.

## How the directory works (important)

**There is no self-service submission process.** The directory is a
curated list (about 32 entries) maintained editorially by NetBox Labs in
their CMS. Entries cannot be added via pull request or web form.

Each listed plugin carries one or more classifications:

| Classification | Meaning | How it is earned |
|----------------|---------|------------------|
| Official | Maintained by NetBox Labs | n/a (we are community) |
| Certified | Quality, compatibility and maintenance criteria met; co-maintainer relationship with NetBox Labs | [Plugin Certification Program](https://github.com/netbox-community/netbox/wiki/Plugin-Certification-Program) |
| Compatible | Community plugin validated on NetBox Cloud and/or NetBox Enterprise | Validation by NetBox Labs (ask them) |
| Partner | Commercial partner integration (e.g. IP Fabric, Slurp'it) | Partnership with NetBox Labs |
| Community | Community-developed plugin listed in the directory | Editorial: ask NetBox Labs |

Our entry point is the **Community** classification.

The community-run discovery mechanism (separate from the directory) is the
GitHub topic [`netbox-plugin`](https://github.com/topics/netbox-plugin)
(~188 repositories, curated by the NetBox community). Tagging the repo is
cheap, expected, and likely how NetBox Labs finds plugins in the first
place.

## Phase 1 — prerequisites (must be done before requesting a listing)

Nothing here involves NetBox Labs; it is all on us. A directory entry that
points at an empty branch or a package nobody can install will not be
accepted.

### 1.1 Merge the plugin code to `main`

`main` currently contains only `.gitignore` and this `docs/` directory.
All plugin code lives on `feature/inventree-sync-plugin`. Merge that
branch (or its successor PR) into `main` so the repository root shows the
plugin (`netbox_inventree_sync/`, `pyproject.toml`, `README.md`).

### 1.2 Polish `pyproject.toml` metadata

Current `pyproject.toml` is minimal. Before a public listing, add:

- `authors`
- `license` and a classifier block (Python versions, development status)
- `[project.urls]`: homepage (this repo), documentation, changelog
- Keep the name `netbox-inventree-sync` (matches the repo and the import
  package `netbox_inventree_sync`).

### 1.3 Publish to PyPI

Every plugin on the directory page is pip-installable; the official
[plugin installation docs](https://netboxlabs.com/docs/netbox/plugins/installation/)
assume `pip install <name>`. Steps:

1. Tag a release: `git tag v0.1.0` (semver).
2. Build sdist + wheel (`python -m build`).
3. Upload to PyPI (`twine upload` or trusted publishing via GitHub
   Actions on the `v*` tag).
4. Record in the README: `pip install netbox-inventree-sync`.

### 1.4 Add GitHub topics

The repo currently has **no topics**. Add via repo *About* gear icon or:

```bash
gh repo edit edurbs/netbox-inventree-sync --add-topic netbox-plugin,netbox,inventree,sync
```

`netbox-plugin` is the one that matters for discovery.

### 1.5 Harden the public-facing docs

- README: installation (PyPI), configuration (`PLUGINS` /
  `PLUGINS_CONFIG`), screenshots (already in `docs/screenshots/` on the
  feature branch), support/issue links.
- Add `COMPATIBILITY.md` at the repo root with the NetBox version matrix
  per release — this is now officially recommended by the
  [plugin development docs](https://netboxlabs.com/docs/netbox/plugins/development/).
- Make sure `min_version`/`max_version` in the `PluginConfig` match the
  matrix.

### 1.6 Release hygiene

- GitHub Releases with changelog notes per tag.
- Issues enabled (they are) and a response workflow.

## Phase 2 — request the listing

Once Phase 1 is done, ask NetBox Labs to add us. Two channels, use both:

1. **Contact form**: <https://netboxlabs.com/contact-us/> — request the
   plugin be listed in the plugins directory with the *Community*
   classification. Include: repo URL, PyPI name, one-line description,
   NetBox versions supported.
2. **NetDev Slack** (<https://netdev.chat/>), `#netbox` channel — where
   plugin authors and NetBox Labs staff interact; a short announcement
   of the plugin plus the same request.

Expect an editorial decision, not a queue. If the answer is "not yet",
the usual reasons are maturity/adoption — keep shipping releases and
re-ask after notable adoption (stars, issues, downstream users).

## Phase 3 — optional badges (later)

- **Compatible**: validated on NetBox Cloud / NetBox Enterprise. Worth
  requesting once the plugin is stable; it materially widens the
  installable base (NetBox Cloud users can only run validated plugins).
- **Certified**: co-maintainer relationship with NetBox Labs under the
  Plugin Certification Program. Only relevant if we want NetBox Labs
  co-maintaining; decide later.

## Bonus — community visibility (open to anyone)

Submit a PR to [netbox-community/awesome-netbox](https://github.com/netbox-community/awesome-netbox)
adding this plugin under its plugins section. That list is
community-curated and mergeable by PR, unlike the NetBox Labs directory.

## Checklist

- [ ] 1.1 Plugin code merged to `main`
- [ ] 1.2 `pyproject.toml` metadata complete
- [ ] 1.3 Published to PyPI (`netbox-inventree-sync`)
- [ ] 1.4 Topics added (`netbox-plugin` at minimum)
- [ ] 1.5 README + `COMPATIBILITY.md` in place
- [ ] 1.6 `v0.1.0` GitHub release with notes
- [ ] 2.1 Contact form submitted to NetBox Labs
- [ ] 2.2 Announced in NetDev Slack `#netbox`
- [ ] Listed at netboxlabs.com/plugins (Community)
- [ ] awesome-netbox PR merged

## References

- Directory: <https://netboxlabs.com/plugins/>
- Plugin development docs: <https://netboxlabs.com/docs/netbox/plugins/development/>
- Plugin installation docs: <https://netboxlabs.com/docs/netbox/plugins/installation/>
- Plugin Certification Program: <https://github.com/netbox-community/netbox/wiki/Plugin-Certification-Program>
- `netbox-plugin` GitHub topic: <https://github.com/topics/netbox-plugin>
- awesome-netbox: <https://github.com/netbox-community/awesome-netbox>
- NetDev Slack: <https://netdev.chat/>
