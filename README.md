# drive-artifacts

GitHub Actions artifacts, stored in your own Google Drive. Same inputs as
`actions/upload-artifact`, your storage quota instead of GitHub's.

## Three steps

1. Install the CLI and log in once, at a machine with a browser:

   ```bash
   pipx install drive-artifacts          # or: uv tool install drive-artifacts
   drive-artifacts login --repo you/your-repo
   ```

   `login` first checks that `gh` is installed and logged in (`gh auth status`),
   then your browser opens Google's consent page. The app asks for one
   permission, `drive.file`: it can see only the files it creates. `login` then
   stores the token as the repository secret `DRIVE_ARTIFACTS_TOKEN` using `gh`.
   Without a logged-in `gh`, `login` stops before the browser opens; run
   `drive-artifacts login --print` instead and add the secret by hand. No
   Google Cloud project, no API keys.

2. Upload in a workflow:

   ```yaml
   - uses: TheChaosBureau/drive-artifacts@v0
     with:
       name: wheel
       path: dist/
       token: ${{ secrets.DRIVE_ARTIFACTS_TOKEN }}
       retention-days: 30
   ```

3. Download in another job, run, or from your laptop:

   ```yaml
   - uses: TheChaosBureau/drive-artifacts/download@v0
     with:
       name: wheel
       path: dist/
       token: ${{ secrets.DRIVE_ARTIFACTS_TOKEN }}
       # run-id: 123          another run
       # ref: refs/heads/main the latest artifact for a ref
   ```

   ```bash
   export DRIVE_ARTIFACTS_TOKEN=...   # the value `drive-artifacts login --print` gave you
   drive-artifacts download --name wheel --ref refs/heads/main --repo-id 123456 --repo you/your-repo
   ```

## What lands in Drive

```
GitHub Artifacts/
  "you/your-repo"/      one folder: the slash is part of its name
    <run id>-<attempt>-<name>.zip
```

One zip per artifact, like GitHub's own, in one folder per repository (named
`owner/name`, slash included). The app owns the `GitHub Artifacts` folder
(configurable with `root`). It cannot see, list or touch anything else
in your Drive, and revoking the app at <https://myaccount.google.com/permissions>
ends its access instantly. Expired artifacts go to Drive's trash, where they stay
recoverable for 30 days.

## A latest copy at a link that never changes

For a file people open rather than download, a rendered paper or a report, the
upload action has a second mode. `latest: true` stores the one file `path`
matches as a plain file, not zipped and with no expiry, at a fixed path under
the root folder. The next upload to that path replaces the content of the same
Drive file, so the file keeps its id and its link.

```yaml
on:
  push:
    branches: [main]
jobs:
  paper:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - run: make paper                # writes paper/main.pdf
      - uses: TheChaosBureau/drive-artifacts@v0
        with:
          path: paper/main.pdf
          latest: true
          token: ${{ secrets.DRIVE_ARTIFACTS_TOKEN }}
```

```
GitHub Artifacts/
  Latest/
    you/
      your-repo/
        main.pdf        always the newest; one file, one link
```

- **Where.** `Latest/<owner>/<repo>/<file name>` under the root folder. Set
  `latest-path` for another place: `Papers/` keeps the file's own name in that
  folder, `Papers/index.html` also names the file. The path is the identity, so
  a different path, a renamed repository on the default path, or a file moved
  or renamed by hand in Drive, starts a new file with a new link.
- **One file.** `path` must match exactly one file; several files take several
  steps. `name`, `retention-days`, `compression-level`, `overwrite` and `prune`
  belong to the zip mode and are not used here.
- **Only the default branch.** The step fails on any other ref, a pull request
  included, so that work in progress cannot replace the canonical file.
  `latest-any-ref: true` lifts that. It is a guard against mistakes, not a
  security boundary: whoever can edit the workflow can set the input.
- **What the file says about itself.** Each upload records, as Drive app
  properties on the file, the repository (id and `owner/name`), commit, ref,
  run id and attempt, a sha256 of the content and the upload time in UTC.
- **Reading them back.** `drive-artifacts latest --json` lists every latest
  file the app can see, whichever repository uploaded it:

  ```json
  [
    {
      "path": "Latest/you/your-repo/main.pdf",
      "name": "main.pdf",
      "url": "https://drive.google.com/file/d/<id>/view?usp=drivesdk",
      "file_id": "<id>",
      "repo": "you/your-repo",
      "repo_id": "123456",
      "commit": "<sha>",
      "ref": "refs/heads/main",
      "run_id": "987654321",
      "run_attempt": "1",
      "uploaded": "2026-11-24T06:00:00Z",
      "sha256": "<digest>",
      "size": 482113,
      "created": "2026-11-20T10:00:00.000Z",
      "modified": "2026-11-24T06:00:01.000Z"
    }
  ]
  ```

  It needs only `DRIVE_ARTIFACTS_TOKEN`; `--repo-id` narrows it to one
  repository. From Python: `ArtifactStore.list_latest()`.
- **One listing for every repository.** What a token can see is decided by the
  app and the Google account, not by the token: a token made by `login` for one
  repository lists, and can replace, the latest files uploaded with a token
  made for another, as long as both logins used the same Google account and
  the same OAuth client. Tested against the real Drive with two tokens from two
  separate logins. So one index can read every repository's files, and one
  token can serve many repositories: `drive-artifacts login --org your-org`
  stores it as an organisation secret (for `gh`, visible to the organisation's
  private repositories by default; GitHub Free does not hand organisation
  secrets to private repositories).
- **Earlier versions.** Drive keeps the replaced content as an earlier revision
  of the same file (*File information → Manage versions*), and removes old
  revisions on its own schedule; the action does not pin them. The nightly
  contract test checks this against the real Drive.

## Inputs

### Upload (`TheChaosBureau/drive-artifacts@v0`)

| input | default | meaning |
|---|---|---|
| `name` | `artifact` | artifact name |
| `path` | required | files, directories or globs, one per line; `!` excludes |
| `token` | required | the `DRIVE_ARTIFACTS_TOKEN` secret |
| `retention-days` | `90` | 1..400 |
| `if-no-files-found` | `warn` | `warn`, `error` or `ignore` |
| `compression-level` | `6` | 0 (store) .. 9 |
| `overwrite` | `false` | replace an artifact of the same name in this run |
| `include-hidden-files` | `false` | include dot-files and dot-directories; a hidden directory that `path` names itself (`.cache/out/*`) is always searched |
| `root` | `GitHub Artifacts` | the folder the app owns |
| `prune` | `true` | trash this repository's expired artifacts after the upload; a failure here is a warning, the upload still succeeds |
| `latest` | `false` | store one plain file at a fixed path and replace it in place; see above |
| `latest-path` | `Latest/<owner>/<repo>/<file name>` | with `latest`: the path under the root folder; a trailing `/` keeps the file's own name |
| `latest-any-ref` | `false` | with `latest`: allow a ref that is not the default branch |
| `source` | `drive-artifacts==<this release>` | the requirement `uvx` installs; leave it alone |

Outputs: `artifact-id`, `artifact-url`, `artifact-digest` (`sha256:…`), `size`.
With `latest`, the id and the url are those of the one file and stay the same
from upload to upload.

### Download (`TheChaosBureau/drive-artifacts/download@v0`)

| input | default | meaning |
|---|---|---|
| `name` | all | omit to download every artifact of the run into subdirectories; a run that stored none is a warning, not a failure |
| `path` | `.` | directory to extract into; a leading `~` is the home directory |
| `token` | required | the `DRIVE_ARTIFACTS_TOKEN` secret |
| `run-id` | this run | another run of this repository |
| `ref` | | latest artifact of `name` for a ref (needs `name`) |
| `commit` | | latest artifact of `name` for a commit (needs `name`) |
| `root` | `GitHub Artifacts` | the folder the app owns |
| `source` | `drive-artifacts==<this release>` | the requirement `uvx` installs; leave it alone |

Give at most one of `run-id`, `ref` and `commit`.

Outputs: `download-path`, and `artifact-digest` (`sha256:…`), which is set only
when a single artifact was downloaded.

## Command line

```bash
drive-artifacts login --repo you/your-repo      # or --org your-org, or --print
drive-artifacts upload --name wheel --path dist/
drive-artifacts download --name wheel --run-id 123
drive-artifacts list                            # this run; or --run-id 123, --ref REF, --all
drive-artifacts upload --latest --path paper/main.pdf
drive-artifacts latest --json                   # every latest file, with link and source
drive-artifacts prune --dry-run
drive-artifacts whoami
drive-artifacts logout
```

- Inside Actions the run is read from the `GITHUB_*` variables. Elsewhere pass
  `--repo-id` (the numeric repository id) and `--repo`, and for a run-scoped
  command `--run-id`.
- `login --repo` and `login --org` need `gh` on `PATH`, logged in. Before the
  browser opens, `login` runs `gh auth status`; if `gh` is missing or not
  logged in, it exits 2 and asks you to re-run with `--print`, and no grant is
  made. If `gh secret set` then fails, `login` exits 2 the same way. The token
  from that attempt is stored nowhere and is left to lapse; `login` does not
  revoke it, because a revoke can end the access of tokens already in use.
- `logout` revokes the app's access at Google. Assume it ends every token minted
  from the same Google account, not only the one in `DRIVE_ARTIFACTS_TOKEN`:
  other repositories that share the account may have to `login` again.
- `upload --latest` takes `--latest-path` and `--any-ref`, and refuses the zip
  options. It reads the default branch from the Actions event; elsewhere pass
  `--default-branch` (with `--ref`), or `--any-ref`.
- `list` shows one run by default; `--run-id` picks another run, `--ref` every
  artifact stored for a ref and `--all` every artifact of the repository, newest
  first.
- `download` takes at most one of `--run-id`, `--ref` and `--commit`; the last
  two need `--name`.
- `--json` prints machine-readable results. `--verbose`, placed before the
  command (`drive-artifacts --verbose upload ...`), logs what the command does
  to stderr.

## Limits worth knowing

- Google allows 100 refresh tokens per account per app; the oldest is revoked
  silently when you log in a 101st time. One token per repository or
  organisation is plenty.
- A token unused for six months stops working. `drive-artifacts login` again.
- Drive's trash empties itself after 30 days. Pruning never deletes permanently.
- Your Drive quota is the limit. Personal accounts start at 15 GB.
- Links: a link to a file is stored as that file under the link's name. A link to a
  directory is followed only when `path` names it; wildcards and directory walks do not go
  through one. A broken link is left out.
- A file whose name holds a backslash or starts like a Windows drive (`C:`) cannot be
  uploaded, because extraction would refuse it; and every path must lie inside the
  directories the patterns search (no `a/*/../../b`).
- Two jobs that upload the same name in the same run with `overwrite: true` at the same
  moment both succeed, and the later upload is the one that stays. The earlier job's
  outputs then point at a file that is in the trash. Give parallel jobs different names.

## Bring your own OAuth client

Set `DRIVE_ARTIFACTS_CLIENT_ID` and `DRIVE_ARTIFACTS_CLIENT_SECRET` (a Desktop
app client with the `drive.file` scope, consent screen In production) before
`login` and in the workflow. Tokens remember the client that minted them.

## Exit codes

`0` ok · `1` filesystem error · `2` usage · `3` authentication · `4` not found ·
`5` Drive error after retries, or the artifact already exists and `overwrite` is off · `6` integrity (digest or unsafe zip) ·
`7` `if-no-files-found: error`.

## Development

```bash
uv sync --all-groups
uv run pytest            # unit + integration, against an in-memory fake Drive
uv run pytest -m contract   # the real API; needs DRIVE_ARTIFACTS_TOKEN
uv run ruff check . && uv run mypy && uv run pyright
```

Apache-2.0. See `CHANGELOG.md`, `SECURITY.md` and `CONTRIBUTING.md`.
