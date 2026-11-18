# drive-artifacts

## Three steps

1. Install the CLI and log in once, at a machine with a browser:

   ```bash
   pipx install drive-artifacts          # or: uv tool install drive-artifacts
   drive-artifacts login --repo you/your-repo
   ```

   Your browser opens Google's consent page. The app asks for one permission,
   `drive.file`: it can see only the files it creates. `login` then stores the
   token as the repository secret `DRIVE_ARTIFACTS_TOKEN` using `gh`; without
   `gh`, run `drive-artifacts login --print` and add the secret by hand. No
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

Read the [privacy policy](privacy.md) for the Drive Artifacts app.
