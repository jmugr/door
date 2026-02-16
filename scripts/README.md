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
