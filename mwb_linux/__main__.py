"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .config import Config, default_config_path
from .service import MouseWithoutBordersService, control_request


def _configure_logging(verbose: bool = False) -> None:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    log_dir = state_home / "powertoys-mwb-linux"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "service.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def _daemon(args: argparse.Namespace) -> int:
    _configure_logging(args.verbose)
    service = MouseWithoutBordersService(Path(args.config) if args.config else None)
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, lambda *_: service.stop())
    try:
        service.start()
        service.wait()
    except OSError as exc:
        logging.error("service failed to start: %s", exc)
        return 1
    finally:
        service.stop()
    return 0


def _configure(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else default_config_path()
    config = Config.load(path)
    previous_machine_name = config.machine_name
    previous_name = (
        config.remote_machines[0]["name"]
        if config.remote_machines
        else config.host_name or config.host
    )
    if args.host is not None:
        config.host = args.host
    if args.host_name is not None:
        config.host_name = args.host_name
    if args.machine_name is not None:
        config.machine_name = args.machine_name
        for index, matrix_name in enumerate(config.machine_matrix):
            if matrix_name.casefold() == previous_machine_name.casefold():
                config.machine_matrix[index] = args.machine_name
                break
    if args.port is not None:
        config.port = args.port
    if args.position is not None:
        config.host_position = args.position
        # The saved monitor belongs to the previous edge; let the settings
        # form and the portal pick the outermost screen again.
        config.host_zone = []
    if args.profile is not None:
        config.crypto_profile = args.profile
    if args.secret_stdin:
        config.secret = sys.stdin.readline().rstrip("\r\n")
    if args.host is not None or args.host_name is not None:
        name = config.host_name.strip() or config.host.strip()
        address = config.host.strip() or name
        primary = {"name": name, "address": address}
        config.remote_machines = [
            primary,
            *[
                remote
                for remote in config.remote_machines[1:]
                if remote["name"].casefold() != name.casefold()
            ],
        ]
        replaced = False
        for index, matrix_name in enumerate(config.machine_matrix):
            if matrix_name.casefold() == previous_name.casefold():
                config.machine_matrix[index] = name
                replaced = True
                break
        if not replaced and name:
            try:
                config.machine_matrix[config.machine_matrix.index("")] = name
            except ValueError:
                pass
    config.validate(require_connection=True)
    config.save(path)
    print(f"Saved configuration to {path} with mode 0600 (key not displayed).")
    return 0


def _control(command: str, **arguments: object) -> int:
    try:
        response = control_request(command, **arguments)
    except OSError as exc:
        print(f"Service is not running: {exc}", file=sys.stderr)
        return 1
    if command == "status":
        print(json.dumps(response.get("status", response), indent=2))
    elif not response.get("ok"):
        print(response.get("error", "command failed"), file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powertoys-mouse-without-borders")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("ui", help="open the settings window")
    daemon = subparsers.add_parser("daemon", help="run the background service")
    daemon.add_argument("--config")
    daemon.add_argument("--verbose", action="store_true")
    configure = subparsers.add_parser("configure", help="write a secure configuration")
    configure.add_argument("--config")
    configure.add_argument("--host")
    configure.add_argument("--host-name")
    configure.add_argument("--machine-name")
    configure.add_argument("--port", type=int)
    configure.add_argument("--position", choices=("left", "right", "top", "bottom"))
    configure.add_argument(
        "--profile",
        choices=(
            "auto",
            "standalone-50k",
            "legacy-50k",
            "random-50k",
            "random-100k",
        ),
    )
    configure.add_argument(
        "--secret-stdin",
        action="store_true",
        help="read the key from stdin so it is not exposed in process arguments",
    )
    switch_machine = subparsers.add_parser(
        "switch-machine", help="switch control to a one-based machine matrix slot"
    )
    switch_machine.add_argument("slot", type=int, choices=range(1, 5))
    for name in ("status", "connect", "disconnect", "reconnect", "switch-host", "local", "quit"):
        subparsers.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["_drag-capture"]:
        from .drag_capture import main as drag_capture_main

        return drag_capture_main()
    if arguments == ["_drag-monitor"]:
        from .drag_capture import monitor_main

        return monitor_main()
    if arguments == ["_drop-indicator"]:
        from .drag_capture import indicator_main

        return indicator_main()
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command in (None, "ui"):
        from .ui import run_ui

        return run_ui([sys.argv[0]])
    if args.command == "daemon":
        return _daemon(args)
    if args.command == "configure":
        return _configure(args)
    if args.command == "switch-machine":
        return _control("switch_machine", slot=args.slot)
    command_map = {"switch-host": "switch_remote", "local": "release_local"}
    return _control(command_map.get(args.command, args.command))


if __name__ == "__main__":
    raise SystemExit(main())
