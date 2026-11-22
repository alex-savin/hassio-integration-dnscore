# Changelog

What changed in each release of the DNS Core integration, newest first.

Each release has a section headed `## <version> — <date>`, where the version
is the one in `custom_components/dnscore/manifest.json`. When a release
reaches `main`, the Release workflow publishes its section as the GitHub
release's notes, and the Marketplace shows them before an update. Changes
that are not released yet go under `## Unreleased`. CONTRIBUTING.md has the
steps.

## Unreleased

The first release.

- **Monitoring** of a DNS Core cluster over its HTTPS admin API, with each
  node read directly:
  - per node: queries, and how many were blocked, cached and forwarded;
    queries per second; the blocked ratio; the cache's entries and hit
    rate; average processing time; forwarded latency (p90); upstream
    timeouts; SERVFAIL answers; the settings' state; the version and when
    it started; memory on the boards;
  - per node, as problems: online, serving, leader, blocking in effect,
    upstreams, and health (DNS Core 0.95.0 or later);
  - for the cluster: members online, the leader and election term, blocked
    per second and the blocked ratio, the blocklist (size, when built and
    checked), the certificate (expiry and renewal), and shared settings.
- **Control**: a switch that turns blocking on and off, a `pause_blocking`
  action that turns it off for a while, a button that refreshes the
  blocklist, and a button per node that flushes its cache.
- **Setup**: sign in once with the cluster's OTA password. Home Assistant
  makes an API token with only the rights it needs and keeps no password.
  The DNS Core app for Home Assistant (DNS Core 0.95.0 or later) is
  discovered on its own. If the token stops working, Home Assistant asks
  you to sign in again.
- **Diagnostics** with tokens, names and addresses removed.
- DNS Core's icon and logo.
