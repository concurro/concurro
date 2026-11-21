"""Run the backoffice API: `python3 -m backoffice --settings PATH [--listen HOST:PORT]`."""

import argparse
import logging
import os
import threading

import uvicorn

from service.identity import Identity

from .app import create_app
from .auth import Sessions
from .internal import create_internal_app
from .settings import SettingsError, load

# How long a stop waits for requests in flight before it cancels them and cleans up.
SHUTDOWN_SECONDS = 5


def main() -> None:
    parser = argparse.ArgumentParser(prog="backoffice")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--listen", default="0.0.0.0:8788")
    parser.add_argument("--internal-listen")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = load(args.settings)
    except SettingsError as error:
        raise SystemExit(f"backoffice: {error}") from None
    host, _, port = args.listen.rpartition(":")
    sessions = Sessions()
    identity = Identity(settings.reader)
    app = create_app(settings, sessions=sessions, identity=identity)
    internal_server, internal_thread = None, None
    if args.internal_listen:
        if not settings.viewer_api_bearer:
            raise SystemExit("backoffice: viewer_api_bearer_path is required for --internal-listen")
        internal_host, _, internal_port = args.internal_listen.rpartition(":")
        internal_server = uvicorn.Server(
            uvicorn.Config(
                create_internal_app(settings, sessions, identity, settings.viewer_api_bearer),
                host=internal_host,
                port=int(internal_port),
                server_header=False,
                proxy_headers=False,
                timeout_graceful_shutdown=SHUTDOWN_SECONDS,
            )
        )
        internal_thread = threading.Thread(target=internal_server.run, name="viewer-authority", daemon=True)
        internal_thread.start()
    try:
        uvicorn.run(
            app,
            host=host,
            port=int(port),
            server_header=False,
            proxy_headers=False,
            # Open live-update streams never finish on their own; without a limit they would hold a stop until killed.
            timeout_graceful_shutdown=SHUTDOWN_SECONDS,
        )
    finally:
        if internal_server and internal_thread:
            internal_server.should_exit = True
            internal_thread.join(timeout=SHUTDOWN_SECONDS)


if __name__ == "__main__":
    main()
