# Google Drive Setup

NomWatch uploads event clips to *your own* Google Drive, using an OAuth grant
you make directly to your own Google account. There's no NomWatch-operated
server or shared API client involved — Google requires each app to have its
own OAuth client, so this is a one-time, ~5 minute setup per user.

## Steps

1. Go to [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) (free Google account, no billing required for this).
2. Create a new project (or reuse any existing one) — the name doesn't matter, e.g. "NomWatch".
3. Go to **APIs & Services → Library**, search for **Google Drive API**, click **Enable**.
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - If prompted to configure a consent screen first, choose **External**, fill in an app name (e.g. "NomWatch") and your own email for the required fields — you can leave it in "Testing" mode, you don't need to publish it.
   - For the OAuth client type, choose **Desktop app**.
5. Download the resulting JSON file.
6. Save it to: `~/.config/nomwatch/drive_credentials.json`
   (create the `~/.config/nomwatch/` folder if it doesn't exist yet)

## First upload

The first time NomWatch tries to upload a clip, it will open a browser window
asking you to sign in and approve access. After that, a token is cached at
`~/.config/nomwatch/drive_token.json` (permissioned 600, owner-only) and
future uploads happen silently — no repeated prompts.

## Scope / privacy note

NomWatch requests the `drive.file` scope only — the most restrictive Drive
scope available. This means NomWatch can only see and manage files *it
creates itself*; it cannot browse, read, or touch any other file already in
your Drive.

## Optional: upload to a specific folder

By default, clips upload to your Drive root. To target a specific folder,
open that folder in Drive, copy the folder ID from the URL
(`https://drive.google.com/drive/folders/<FOLDER_ID>`), and set it during
`nomwatch setup`, or by editing `storage.drive_folder_id` in
`~/.config/nomwatch/config.yml`.
