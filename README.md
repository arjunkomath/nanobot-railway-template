# nanobot Railway Template

One-click deploy [nanobot](https://github.com/nano-bot/nanobot) (v0.4.1) on [Railway](https://railway.app) with a web-based config UI and status dashboard.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/template/nanobot)

## What you get

- **Web Config UI** — configure providers, channels, tools, and agent defaults from your browser
- **Status Dashboard** — monitor gateway state, uptime, provider/channel status, and live logs
- **Gateway Management** — start, stop, and restart the nanobot gateway from the UI
- **Basic Auth** — password-protected admin panel
- **Persistent Storage** — config and data survive container restarts via Railway volume

## Quick Start

### Deploy to Railway

1. Click the "Deploy on Railway" button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to logs)
3. Attach a volume mounted at `/data`
4. Open your app URL — you'll be prompted for credentials (default username: `admin`)
5. Configure at least one provider API key and hit Save
6. Once setup is complete, remove the public endpoint from your Railway service — the web UI is only needed for initial configuration and nanobot operates entirely through its configured channels (Telegram, WhatsApp, etc.)

### Run Locally with Docker

```bash
docker build -t nanobot .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v nanobot-data:/data nanobot
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(generated)* | Basic auth password. If unset, a random password is generated and printed to stdout |

Nanobot config can also be set via environment variables with the `NANOBOT_` prefix (e.g. `NANOBOT_PROVIDERS__ANTHROPIC__API_KEY`), but the web UI is the recommended way to manage configuration.

## Architecture

```
Railway Container
├── Python Web Server (Starlette + uvicorn)
│   ├── / — Config editor + status dashboard
│   ├── /health — Health check (no auth)
│   └── /api/* — Config, status, logs, gateway control
└── nanobot gateway — managed as async subprocess
```

The web server runs on `$PORT` and manages the nanobot gateway as a child process. Gateway stdout/stderr is captured into a ring buffer and viewable in the dashboard.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | Yes | Web UI |
| `GET` | `/health` | No | Health check |
| `GET` | `/api/config` | Yes | Get config (secrets masked) |
| `PUT` | `/api/config` | Yes | Save config |
| `GET` | `/api/status` | Yes | Gateway, provider, channel status |
| `GET` | `/api/logs` | Yes | Recent gateway log lines |
| `POST` | `/api/gateway/start` | Yes | Start gateway |
| `POST` | `/api/gateway/stop` | Yes | Stop gateway |
| `POST` | `/api/gateway/restart` | Yes | Restart gateway |

## Supported Providers

Configurable from the UI: Anthropic, OpenAI, OpenRouter, DeepSeek, Groq, Gemini, Zhipu, Mistral, vLLM, Ollama, and any OpenAI-compatible endpoint (Custom). nanobot supports many more; set them via `NANOBOT_PROVIDERS__<NAME>__API_KEY` env vars.

## Supported Channels

Configurable from the UI: Telegram, WhatsApp, Feishu/Lark. Their dependencies are baked into the image.

**WhatsApp pairing:** enable the channel and save, then open **Status → Recent Logs** and scan the QR code with WhatsApp → Linked Devices. The session is stored on the `/data` volume, so you only pair once.

## Upgrading from nanobot 0.1.x

Existing configs on the volume are migrated automatically on startup:

- `tools.exec.restrictToWorkspace` moves to `tools.restrictToWorkspace`, and `tools.exec.timeout` is read as `maxRuntime`.
- Web search now defaults to DuckDuckGo. If you had a Brave API key set, the template pins `tools.web.search.provider` to `brave` so it keeps working.
- WhatsApp no longer uses a separate bridge (`bridgeUrl` is ignored). Re-pair using the QR code in the logs.
