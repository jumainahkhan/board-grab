# Board Grab

Paste a Pinterest board link and download every image as one ZIP. No extension, no sign-up.

- Accepts `pin.it/...` short links and `pinterest.com/user/board[/section]` links
- Shows a preview grid of everything it found, then streams a ZIP
- Nothing is stored on the server. Images are fetched from Pinterest's CDN and piped straight into the download.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Then open http://localhost:8000

## Deploy (Render, free)

1. Push this folder to a GitHub repo.
2. On render.com: **New → Blueprint**, pick the repo. It reads `render.yaml` and builds the Dockerfile.

It runs anywhere that runs Docker (Railway, Fly.io, a VPS).

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `MAX_PINS` | 1000 | Max pins per board |
| `SCANS_PER_WINDOW` | 10 | Board scans per IP per window |
| `ZIPS_PER_WINDOW` | 5 | ZIP downloads per IP per window |
| `RATE_WINDOW` | 600 | Rate-limit window in seconds |
| `MAX_CONCURRENT_SCANS` | 3 | Scans running at once, server-wide |
| `SCAN_TIMEOUT` | 120 | Seconds before a scan gives up |
| `TRUST_PROXY` | 1 | Use `X-Forwarded-For` for the client IP. Set to 0 if not behind a proxy. |

## Limits

- Public boards only.
- Video pins are skipped because Pinterest streams them, so there's no single file to save.
- Pinterest changes its site from time to time. If scans start failing, update gallery-dl:
  `pip install -U gallery-dl`, then bump the version in `requirements.txt`.
- If the server's IP gets blocked by Pinterest, redeploy on another host or region.
