# Plugin directory listing — Phase 1 + Bonus (design)

Date: 2026-11-06 · Issue: edurbs/netbox-inventree-sync#4

## Intent

Get the repository into the shape required for a *Community* listing on
the official NetBox plugin directory (issue 4, Phase 1) and add community
visibility (Bonus).

## Why

The directory is the discovery channel for NetBox plugins, and NetBox
Labs lists plugins editorially — there is no self-service submission.
Phase 1 is the part that is entirely ours: metadata, PyPI, topics, docs,
a release. Without it, a listing request points at a package nobody can
`pip install`. Checklist item 1.1 (merge plugin code to `main`) is
already done via PR #1.

## Goals

- **1.2** complete `pyproject.toml` metadata + MIT `LICENSE` file
- **1.3** publish `netbox-inventree-sync` 0.1.0 to PyPI via GitHub
  Actions trusted publishing
- **1.4** add repo topics (`netbox-plugin`, `netbox`, `inventree`, `sync`)
- **1.5** README install/support polish + `COMPATIBILITY.md` +
  `PluginConfig.min_version`
- **1.6** tag `v0.1.0` + GitHub Release with notes
- **Bonus** PR to awesome-netbox's *Synchronization* section; update the
  issue 4 checklist to match reality

## Non-goals

- Phase 2 (NetBox Labs contact form, NetDev Slack announcement)
- Phase 3 (Compatible / Certified badges)
- Any plugin logic changes

## Approaches considered

- **PyPI publishing** — (a) GitHub Actions trusted publishing,
  (b) manual `twine` + API token, (c) prepare-only, no publish.
  Picked (a): no stored secrets, every future release is just a tag
  push, at the cost of a one-time owner registration of the pending
  publisher on PyPI.
- **Bonus target** — awesome-netbox's *Plugins* section no longer lists
  individual plugins (it is a single link to netboxlabs.com). Options:
  PR under *Plugins* as the issue literally says (near-certain
  rejection), skip the bonus, or PR under the *Synchronization* section
  next to `netbox-sync` / `netbox-zabbix-sync` / `netbox2netshot`.
  Picked *Synchronization*: matches the list's current curation style.
- **License** — MIT (picked) over Apache-2.0 / GPL-3.0: short,
  permissive, the common choice among community NetBox plugins.

## Decisions

- `authors = ["Eduardo Soares <edurbs@gmail.com>"]` — public GitHub
  email; owner may swap for the noreply address
- Classifiers: MIT, Development Status :: 4 - Beta, Python 3.12 / 3.13,
  Intended Audience :: System Administrators
- `[project.urls]`: homepage (this repo), changelog (releases page),
  documentation (repo `docs/`)
- `PluginConfig.min_version = '4.7'` and **no** `max_version` — max
  pins break artificially on NetBox minor releases; the tested matrix
  lives in `COMPATIBILITY.md` instead
- `COMPATIBILITY.md`: NetBox >= 4.7 (tested 4.7.4), InvenTree 1.7
  (tested 1.7.0), Python >= 3.12
- `.github/workflows/release.yml`: on `v*` tag push → `python -m build`
  → `pypa/gh-action-pypi-publish` with `id-token: write`, no GitHub
  environment (keeps the PyPI registration simple)
- Version stays `0.1.0`; first tag `v0.1.0`
- README: `pip install netbox-inventree-sync` becomes the primary
  install; git+ kept as the dev alternative; add support/issue links and
  a license note
- Repo changes land via a PR to `main` (matches repo history)

## Owner steps (cannot be done by the agent)

- Register the trusted publisher at
  <https://pypi.org/manage/account/publishing/> — owner `edurbs`, repo
  `netbox-inventree-sync`, workflow `release.yml` — **before** the tag
  is pushed
- Merge the repo PR; submit/approve the awesome-netbox PR

## Left to the builder

- Exact release-notes wording, final README phrasing, classifier
  fine-tuning within the set above
