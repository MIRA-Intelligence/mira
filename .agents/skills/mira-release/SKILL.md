---
name: mira-release
description: Release coordinated Mira engine and mira-ui desktop versions from the dev branches through the long-lived release branches. Use when publishing a Mira patch, RC, or stable GitHub release; updating mira-ui compatibility.json; tagging engine and desktop versions; monitoring release workflows; validating release assets or PyPI; or running the paired release-train smoke test.
---

# Mira Release

Publish the engine before the desktop app. The desktop bundle downloads the latest published engine release, so reversing this order can silently ship an old engine.

## Inputs

Resolve or ask for:

- Engine version/tag, for example `0.3.4` / `v0.3.4`.
- UI version/tag, for example `0.4.4` / `v0.4.4`.
- Release type: RC or stable.

For a patch request, derive the next versions from the latest **published GitHub releases**, not merely the highest local tag.

## Safety rules

- Work from `/Users/cwang/Code/mira` and `/Users/cwang/Code/mira-ui` unless the user supplies other paths.
- Require clean worktrees before merging or tagging. Preserve unrelated user changes.
- Fetch `dev`, `release`, `main`, and tags before deciding versions.
- Never replace or force-push `release`. It contains release-only fixes and historical merge commits that may not exist on `dev`.
- Merge `origin/dev` into a branch based on `origin/release`, then push that merge to `release` normally.
- Use annotated tags and never move an existing tag.
- Do not tag mira-ui until the engine GitHub Release is published and its required assets exist.
- Stop on failing tests, merge conflicts, failed release jobs, missing assets, version mismatch, or compatibility mismatch. Diagnose and fix before continuing.
- Treat PyPI and GitHub Releases as immutable publication boundaries. Verify everything possible before pushing a tag.

## 1. Inspect and choose versions

1. Check both worktrees and current branches.
2. Query the latest published releases:

```bash
gh release view --repo MIRA-Intelligence/mira --json tagName,publishedAt,url
gh release view --repo MIRA-Intelligence/mira-ui --json tagName,publishedAt,url
```

3. Confirm the proposed tags do not exist locally or remotely.
4. Review commits since the previous tags and note user-visible changes.
5. Confirm `mira_engine/channels/ui.py::_API_CONTRACT_VERSION`. Only bump it for a backward-incompatible wire change.

## 2. Prepare dev and compatibility

Update `/Users/cwang/Code/mira-ui/compatibility.json`:

- `release_train`: current `YYYY.MM`.
- `ui`: target UI version without `v`.
- `agent`: target engine version without `v`.
- `api_contract`: current engine API contract.
- `min_agent_for_ui`: minimum engine needed by this UI. Use the new engine version when the UI depends on new endpoints or event types.

Validate locally:

```bash
node scripts/validate-compatibility.mjs --file compatibility.json --require-ui <ui-version>
```

Run the preflight script after committing preparation changes:

```bash
python .agents/skills/mira-release/scripts/preflight.py \
  --agent-version <engine-version> \
  --ui-version <ui-version>
```

Commit and push compatibility or release-document changes to `dev`, then wait for the `dev` CI checks to pass.

## 3. Verify source before publication

Run the project-native checks:

```bash
# /Users/cwang/Code/mira
python -m pytest tests -q

# /Users/cwang/Code/mira-ui
npm test -- --run
npm run build:web
npm run build:electron
```

Use the exact commands declared by current workflows if they differ. A previous PR run is useful evidence but does not replace release-source verification after preparation changes.

## 4. Publish the engine first

From the Mira repo:

1. Create a temporary release branch from `origin/release`.
2. Merge `origin/dev` with a merge commit. Resolve conflicts conservatively; retain release-only fixes.
3. Re-run relevant tests on the merged release tree.
4. Push the merge commit to `origin/release` without force.
5. Create and push annotated tag `v<engine-version>` at that exact commit.
6. Watch `.github/workflows/agent-release.yml` to completion.
7. Verify the published release contains:
   - wheel and sdist;
   - macOS, Linux, and Windows engine artifacts;
   - `SHA256SUMS.txt`;
   - a non-zero resolved package version.
8. Verify PyPI shows the version when the publish job is enabled.

Do not proceed to mira-ui while any engine job or asset check is incomplete.

## 5. Publish mira-ui second

From the mira-ui repo:

1. Re-run the compatibility validator against the target UI version.
2. Create a temporary release branch from `origin/release`.
3. Merge `origin/dev` with a merge commit.
4. Re-run tests plus `build:web` and `build:electron` on the merged release tree.
5. Push the merge commit to `origin/release` without force.
6. Create and push annotated tag `v<ui-version>` at that exact commit.
7. Watch `.github/workflows/desktop-release.yml` to completion.
8. Verify the published release contains both standalone and bundle artifacts for macOS and Windows, including update manifests.
9. Confirm bundle logs identify the intended engine tag/version rather than an older release.

The repository package version may intentionally differ before CI; the release workflow derives artifact versions from the tag.

## 6. Validate the paired release train

Trigger the Mira release-train workflow only after both GitHub Releases exist:

```bash
gh workflow run release-train.yml \
  --repo MIRA-Intelligence/mira \
  --ref release \
  -f agent_tag=v<engine-version> \
  -f ui_tag=v<ui-version>
```

Watch the resulting run. Require `verify-tags` and `smoke` to pass, and verify the smoke report and release summary artifacts were produced.

## 7. Report

Return:

- Engine and UI release URLs and tag commit SHAs.
- Workflow run URLs and conclusions.
- Compatibility mapping used.
- Key asset names verified.
- PyPI verification result.
- Release-train result.
- Any non-blocking warnings, such as deprecated GitHub Actions runtimes.

If a publication failed after a tag was pushed, do not delete or retarget the tag automatically. Fix the workflow or source with a new patch/RC unless the user explicitly authorizes destructive tag recovery.
