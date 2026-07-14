# NomWatch operator guide

## Service model

Run exactly one `nomwatch host`. On macOS, install the per-user LaunchAgent;
it starts at login and cannot operate while the Mac is asleep. On Raspberry
Pi OS, install the system units under `packaging/systemd`; the main unit runs
as the unprivileged `nomwatch` user and works after boot without a login.
Tailscale is optional and is never a child or required dependency of the
main host.

The separate Pi helper runs as root but accepts only versioned operation
names. It reconstructs a fixed Tailscale argv internally and never accepts
flags, URLs, ports, auth keys, shell text, or arbitrary commands.

## Routine checks

```text
nomwatch status
nomwatch diagnose --json-output
nomwatch backup
```

`diagnose` checks private modes, SQLite integrity/schema, free-disk floor,
migration snapshots, legacy PID/heartbeat artifacts, and whether a private
Serve mapping requires cleanup. It does not print secrets, tokens, command
output, camera frames, or raw request bodies.

## Recovery

1. Keep loopback/LAN available; do not alter unrelated OS or Tailscale state.
2. Stop the exact `com.nomwatch.host` LaunchAgent or `nomwatch.service` unit.
3. Run `nomwatch diagnose --json-output`.
4. If needed, restore a verified backup with `nomwatch restore-backup ... --yes`.
5. Start the service and confirm login, database integrity, MediaMTX health,
   monitoring ownership, and private access.

If `remote_access.cleanup_required` is reported, service uninstall is blocked.
Restore Tailscale connectivity and use the Access page to diagnose/disable the
exact owned mapping. NomWatch will never run `serve reset` or remove a mapping
that no longer exactly matches its ownership record.

## Upgrade and rollback

The first host cutover quiesces the exact legacy LaunchAgent, takes a private
checksummed migration snapshot, imports idempotently, and verifies SQLite. A
manual/unknown writer lock aborts cutover. If import fails after booting out
the exact legacy service, that exact service is bootstrapped again. Rollback
restores the migration snapshot; it does not merge data created afterward.
