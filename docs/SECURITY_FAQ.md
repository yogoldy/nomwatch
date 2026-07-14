# NomWatch security FAQ

## Is LAN access encrypted?

No. Explicit LAN mode is authenticated but uses trusted-network HTTP. A
device or attacker able to observe or modify that LAN can see credentials,
sessions, and video. Prefer the private Tailscale HTTPS URL, including at
home, when confidentiality matters.

## Does Tailscale replace NomWatch login?

No. Tailnet access controls are an additional network boundary. Every UI,
API, live manifest, segment, event, and clip still requires a NomWatch
account with the appropriate role.

## Can NomWatch make anything public?

No. NomWatch has no router, UPnP, port-forwarding, DDNS, public-listener, or
Tailscale Funnel mutation. Its Tailscale adapter owns only one tailnet-private
Serve mapping to an authenticated loopback gateway and refuses to overwrite
any pre-existing configuration.

## What leaves the host?

Camera RTSP, rolling segments, inference frames, and continuous recordings
remain local. An authenticated viewer deliberately receives an on-demand HLS
remux. Optional notification text or an explicitly enabled derived event-clip
export can leave the host. QR and HLS browser assets are local.

## What does the HTTPS disclosure mean?

Tailscale HTTPS certificate issuance publishes the full device `.ts.net`
name in public Certificate Transparency logs. It does not make the service
public. Rename a device whose name contains sensitive information before
acknowledging and enabling HTTPS.

## How are passwords and sessions stored?

Passwords use Argon2id. Session cookies are random 256-bit bearer values;
SQLite stores only keyed digests. Sessions have idle/absolute expiration,
can be revoked, are split by origin class, and remote sessions are revoked on
Tailscale ownership drift.

## How do backup and recovery work?

`nomwatch backup` creates a consistent local operational SQLite backup under
`NOMWATCH_HOME/backups`. It excludes media bytes and the separate unattended
secret store. It is not exposed through the web UI. Stop the host and use
`nomwatch restore-backup <directory> --yes`; the pre-restore database is
preserved as a rollback copy.
