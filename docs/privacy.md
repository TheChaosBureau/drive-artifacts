---
permalink: /privacy/
---

# Privacy policy for the Drive Artifacts app

*Effective 2026-11-18.*

Drive Artifacts is an open-source command-line tool and GitHub Action published
by The Chaos Bureau. It stores build artifacts from your GitHub Actions
workflows in your own Google Drive.

**What the app accesses.** The app requests the Google Drive `drive.file`
permission only. With it the app can create files and folders in your Drive
and read, modify or trash the files it created. It cannot see any other file in
your Drive.

**Where your data goes.** Nothing goes anywhere but your Google Drive and the
GitHub repository you run the tool in. Signing in and renewing access go through
Google's own sign-in servers. The Chaos Bureau runs no server, receives no data,
and collects no telemetry. The OAuth token the app obtains is stored
where you put it: as a GitHub Actions secret you control, or printed to your
terminal on request.

**Revoking access.** Remove the app at
<https://myaccount.google.com/permissions>, or run `drive-artifacts logout`.
Either can end the app's access for every repository that uses the same Google
account.
Files the app created stay in your Drive, owned by you.

**No warranty, no support.** Drive Artifacts is provided as is, under the
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0), without
warranty of any kind. You use it at your own risk. The Chaos Bureau is not
responsible for lost, damaged or exposed files, for the Drive storage it uses,
or for any other result of using it. It is not a supported product: there is no
support channel, and no promise of fixes, updates or continued availability.

**Changes.** This policy can change at any time. The version on this page is
the one in force.
