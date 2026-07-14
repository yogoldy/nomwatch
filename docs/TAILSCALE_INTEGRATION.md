# Private Tailscale integration

NomWatch supports Tailscale client versions `>=1.90.0,<1.99.0` for this
release. Unknown structured output or a version outside that fixture-tested
range disables mutation and leaves loopback/LAN service available.

The integration owns at most one node-hostname mapping: private HTTPS port
443, root path `/`, proxying to the dedicated authenticated gateway on
`http://127.0.0.1:5152`. It will not coexist with or overwrite any existing
Serve, Funnel, TCP, or Tailscale Service configuration. It never configures
tailnet policy, routes, tags, auth keys, the daemon, or a public endpoint.

The mutation grammar is deliberately fixed:

```text
tailscale login --timeout=120s
tailscale serve --bg --yes --https=443 --set-path=/ http://127.0.0.1:5152
tailscale serve --yes --https=443 --set-path=/ off
```

The adapter inventories `version --json`, `status --json`, node-level
`serve status --json`, read-only `funnel status --json`, and the distinct
Services inventory from `serve get-config --all` before it mutates. It never
uses `serve reset`, because that could erase user-owned configuration. It
never invokes a Funnel mutation. Disable is attempted only when current node,
tailnet, authority, path, proxy, exclusivity, and private/public status still
match the saved ownership record exactly. Drift closes the dedicated backend
and revokes Tailscale-origin NomWatch sessions.

Tailscale HTTPS keeps access inside the tailnet and tailnet access controls
still apply, but NomWatch login remains required. Enabling HTTPS publishes the
device's fully qualified `.ts.net` name in public Certificate Transparency
logs. The UI requires that disclosure to be acknowledged before enablement.

Behavior was verified on 2026-07-14 against the official Tailscale Serve,
Serve CLI, CLI, HTTPS certificate, macOS variants, Linux install, Linux
operator-permission, and access-control documentation linked from ADR 0001.
