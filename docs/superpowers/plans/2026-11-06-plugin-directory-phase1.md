# Plugin Directory Phase 1 + Bonus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #4 Phase 1 (PyPI-ready metadata, docs, topics, v0.1.0 published via trusted publishing) plus the Bonus (awesome-netbox *Synchronization* PR).

**Architecture:** Chores only — no plugin logic changes. Metadata/LICENSE/docs/workflow edits land as one PR to `main` from `feature/directory-phase1`; a tag-triggered GitHub Actions workflow publishes to PyPI via OIDC trusted publishing; external actions (topics, tag, release, awesome-netbox PR, issue checklist) run through `gh`.

**Tech Stack:** setuptools/pyproject (PEP 639 metadata), `python -m build` + `twine check`, GitHub Actions (`pypa/gh-action-pypi-publish@release/v1`), `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-11-06-plugin-directory-phase1-design.md`

## Global Constraints

- Package name stays `netbox-inventree-sync`; version stays `0.1.0`; first tag is `v0.1.0`.
- `requires-python` stays `>=3.12`; no new runtime dependencies.
- No plugin logic changes — only metadata, docs, CI, and GitHub/external actions.
- The workflow file MUST be `.github/workflows/release.yml` — the PyPI trusted-publisher registration references that exact name.
- `min_version = '4.7'`, NO `max_version` in `PluginConfig`.
- Tested matrix, verbatim: NetBox 4.7.4, InvenTree 1.7.0, Python ≥ 3.12.
- License: MIT, copyright `2026 Eduardo Soares`; author `Eduardo Soares <edurbs@gmail.com>`.
- Repo topics: `netbox-plugin`, `netbox`, `inventree`, `sync`.
- All work on branch `feature/directory-phase1` (worktree at `~/.local/share/opencode/worktree/802c8b/plugin-directory-phase1`); everything lands via PR to `main`. Never push to `main` directly.
- A `.venv` with `build` + `twine` already exists in the worktree (baseline). Use `.venv/bin/python`.
- **OWNER gates** (PR merge, PyPI publisher registration) pause and ask the human; the agent cannot do them.

## Review Focus

1. **Trusted-publisher registration mismatch** (owner/repo/workflow filename) → publish fails with 403. Pinned: filename `release.yml`, no environment; Task 6 verifies registration BEFORE tagging and the run + PyPI JSON after.
2. **PEP 639 license metadata** — `license = "MIT"` is an SPDX expression needing setuptools ≥ 77 and must NOT be paired with a `License ::` classifier (setuptools rejects the combo). Pinned in Task 1; verified via `License-Expression: MIT` in built metadata.
3. **Artifact completeness after pyproject edits** — `LICENSE` must ship in the sdist; the wheel's module list must not shrink. Verified by listing both artifacts (Task 1).
4. **COMPATIBILITY.md vs `PluginConfig.min_version` drift** — both must say 4.7. Verified by grepping both (Task 2).
5. **README install drift** — the production section must lead with `pip install netbox-inventree-sync`; the git+ form stays only as the dev alternative. Verified by grep (Task 3).

---

### Task 1: Package metadata + LICENSE (checklist 1.2)

**Files:**
- Create: `LICENSE`
- Modify: `pyproject.toml`, `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: upload-ready distributions whose METADATA carries the author, `License-Expression: MIT`, `License-File: LICENSE`, the five classifiers, and three `Project-URL` entries. Tasks 5–7 depend on the package building cleanly.

- [ ] **Step 1: Create `LICENSE`** — the standard MIT text; line 1 `MIT License`, copyright line `Copyright (c) 2026 Eduardo Soares`.

- [ ] **Step 2: Edit `pyproject.toml`**
  - `[build-system] requires = ["setuptools>=77"]` (PEP 639 support).
  - `authors = [{ name = "Eduardo Soares", email = "edurbs@gmail.com" }]`
  - `license = "MIT"` — SPDX expression; do NOT add any `License ::` classifier.
  - `classifiers`: `Development Status :: 4 - Beta`, `Intended Audience :: System Administrators`, `Programming Language :: Python :: 3.12`, `Programming Language :: Python :: 3.13`, `Topic :: System :: Systems Administration`.
  - `[project.urls]`: `Homepage = "https://github.com/edurbs/netbox-inventree-sync"`, `Documentation = "https://github.com/edurbs/netbox-inventree-sync/tree/main/docs"`, `Changelog = "https://github.com/edurbs/netbox-inventree-sync/releases"`.
  - Everything else (name, version, deps, packages.find) unchanged.

- [ ] **Step 3: Append `dist/` to `.gitignore`** (build residue from `python -m build`).

- [ ] **Step 4: Rebuild and verify metadata**

```bash
rm -rf dist/ && .venv/bin/python -m build && .venv/bin/twine check dist/*
tar -xOf dist/netbox_inventree_sync-0.1.0.tar.gz netbox_inventree_sync-0.1.0/PKG-INFO | grep -E "Author-email|License-Expression|Classifier|Project-URL"
tar -tf dist/netbox_inventree_sync-0.1.0.tar.gz | grep -c LICENSE
```

Expected: twine `PASSED` for both; PKG-INFO shows `Author-email: Eduardo Soares <edurbs@gmail.com>`, `License-Expression: MIT`, all 5 classifiers, 3 `Project-URL:` lines; sdist lists `LICENSE` ≥ 1. Also verify the wheel's module list did not shrink:

```bash
python3 -c "import zipfile; print(len([n for n in zipfile.ZipFile('dist/netbox_inventree_sync-0.1.0-py3-none-any.whl').namelist() if n.endswith('.py')]))"
```

Expected: same count as the pre-edit baseline (record it before editing).

- [ ] **Step 5: Commit**

```bash
git add LICENSE pyproject.toml .gitignore
git commit -m "chore: complete package metadata, add MIT license (1.2)"
```

---

### Task 2: NetBox version floor + COMPATIBILITY.md (checklist 1.5)

**Files:**
- Modify: `netbox_inventree_sync/__init__.py` (class `NetBoxInvenTreeSyncConfig`)
- Create: `COMPATIBILITY.md`

**Interfaces:**
- Consumes: nothing.
- Produces: `min_version = '4.7'` on `PluginConfig` (NetBox enforces it at plugin load); `COMPATIBILITY.md` is the human-facing matrix README links to.

- [ ] **Step 1: Add `min_version = '4.7'`** to `NetBoxInvenTreeSyncConfig`, placed with the other class attributes (next to `version`). Do not add `max_version`.

- [ ] **Step 2: Write `COMPATIBILITY.md`** — title, one intro sentence, then a matrix table with exactly:

| Component | Requirement |
|-----------|-------------|
| NetBox    | ≥ 4.7 (tested with 4.7.4) |
| InvenTree | 1.7 series (tested with 1.7.0) |
| Python    | ≥ 3.12 |

Then a short note: the NetBox floor is enforced via `PluginConfig.min_version`; there is intentionally no `max_version` — newer NetBox minors are expected to work, report breakage as issues.

- [ ] **Step 3: Verify consistency and syntax**

```bash
grep -n "min_version" netbox_inventree_sync/__init__.py
grep -n "4\.7" COMPATIBILITY.md
python3 -c "import ast; ast.parse(open('netbox_inventree_sync/__init__.py').read()); print('syntax OK')"
```

Expected: `min_version = '4.7'`; COMPATIBILITY.md mentions 4.7.4; `syntax OK`. (A full import needs a NetBox install — not run here.)

- [ ] **Step 4: Commit**

```bash
git add netbox_inventree_sync/__init__.py COMPATIBILITY.md
git commit -m "feat: enforce NetBox >= 4.7 floor, add COMPATIBILITY.md (1.5)"
```

---

### Task 3: README install + support polish (checklist 1.5)

**Files:**
- Modify: `README.md` (production-install block around line 80; end of file)

**Interfaces:**
- Consumes: the PyPI name `netbox-inventree-sync` (published in Task 6).
- Produces: README whose production section installs from PyPI.

- [ ] **Step 1: Rewrite the production install command** — in "Installing into a running production environment", replace the `pip install git+https://github.com/edurbs/netbox-inventree-sync.git` block with `pip install netbox-inventree-sync`. Keep one line below it: the git+ form remains the option for tracking development. Fix any prose that implies GitHub-only installation.

- [ ] **Step 2: Add Support and License sections at the end** — Support: "Issues: https://github.com/edurbs/netbox-inventree-sync/issues". License: MIT, linking `LICENSE`. Also one line linking `COMPATIBILITY.md` for the supported version matrix.

- [ ] **Step 3: Verify**

```bash
grep -n "pip install netbox-inventree-sync" README.md
grep -n "issues\|LICENSE.md\|COMPATIBILITY.md" README.md
```

Expected: the PyPI install appears in the production section; git+ appears only as the dev alternative; Support/License/Compatibility lines present.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: PyPI-first install, support/license sections (1.5)"
```

---

### Task 4: Release workflow (checklist 1.3)

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: a `v*`-tag-triggered publish job using OIDC trusted publishing. Task 6 pushes the tag that triggers it.

- [ ] **Step 1: Write the workflow** — exact content:

```yaml
name: release

on:
  push:
    tags:
      - "v*"

jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install build
      - run: python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

No `environment:` key — the PyPI registration (Task 6, owner step) is done without one.

- [ ] **Step 2: Verify** the YAML parses (and actionlint if present):

```bash
.venv/bin/pip install -q pyyaml
.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml')); print('yaml OK')"
command -v actionlint && actionlint .github/workflows/release.yml || true
```

Expected: `yaml OK`; actionlint clean if available.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: tag-triggered PyPI publish via trusted publishing (1.3)"
```

---

### Task 5: Land the PR + repo topics (checklist 1.1 tick, 1.4)

**Files:** none in-repo (push, PR, `gh repo edit`).

**Interfaces:**
- Consumes: commits from Tasks 1–4.
- Produces: merged `main` containing all Phase 1 repo changes; topics set.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feature/directory-phase1
gh pr create --base main --title "Plugin directory prerequisites: metadata, docs, release workflow" --body "Phase 1 of #4 (items 1.2–1.5). 1.1 already done in #1."
```

- [ ] **Step 2: OWNER GATE — ask the human to review and merge the PR.** Wait. After merge, sync: `git fetch origin`.

- [ ] **Step 3: Add topics and verify**

```bash
gh repo edit edurbs/netbox-inventree-sync --add-topic netbox-plugin --add-topic netbox --add-topic inventree --add-topic sync
gh api repos/edurbs/netbox-inventree-sync/topics --jq '.names'
```

Expected: all four topics listed.

---

### Task 6: Publish v0.1.0 to PyPI + GitHub Release (checklist 1.3, 1.6)

**Files:** none in-repo (tag, workflow run, release).

**Interfaces:**
- Consumes: merged `main` with `.github/workflows/release.yml` (Task 5), owner's PyPI registration (Step 1).
- Produces: `netbox-inventree-sync 0.1.0` on PyPI; GitHub Release `v0.1.0`.

- [ ] **Step 1: OWNER GATE — ask the human to register the trusted publisher** at <https://pypi.org/manage/account/publishing/>: PyPI project name `netbox-inventree-sync`, owner `edurbs`, repository `netbox-inventree-sync`, workflow filename `release.yml`, workflow environment **blank**. Wait for their confirmation. Doing this before the tag exists is fine (pending publishers).

- [ ] **Step 2: Tag and push**

```bash
git fetch origin
git tag -a v0.1.0 -m "v0.1.0" origin/main
git push origin v0.1.0
```

- [ ] **Step 3: Watch the run**

```bash
sleep 10 && gh run list --repo edurbs/netbox-inventree-sync --limit 3
gh run watch --repo edurbs/netbox-inventree-sync $(gh run list --repo edurbs/netbox-inventree-sync --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: `release` workflow success. A 403 on publish means the registration doesn't match — recheck Step 1 details with the owner.

- [ ] **Step 4: Verify PyPI**

```bash
curl -sf https://pypi.org/pypi/netbox-inventree-sync/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
```

Expected: `0.1.0`.

- [ ] **Step 5: Create the GitHub Release** with real notes: first public release; one-paragraph what-it-does (NetBox plugin keeping InvenTree in sync via its REST API); tested matrix (NetBox 4.7.4, InvenTree 1.7.0, Python ≥ 3.12); `pip install netbox-inventree-sync`; PyPI link.

```bash
gh release create v0.1.0 --repo edurbs/netbox-inventree-sync --title "v0.1.0" --notes-file /tmp/opencode/release-notes.md
```

---

### Task 7: Bonus — awesome-netbox PR + issue #4 checklist update

**Files:** none in this repo (fork edit via API, PR, issue body edit).

**Interfaces:**
- Consumes: merged/published state from Tasks 5–6 (the PR body links the released package).
- Produces: submitted awesome-netbox PR; issue #4 checklist reflecting reality.

- [ ] **Step 1: Fork and branch** — `gh repo fork netbox-community/awesome-netbox --clone=false` (fork: `edurbs/awesome-netbox`); branch `add-netbox-inventree-sync`.

- [ ] **Step 2: Add the entry via the contents API** — fetch `README.md` from the fork's default branch (get its sha), add one line at the end of the **Synchronization** section (after the `sol1/icingaweb2-module-netbox` line), keeping list style:

```markdown
- [edurbs/netbox-inventree-sync](https://github.com/edurbs/netbox-inventree-sync) - NetBox plugin that keeps InvenTree inventory in sync via its REST API.
```

PUT the updated file to the new branch via `gh api repos/edurbs/awesome-netbox/contents/README.md`.

- [ ] **Step 3: Open the PR** — `gh pr create --repo netbox-community/awesome-netbox` from the fork branch, title `Add netbox-inventree-sync to Synchronization`, body linking the repo, PyPI page, and v0.1.0 release.

- [ ] **Step 4: Update issue #4's checklist** — fetch the body, tick `[x]` for 1.1–1.6, reword the Bonus bullet to note it was submitted under *Synchronization* (with the PR link), leave Phase 2/3 items unticked; `gh issue edit 4 --body-file`. Preserve all other content verbatim.
