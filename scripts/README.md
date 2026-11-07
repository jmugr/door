# Garage Scripts

## create-changelog.py

Generates a changelog from git history of published files in the vault.

### Usage

```bash
# Generate changelog for vault in current directory
python scripts/create-changelog.py --vault-path=vault

# Or from the ops/prod folder with your full version
python create-changelog_v2.py --local-only
```

### Requirements

```bash
pip install gitpython python-frontmatter tzdata
```

### Timezone

The changelog uses **America/Chicago (Central Time)** for all timestamps and date groupings.

### What it does

- Scans git history of the vault repository
- Identifies files with `publish: true` in frontmatter
- Tracks file additions, changes, and renames
- Generates `Garage changelog.md` with:
  - Changes grouped by date (newest first)
  - "Published" label for first appearance
  - "Changed" label for subsequent updates
  - Rename events with old and new names

### Automated Updates

The changelog is automatically regenerated on every push to the vault repository via GitHub Actions (see `.github/workflows/deploy.yml`).

## vault-stats.py

Generates the **Garage stats** page (`Garage stats.md`) and `public-vault-stats.json` from the vault's git history and current frontmatter. Both are written into the vault folder you pass, so only run it against a clone or the CI checkout. It refuses to run on a folder containing `.obsidian/workspace.json`, which only the live Obsidian vault has.

### Usage

```bash
# Local preview against a clone of the vault
python scripts/vault-stats.py --vault-path=C:/Projects/garage-preview/vault

# Tests
python -m unittest -v scripts/test_vault_stats.py
```

Needs `python-frontmatter` and `tzdata` (both in `requirements.txt`).

### What it shows

- Summary table: files, Markdown notes, syndicated notes (`publish: true`, `syndicate: ai`), files added per day, lines in notes, commits.
- Notes by first-level `type` and `topic` tag (`task` counts as `type/task`). Only tags that already appear on published notes are named; everything else is counted as "other", so private tags never reach the site.
- Daily new files, new lines and commits with 7-day and 30-day averages, files by month of last edit, and files and lines by the quarter they were added.

Hidden folders (`.obsidian`, `.github`, ...) are left out. Two self-checks replay the history and must match HEAD exactly, file for file and line for line; if either fails, the deploy step fails on its own and the site deploys without the page.

### Automated Updates

Runs in the deploy workflow after the changelog, on every vault push (see `.github/workflows/deploy.yml`).
