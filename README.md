# docker-media-server

Self-hosted media server on a single Docker host, behind Traefik with Rauthy (OIDC) for auth.

`docker-compose.yml` is the entry point. It includes the stacks in `stacks/`, split by concern:

| Stack | Services |
|---|---|
| `infra` | traefik, rauthy, pihole, cloudflare-ddns, homepage portal |
| `media` | jellyfin, komga, komf, calibre |
| `arr` | sonarr, radarr, bazarr, prowlarr, recyclarr, cleanuparr, seerr, jfa-go |
| `download` | gluetun, qbittorrent |
| `invidious` | invidious, companion, postgres |
| `apps` | filebrowser, babybuddy |
| `monitoring` | beszel, gatus, dozzle, wud, ntfy, homepage status |

## Usage

Secrets and host-specific values (`DOMAIN`, `PUID`, API keys, tokens…) live in `.env`, which is not committed. Compose fails with a hint when a required variable is missing.

```sh
docker compose up -d
```
