#!/usr/bin/env python3
import argparse
import fcntl
import ipaddress
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

try:
    from . import generate as generator
except ImportError:
    import generate as generator


class EnrollmentError(Exception):
    pass


def _validate_input(host, port, device_id, label):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise EnrollmentError("host must be a valid IPv4 address.")
    if address.version != 4 or host != str(address):
        raise EnrollmentError("host must be a valid IPv4 address.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise EnrollmentError("port must be an integer from 1 to 65535.")
    if not isinstance(device_id, str) or not generator.ID_PATTERN.fullmatch(device_id):
        raise EnrollmentError("device-id must use lowercase ASCII letters, digits, or hyphens.")
    if (
        not isinstance(label, str)
        or not label
        or label != label.strip()
        or len(label) > 80
        or not label.isprintable()
    ):
        raise EnrollmentError("label must be a nonempty, short, printable label.")
    if label.upper() in generator.RESERVED_MIHOMO_LABELS:
        raise EnrollmentError("label must not use a reserved Mihomo name.")


def _open_private_parent(path):
    absolute = path if path.is_absolute() else Path.cwd() / path
    for ancestor in (absolute.parent,) + tuple(absolute.parent.parents):
        try:
            details = ancestor.lstat()
        except OSError:
            raise EnrollmentError("inventory parent must be an existing local directory.")
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise EnrollmentError("inventory parents must be directories without symbolic links.")
    try:
        descriptor = os.open(
            str(path.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise EnrollmentError("inventory parent must be a local directory without symbolic links.")
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
            raise EnrollmentError("inventory parent permissions must be owner-only, such as chmod 700.")
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise EnrollmentError("inventory parent must be owned by the current operator.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _check_existing_file(path):
    try:
        details = path.lstat()
    except OSError:
        raise EnrollmentError("unable to inspect the existing inventory.")
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise EnrollmentError("inventory must be a regular file without symbolic or hard links.")
    if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
        raise EnrollmentError("inventory must be owned by the current operator.")


def _stage_private(path, content):
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + path.name + ".", dir=str(path.parent)
        )
    except OSError:
        raise EnrollmentError("unable to stage a private inventory file.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        generator.load_inventory(temporary_path)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary_path


def _publish(staged, path, parent_descriptor):
    try:
        os.replace(str(staged), str(path))
        os.fsync(parent_descriptor)
    except OSError:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise EnrollmentError("unable to atomically replace the inventory; check the path and permissions.")


def enroll(inventory_path, host, port, device_id, label):
    _validate_input(host, port, device_id, label)
    path = Path(inventory_path)
    parent_descriptor = _open_private_parent(path)
    try:
        if path.exists() or path.is_symlink():
            _check_existing_file(path)
            try:
                inventory = generator.load_inventory(path)
            except generator.GenerationError as error:
                raise EnrollmentError(str(error))
            if inventory["host"] != host or inventory["port"] != port:
                raise EnrollmentError("host and port must match the existing inventory.")
            for device in inventory["devices"]:
                if device["id"] == device_id:
                    if device["label"] != label:
                        raise EnrollmentError("device-id already exists with a different label.")
                    return False
                if device["label"] == label:
                    raise EnrollmentError("label already belongs to a different device-id.")
            devices = inventory["devices"] + [
                {"id": device_id, "label": label, "secret": secrets.token_urlsafe(32)}
            ]
        else:
            devices = [{"id": device_id, "label": label, "secret": secrets.token_urlsafe(32)}]
        content = json.dumps(
            {"host": host, "port": port, "devices": devices},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        staged = _stage_private(path, content)
        _publish(staged, path, parent_descriptor)
        return True
    finally:
        fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Create or add one device to a private Outline inventory without printing secrets."
    )
    parser.add_argument("--inventory", required=True, help="local JSON inventory in an owner-only directory")
    parser.add_argument("--host", required=True, help="server IPv4 address")
    parser.add_argument("--port", required=True, type=int, help="server TCP/UDP port")
    parser.add_argument("--device-id", required=True, help="unique lowercase device identifier")
    parser.add_argument("--label", required=True, help="unique printable Mihomo proxy label")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        changed = enroll(args.inventory, args.host, args.port, args.device_id, args.label)
    except (EnrollmentError, generator.GenerationError, OSError) as error:
        print("Enrollment error: {}".format(error), file=sys.stderr)
        return 1
    print(
        "Enrolled device without printing secrets." if changed
        else "Device already enrolled; inventory unchanged and no secrets printed.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
