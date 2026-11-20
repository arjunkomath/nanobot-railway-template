import asyncio
import base64
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from nanobot.config.loader import (
    get_config_path,
    load_config,
    save_config,
)
from nanobot.config.schema import Config

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
SECRET_FIELDS = {
    "api_key", "apiKey", "token", "password",
    "app_secret", "appSecret", "client_secret", "clientSecret",
    "encrypt_key", "encryptKey", "verification_token", "verificationToken",
    "webhook_secret_token", "webhookSecretToken", "token_issue_secret", "tokenIssueSecret",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")


class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None

        # Bad or malformed credentials are treated as anonymous (return None)
        # rather than raising, so protected routes answer 401 with a
        # WWW-Authenticate header and the browser re-prompts for a login.
        auth = conn.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return None
            decoded = base64.b64decode(credentials).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

        username, _, password = decoded.partition(":")
        if secrets.compare_digest(username.encode(), ADMIN_USERNAME.encode()) and secrets.compare_digest(
            password.encode(), ADMIN_PASSWORD.encode()
        ):
            return AuthCredentials(["authenticated"]), SimpleUser(username)

        return None


def require_auth(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="nanobot"'},
        )
    return None


class GatewayManager:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.start_time: float | None = None
        self.restart_count = 0
        self._read_tasks: list[asyncio.Task] = []

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self.state = "starting"
        try:
            self.process = await asyncio.create_subprocess_exec(
                "nanobot", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.state = "running"
            self.start_time = time.time()
            task = asyncio.create_task(self._read_output())
            self._read_tasks.append(task)
        except Exception as e:
            self.state = "error"
            self.logs.append(f"Failed to start gateway: {e}")

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.state = "stopped"
        self.start_time = None

    async def restart(self):
        await self.stop()
        self.restart_count += 1
        await self.start()

    async def _read_output(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                cleaned = ANSI_ESCAPE.sub("", decoded)
                self.logs.append(cleaned)
        except asyncio.CancelledError:
            return
        if self.process and self.process.returncode is not None and self.state == "running":
            self.state = "error"
            self.logs.append(f"Gateway exited with code {self.process.returncode}")

    def get_status(self) -> dict:
        pid = None
        if self.process and self.process.returncode is None:
            pid = self.process.pid
        uptime = None
        if self.start_time and self.state == "running":
            uptime = int(time.time() - self.start_time)
        return {
            "state": self.state,
            "pid": pid,
            "uptime": uptime,
            "restart_count": self.restart_count,
        }


gateway = GatewayManager()
config_lock = asyncio.Lock()


def mask_secrets(data, _path=""):
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and v:
                result[k] = v[:8] + "***" if len(v) > 8 else "***"
            else:
                result[k] = mask_secrets(v, f"{_path}.{k}")
        return result
    if isinstance(data, list):
        return [mask_secrets(item, _path) for item in data]
    return data


def _collect_secret_values(data, field_name):
    values = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == field_name and isinstance(v, str):
                values.append(v)
            else:
                values.extend(_collect_secret_values(v, field_name))
    elif isinstance(data, list):
        for item in data:
            values.extend(_collect_secret_values(item, field_name))
    return values


def merge_secrets(new_data, existing_data):
    if isinstance(new_data, dict) and isinstance(existing_data, dict):
        result = {}
        for k, v in new_data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and (v.endswith("***") or v == ""):
                result[k] = existing_data.get(k, "")
            else:
                result[k] = merge_secrets(v, existing_data.get(k, {}))
        return result
    return new_data


def load_cron_jobs(config: Config) -> list:
    # nanobot keeps every job in a single store file: {"version": ..., "jobs": [...]}
    store = config.workspace_path / "cron" / "jobs.json"
    try:
        jobs = json.loads(store.read_text()).get("jobs", [])
    except (OSError, ValueError, AttributeError):
        return []
    return jobs if isinstance(jobs, list) else []


def migrate_legacy_config():
    # nanobot <0.2 used Brave whenever tools.web.search.apiKey was set; newer
    # versions default to DuckDuckGo unless a provider is named. Pin existing
    # Brave users to "brave" so upgrading doesn't silently switch providers.
    path = get_config_path()
    try:
        raw = json.loads(path.read_text())
        search = raw["tools"]["web"]["search"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if isinstance(search, dict) and search.get("apiKey") and not search.get("provider"):
        search["provider"] = "brave"
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
        print("Migrated config: tools.web.search.provider set to 'brave'")


async def homepage(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return templates.TemplateResponse(request, "index.html")


async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.state})


async def api_config_get(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    config = load_config()
    data = config.model_dump(by_alias=True)
    return JSONResponse(mask_secrets(data))


async def api_config_put(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        restart = body.pop("_restartGateway", False)

        async with config_lock:
            existing_config = load_config()
            existing_data = existing_config.model_dump(by_alias=True)

            merged = merge_secrets(body, existing_data)

            try:
                new_config = Config.model_validate(merged)
            except Exception as e:
                err_msg = str(e)
                for field in SECRET_FIELDS:
                    for val in _collect_secret_values(merged, field):
                        if val and len(val) > 3:
                            err_msg = err_msg.replace(val, "***")
                return JSONResponse({"error": f"Validation error: {err_msg}"}, status_code=400)

            save_config(new_config)

        if restart:
            asyncio.create_task(gateway.restart())

        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        print(f"Config save error: {type(e).__name__}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    config = load_config()
    data = config.model_dump()

    providers = {}
    for name, prov in data["providers"].items():
        providers[name] = {"configured": bool(prov.get("api_key"))}

    channels = {}
    for name, chan in data["channels"].items():
        if isinstance(chan, dict):
            channels[name] = {"enabled": chan.get("enabled", False)}

    cron_jobs = load_cron_jobs(config)

    return JSONResponse({
        "gateway": gateway.get_status(),
        "providers": providers,
        "channels": channels,
        "cron": {"count": len(cron_jobs), "jobs": cron_jobs},
    })


async def api_logs(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return JSONResponse({"lines": list(gateway.logs)})


async def api_gateway_start(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.start())
    return JSONResponse({"ok": True})


async def api_gateway_stop(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.stop())
    return JSONResponse({"ok": True})


async def api_gateway_restart(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.restart())
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(app):
    migrate_legacy_config()
    config = load_config()
    if config.get_api_key():
        asyncio.create_task(gateway.start())
    try:
        yield
    finally:
        await gateway.stop()


routes = [
    Route("/", homepage),
    Route("/health", health),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/gateway/start", api_gateway_start, methods=["POST"]),
    Route("/api/gateway/stop", api_gateway_stop, methods=["POST"]),
    Route("/api/gateway/restart", api_gateway_restart, methods=["POST"]),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuthBackend())],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def handle_signal():
        loop.create_task(gateway.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve())
