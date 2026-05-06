# Releasing

## TL;DR — every release

```bash
# 1. Bump the version in custom_components/reeftanktracker/manifest.json
#    e.g. "version": "0.1.4"

# 2. Commit + push
git add .
git commit -m "0.1.4 — what changed"
git push

# 3. Create a GitHub Release (REQUIRED for HACS to detect updates)
gh release create v0.1.4 \
  --title "0.1.4" \
  --notes "Short release notes here"
```

That's it. Within ~minutes HACS will surface the update on every install.

## Why a tag isn't enough — HACS uses GitHub Releases

When the repo has **at least one published GitHub Release**, HACS:

- Treats the latest release's **tag** as the available version
- Reads release notes for the "What's new" view
- Compares against the user's installed version (taken from `manifest.json`)

Without releases, HACS falls back to **commit-tracking mode**, which:

- Shows commit hashes (`1e8fbc8 → 5490910`) instead of versions
- Forces users to "redownload" instead of getting an update prompt
- Makes "what changed" invisible

If users see commit hashes in the update card, the answer is always: **publish a Release**.

## Release procedure (long form)

1. **Bump `manifest.json` version** to match the planned tag (no `v` prefix here — `"version": "0.1.4"`).
2. **Update `CHANGELOG.md`** with what changed.
3. **Commit + push to main:**
   ```bash
   git add custom_components/reeftanktracker/manifest.json CHANGELOG.md ...
   git commit -m "0.1.4 — short summary"
   git push
   ```
4. **Wait for CI** (`.github/workflows/tests.yml`) to go green. If it fails, fix and re-push before tagging — a release pinned to broken code is hard to undo cleanly.
5. **Create the release.** Two equivalent paths:

   **GitHub CLI (preferred):**
   ```bash
   gh release create v0.1.4 \
     --title "0.1.4" \
     --notes-from-tag           # uses the tag's annotated message; or use --notes
   ```

   **Web UI:** <https://github.com/jordanduffybd/reeftanktracker/releases/new>
   - Tag: `v0.1.4` (create new tag)
   - Title: `0.1.4`
   - Description: paste from `CHANGELOG.md`
   - Hit "Publish release"

6. **Verify in HACS** (HA → HACS → Reef Tank Tracker):
   - Should now read **0.1.4** as latest, not a commit hash.
   - Click the update prompt → restart HA when finished.

## Versioning policy

We follow semver-flavoured `MAJOR.MINOR.PATCH`:

- **PATCH** (`0.1.x`): bug fixes only, no behaviour changes
- **MINOR** (`0.x.0`): new features, additive entity/sensor changes
- **MAJOR** (`x.0.0`): breaking changes — entity ID changes, removed services, schema migrations

While we're pre-1.0, breaking changes can land in MINOR bumps. Anything user-visible (entity ID rename, service signature change) gets a clear note in `CHANGELOG.md` and the GitHub Release notes.

## Pre-release / beta releases

For wide changes you'd rather test on a single tank first:

```bash
gh release create v0.2.0-beta.1 --prerelease --title "0.2.0-beta.1" --notes "..."
```

HACS hides pre-releases from most users by default; it surfaces them only for repos the user has opted into "show beta". Useful when iterating with the bridge connected to live tank.

## Forgot to bump the manifest before tagging?

If `manifest.json` says 0.1.3 but you tagged 0.1.4, HA shows mismatched install/latest versions. Fix:

```bash
# Edit manifest.json → 0.1.4
git add custom_components/reeftanktracker/manifest.json
git commit -m "Bump manifest to 0.1.4"
git push

# Move the tag to the new commit
git tag -d v0.1.4
git push origin :refs/tags/v0.1.4
git tag -a v0.1.4 -m "0.1.4"
git push origin v0.1.4

# Republish the GitHub release
gh release delete v0.1.4 --yes
gh release create v0.1.4 --title "0.1.4" --notes "..."
```

(Don't be that person who does this on purpose. But it happens.)

## Known caveats

- HACS caches release info. Click **HACS → Settings → Reload data** to force a refresh; otherwise it polls every few hours.
- The integration is single-instance. Schema-breaking storage changes need a `STORAGE_VERSION` bump and a migration in `coordinator.py` — don't ship those silently.
