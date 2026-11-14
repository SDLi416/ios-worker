import argparse
import json
import os
import sys
from pathlib import Path
import uvicorn
from api import create_app
from worker import acquire_service_lock, Problem


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Run the private HTTPS iOS Worker")
    parser.add_argument('--config', default=os.environ.get('IOS_WORKER_SERVICE_CONFIG'))
    args = parser.parse_args()
    config_path = Path(args.config) if args.config else Path(__file__).resolve().parent.parent / "config/config.json"
    config = json.loads(config_path.read_text())
    try:
        service_lock = acquire_service_lock(config)
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        return 0 if exc.status == 409 else 1
    try:
        app = create_app(config)
        uvicorn.run(app, host=config["host"], port=config["port"],
                    ssl_keyfile=config["tls_key"], ssl_certfile=config["tls_cert"],
                    proxy_headers=False, access_log=False, workers=1,
                    limit_concurrency=24, timeout_keep_alive=5,
                    limit_max_requests=20000, log_level="warning")
    finally:
        os.close(service_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
