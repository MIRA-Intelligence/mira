# GitHub and CNB Synchronization

GitHub is the canonical repository for MIRA. CNB is a domestic development
entry point and mirror. The sync rules are intentionally asymmetric so that
only GitHub can merge protected branches.

## Flow

1. GitHub mirrors protected refs to CNB.
   - Branches: `dev`, `main`, `release`, `release/**`
   - Tags: `v*`
2. CNB mirrors contributor branches back to GitHub only when the branch name
   matches `cnb/**`.
3. GitHub opens or reuses a PR from the mirrored `cnb/**` branch into `dev`.
4. CNB issue events create or update a GitHub issue with CNB metadata markers.
5. Review, CI, and merge happen on GitHub. CNB protected branches should not be
   merged directly.

## Required GitHub Secrets

Configure these in the GitHub repository secrets:

- `CNB_REPO_URL`: CNB HTTPS Git URL, for example `https://cnb.cool/<group>/mira.git`
- `CNB_GIT_USERNAME`: CNB Git username, usually `cnb`
- `CNB_GIT_TOKEN`: CNB token with write access to the mirrored repository
- `GH_SYNC_TOKEN`: optional but recommended. Use the same fine-grained GitHub
  token described below so the `cnb/**` inbound PR workflow can create pull
  requests even when the organization disables write access for `GITHUB_TOKEN`.
  GitHub repository secret names cannot start with `GITHUB_`, so this repo uses
  `GH_SYNC_TOKEN` for the GitHub-side secret.

If you do not configure `GH_SYNC_TOKEN` as a GitHub repository secret, also enable
GitHub Actions to create pull requests:

```text
Settings -> Actions -> General -> Workflow permissions -> Allow GitHub Actions to create and approve pull requests
```

## Required CNB Secrets

Configure these in the CNB key repository or repository variables:

- `GH_SYNC_TOKEN`: GitHub fine-grained token for `MIRA-Intelligence/mira`

The CNB-to-GitHub Git push uses `x-access-token` as the HTTPS username, so no
separate GitHub username or email variable is required. The same `GH_SYNC_TOKEN`
name is used in GitHub repository secrets and in CNB key-repository imports to
avoid name drift.

The GitHub token needs:

- Contents: read and write
- Issues: read and write

If the target GitHub repository changes, also set:

- `GITHUB_TARGET_REPOSITORY`: `owner/repo`

## Branch Rules

Protect these branches on both GitHub and CNB:

- `dev`
- `main`
- `release`
- `release/**`

On CNB, only the sync bot/token should be allowed to update the protected
branches. Domestic contributors should create branches under:

```text
cnb/<user>/<topic>
```

Do not merge CNB PRs into protected branches. The CNB branch will be mirrored
to GitHub, and GitHub will create the canonical PR into `dev`.

## Smoke Test

1. Run the GitHub workflow `Mirror GitHub to CNB` manually from `dev`.
2. Confirm CNB `dev` matches the GitHub `dev` commit.
3. In CNB, create a branch such as `cnb/smoke/sync-test` and push a small test
   commit.
4. Confirm the same branch appears on GitHub.
5. Confirm GitHub Actions opens a PR titled `[CNB] smoke: sync-test`.
6. Create a CNB issue and confirm a GitHub issue appears with the
   `CNB-Issue-ID` marker.

## Conflict Policy

The GitHub-to-CNB mirror uses non-force pushes. If a CNB protected branch has
local-only commits, the workflow fails instead of overwriting the branch.
Resolve by moving the CNB-only work to a `cnb/**` branch and opening a GitHub
PR.

CNB issue sync is inbound-only. GitHub remains the canonical issue tracker once
the issue has been created there. Full GitHub-to-CNB issue/comment mirroring
can be added later with CNB OpenAPI calls, but should use the same marker-based
deduplication approach.
