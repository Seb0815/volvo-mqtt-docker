import argparse
import logging
import os
import signal
import sys
import threading

from .api import BridgeError, Http, Tokens, Volvo
from .auth import authorize
from .bridge import run
from .config import Config
from .dashboard import Dashboard, Status


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Volvo zu MQTT Bridge")
    parser.add_argument("command", choices=("auth", "run"), nargs="?", default="run")
    args = parser.parse_args()
    try:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("LOG_LEVEL muss DEBUG, INFO, WARNING oder ERROR sein")
        logging.basicConfig(
            level=level, format="%(asctime)s %(levelname)s %(message)s"
        )
        config = Config.from_env(args.command)
        http = Http()
        tokens = Tokens(config, http)
        if args.command == "auth":
            authorize(config, tokens)
        else:
            tokens.load()
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            status = Status(config)
            with Dashboard(config, status):
                run(config, Volvo(config, tokens, http), stop, status=status)
    except (BridgeError, ValueError, OSError) as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
