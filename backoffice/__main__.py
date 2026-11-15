"""Run the backoffice API: `python3 -m backoffice --settings PATH [--listen HOST:PORT]`."""

import argparse
import logging
import os

import uvicorn

from .app import create_app
from .settings import SettingsError, load

# How long a stop waits for requests in flight before it cancels them and cleans up.
SHUTDOWN_SECONDS = 5


def main() -> None:
    parser = argparse.ArgumentParser(prog="backoffice")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--listen", default="0.0.0.0:8788")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = load(args.settings)
    except SettingsError as error:
        raise SystemExit(f"backoffice: {error}") from None
    host, _, port = args.listen.rpartition(":")
    uvicorn.run(
        create_app(settings),
        host=host,
        port=int(port),
        server_header=False,
        proxy_headers=False,
        # Open live-update streams never finish on their own; without a limit they would hold a stop until killed.
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
