# docker-media-server

Single-host Docker media server. `docker-compose.yml` includes the stacks in
`stacks/`; see `README.md` for which service lives where. Secrets and
`DOMAIN` (maximedavin.com) live in `.env`, which is not committed. Per-service
config and data live outside the repo, in `~/.config/<service>`.

## Adding a service

A new service is not done until every step below is done (or skipped on
purpose). Steps 3 and 4 are outside the repo and are the ones that get
forgotten.

1. **Compose entry** in the stack that fits its concern.
   - `extends: { file: common/base.yml, service: app }` (or `svc` for
     services with their own identity/networking; see `common/base.yml`).
   - Explicit image tag, following the tag policy at the top of
     `docker-compose.yml`, plus a `wud.tag.include` label for any tag that is
     not `latest`/`release`/`nightly`.
   - Config volume under `${HOME}/.config/<service>`.
2. **Traefik labels**: `traefik.enable=true`,
   ``Host(`<name>.${DOMAIN}`)``, and the `loadbalancer.server.port` when the
   image exposes more than one port. Admin tools get
   `middlewares=oidc-admin@file`; the `@file` suffix is required.
3. **Public DNS**: Cloudflare CNAME `<name>` -> `maximedavin.com`, DNS only
   (not proxied), auto TTL, matching every other subdomain. cloudflare-ddns
   only updates the apex A record; subdomain records are not created for
   you. Without the record the host still resolves on the server itself (the
   host's own dnsmasq has `address=/maximedavin.com/192.168.0.42` in
   `/etc/dnsmasq.conf`), so it looks fine from here, but LAN devices get
   NXDOMAIN and the Let's Encrypt certificate cannot be issued or renewed.
   Check with `dig +short <name>.maximedavin.com @1.1.1.1`.
   LAN clients use the Pi-hole container (192.168.0.113, macvlan), which
   forwards these names upstream and caches the NXDOMAIN from any lookup made
   before the record existed. Flush it with `docker exec pihole pihole
   reloaddns`. The host cannot reach 192.168.0.113 directly (macvlan), so test
   it with `docker exec pihole dig <name>.maximedavin.com @127.0.0.1`.
4. **Rauthy redirect URIs**, for hosts behind `oidc-admin`/`oidc-user`: add
   `https://<name>.<domain>/oidc/callback` and `https://<name>.<domain>/*` to
   the `traefik` client. Procedure in the header of `traefik/dynamic.yml`.
   Symptom when missing: a bare 400 from Rauthy right after the login redirect.
5. **Optional**: a tile in `homepage/portal/services.yaml` (guest-facing) or
   `homepage/status/services.yaml` (admin), and a health check in
   `gatus/config.yaml`. Update the stack table in `README.md`.
