#!/usr/bin/env python3
import argparse
import base64
import grp
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ETC_DIR = Path("/etc/keel-outline")
ACCESS_PATH = ETC_DIR / "access.json"
STATE_PATH = ETC_DIR / "state.json"
INVENTORY_PATH = ETC_DIR / "inventory.json"
SERVER_PATH = ETC_DIR / "server.yaml"
PROFILE_PATH = Path("/var/lib/keel-sub/profile.yaml")
DELIVERY_PATH = ETC_DIR / "delivery.json"
GRAFANA_ENV_PATH = ETC_DIR / "grafana.env"
GRAFANA_SECRET_PATH = ETC_DIR / "grafana-secret.key"
NGINX_PATH = Path("/etc/nginx/sites-available/keel-outline")
ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")
RESERVED_LABELS = {
    "KEEL OUTLINE", "DIRECT", "REJECT", "REJECT-DROP", "PASS",
    "PASS-RULE", "GLOBAL", "COMPATIBLE",
}
EXCLUDED_SS_PORTS = {22, 80, 443, 3000, 9090, 9091, 9100}
MAX_FILE_BYTES = 2 * 1024 * 1024


class HostError(Exception):
    pass


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HostError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def _regular_file(path, private=False):
    path = Path(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise HostError("A required managed file is missing.")
    if not stat.S_ISREG(details.st_mode):
        raise HostError("Managed paths must be regular files without symlinks.")
    if private and (
        stat.S_IMODE(details.st_mode) & 0o077
        or details.st_uid != os.geteuid()
    ):
        raise HostError("Private managed files must be owner-only.")
    return details


def _read_json(path, private=False):
    path = Path(path)
    _regular_file(path, private=private)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HostError("A managed JSON file is too large.")
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except HostError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise HostError("A managed JSON file is invalid or unreadable.")


def atomic_write(path, content, mode=0o600, uid=None, gid=None):
    path = Path(path)
    parent = path.parent
    try:
        details = parent.lstat()
    except FileNotFoundError:
        raise HostError("The destination directory is missing.")
    if not stat.S_ISDIR(details.st_mode):
        raise HostError("The destination parent must be a real directory.")
    try:
        destination = path.lstat()
        if not stat.S_ISREG(destination.st_mode):
            raise HostError("Refusing to replace a non-regular managed path.")
    except FileNotFoundError:
        pass
    if isinstance(content, str):
        content = content.encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".keel-new-", dir=str(parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), mode)
            if uid is not None or gid is not None:
                os.fchown(
                    target.fileno(),
                    -1 if uid is None else uid,
                    -1 if gid is None else gid,
                )
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(str(temporary_path), str(path))
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def validate_device(device_id, label):
    if not isinstance(device_id, str) or not ID_PATTERN.fullmatch(device_id):
        raise HostError("DEVICE_ID must be 1-64 lowercase letters, digits, or interior hyphens.")
    if (
        not isinstance(label, str)
        or not label
        or label != label.strip()
        or len(label) > 80
        or not label.isprintable()
        or label.upper() in RESERVED_LABELS
    ):
        raise HostError("LABEL is empty, unsafe, too long, or reserved.")


def validate_port(port):
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        raise HostError("SS_PORT must be an integer from 1024 to 65535.")
    if isinstance(port, bool) or str(port_number) != str(port):
        raise HostError("SS_PORT must be an integer from 1024 to 65535.")
    if not 1024 <= port_number <= 65535 or port_number in EXCLUDED_SS_PORTS:
        raise HostError("SS_PORT is reserved for a web or local monitoring service.")
    return port_number


def validate_install(public_ip, port, device_id, label):
    try:
        address = ipaddress.ip_address(public_ip)
    except ValueError:
        raise HostError("PUBLIC_IPV4 must be a public IPv4 address.")
    if address.version != 4 or not address.is_global:
        raise HostError("PUBLIC_IPV4 must be a globally routable IPv4 address.")
    port_number = validate_port(port)
    validate_device(device_id, label)
    return public_ip, port_number


def _valid_access(value):
    if not isinstance(value, dict) or set(value) != {"token", "admin", "viewer"}:
        raise HostError("access.json must contain exactly token, admin, and viewer.")
    if any(
        not isinstance(value[key], str) or not TOKEN_PATTERN.fullmatch(value[key])
        for key in value
    ):
        raise HostError("access.json contains an invalid credential.")
    if len(set(value.values())) != 3:
        raise HostError("access.json credentials must be distinct.")
    return value


def ensure_access(path=ACCESS_PATH):
    path = Path(path)
    if path.exists() or path.is_symlink():
        return _valid_access(_read_json(path, private=True))
    value = {key: secrets.token_urlsafe(32) for key in ("token", "admin", "viewer")}
    atomic_write(path, json.dumps(value, indent=2) + "\n")
    return value


def ensure_state(path, public_ip, port, device_id, label):
    path = Path(path)
    expected = {
        "version": 1,
        "host": public_ip,
        "port": port,
        "deviceId": device_id,
        "label": label,
    }
    if path.exists() or path.is_symlink():
        current = _read_json(path, private=True)
        if current != expected:
            raise HostError("Managed state does not match this install invocation.")
        return current
    atomic_write(path, json.dumps(expected, indent=2) + "\n")
    return expected


def nginx_config(public_ip, token, https):
    if not TOKEN_PATTERN.fullmatch(token):
        raise HostError("The subscription token is invalid.")
    config = """server {
    listen 80 default_server;
    server_name _;
    server_tokens off;
    access_log off;
    error_log /dev/null;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/keel-acme;
    }
    location / {
        return 404;
    }
}
"""
    if not https:
        return config
    return config + """server {
    listen 443 ssl default_server;
    server_name PUBLIC_IP;
    server_tokens off;
    ssl_certificate /etc/letsencrypt/live/keel-outline/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/keel-outline/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null;
    location = /sub/TOKEN.yaml {
        alias /var/lib/keel-sub/profile.yaml;
        default_type application/yaml;
        add_header Cache-Control "private, no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Robots-Tag "noindex" always;
    }
    location /sub/ {
        return 404;
    }
    location / {
        proxy_http_version 1.1;
        proxy_set_header Host PUBLIC_IP;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host PUBLIC_IP;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_pass http://127.0.0.1:3000;
    }
}
""".replace("PUBLIC_IP", public_ip).replace("TOKEN", token)


def build_delivery(public_ip, access):
    access = _valid_access(access)
    return {
        "subscriptionUrl": "https://{}/sub/{}.yaml".format(public_ip, access["token"]),
        "dashboardUrl": "https://{}/d/keel-outline/keel-outline".format(public_ip),
        "username": "keel-viewer",
        "password": access["viewer"],
    }


def ensure_secret(path=GRAFANA_SECRET_PATH):
    path = Path(path)
    if path.exists() or path.is_symlink():
        _regular_file(path, private=True)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise HostError("A managed secret is unreadable.")
        if not TOKEN_PATTERN.fullmatch(value):
            raise HostError("A managed secret is invalid.")
        return value
    value = secrets.token_urlsafe(48)
    atomic_write(path, value + "\n")
    return value


def write_grafana_environment(public_ip):
    access = _valid_access(_read_json(ACCESS_PATH, private=True))
    secret = ensure_secret()
    values = {
        "GF_SECURITY_ADMIN_USER": "keel-admin",
        "GF_SECURITY_ADMIN_PASSWORD": access["admin"],
        "GF_SECURITY_SECRET_KEY": secret,
        "GF_SERVER_HTTP_ADDR": "127.0.0.1",
        "GF_SERVER_HTTP_PORT": "3000",
        "GF_SERVER_ROOT_URL": "https://{}/".format(public_ip),
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_USERS_AUTO_ASSIGN_ORG_ROLE": "Viewer",
        "GF_AUTH_ANONYMOUS_ENABLED": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_SECURITY_COOKIE_SECURE": "true",
        "GF_SECURITY_DISABLE_GRAVATAR": "true",
        "GF_SNAPSHOTS_EXTERNAL_ENABLED": "false",
        "GF_SNAPSHOTS_PUBLIC_MODE": "false",
    }
    content = "".join("{}={}\n".format(key, values[key]) for key in sorted(values))
    atomic_write(GRAFANA_ENV_PATH, content)


def write_nginx(public_ip, https):
    access = _valid_access(_read_json(ACCESS_PATH, private=True))
    atomic_write(NGINX_PATH, nginx_config(public_ip, access["token"], https))


def write_delivery(public_ip):
    access = _valid_access(_read_json(ACCESS_PATH, private=True))
    atomic_write(DELIVERY_PATH, json.dumps(build_delivery(public_ip, access), indent=2) + "\n")


def _profile_gid():
    try:
        return grp.getgrnam("www-data").gr_gid
    except KeyError:
        raise HostError("The www-data group is required.")


def _file_content(path, private=True):
    _regular_file(path, private=private)
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise HostError("A managed configuration file is unreadable.")
    if len(content) > MAX_FILE_BYTES:
        raise HostError("A managed configuration file is too large.")
    return content


def _destination(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    details = _regular_file(path)
    return {
        "content": _file_content(path, private=False),
        "mode": stat.S_IMODE(details.st_mode),
        "uid": details.st_uid,
        "gid": details.st_gid,
    }


def publish_many(items):
    backups = {}
    published = []
    try:
        for source, destination, mode, uid, gid in items:
            backups[str(destination)] = _destination(destination)
            content = _file_content(source)
            atomic_write(destination, content, mode=mode, uid=uid, gid=gid)
            published.append(destination)
    except Exception as error:
        rollback_errors = []
        for destination in reversed(published):
            backup = backups[str(destination)]
            try:
                if backup is None:
                    Path(destination).unlink()
                else:
                    atomic_write(destination, **backup)
            except Exception:
                rollback_errors.append(destination)
        if rollback_errors:
            raise HostError("Configuration publication and rollback both failed.")
        raise HostError("Configuration publication failed; prior files were restored.") from error


def publish_install(outline_source, clash_source):
    publish_many([
        (outline_source, SERVER_PATH, 0o600, 0, 0),
        (clash_source, PROFILE_PATH, 0o640, 0, _profile_gid()),
    ])


def publish_server(outline_source):
    publish_many([(outline_source, SERVER_PATH, 0o600, 0, 0)])


def publish_profile(clash_source):
    publish_many([(clash_source, PROFILE_PATH, 0o640, 0, _profile_gid())])


def snapshot_configs(directory):
    directory = Path(directory)
    for source, name in ((SERVER_PATH, "server.yaml"), (PROFILE_PATH, "profile.yaml")):
        _regular_file(source)
        atomic_write(directory / name, _file_content(source, private=False))


def rollback_configs(directory):
    directory = Path(directory)
    publish_many([
        (directory / "server.yaml", SERVER_PATH, 0o600, 0, 0),
        (directory / "profile.yaml", PROFILE_PATH, 0o640, 0, _profile_gid()),
    ])


def verify_managed():
    state = _read_json(STATE_PATH, private=True)
    access = _valid_access(_read_json(ACCESS_PATH, private=True))
    _read_json(INVENTORY_PATH, private=True)
    _regular_file(SERVER_PATH, private=True)
    profile = _regular_file(PROFILE_PATH)
    if (
        stat.S_IMODE(profile.st_mode) != 0o640
        or profile.st_uid != 0
        or profile.st_gid != _profile_gid()
    ):
        raise HostError("profile.yaml must be root:www-data mode 0640.")
    delivery = _read_json(DELIVERY_PATH, private=True)
    if delivery != build_delivery(state["host"], access):
        raise HostError("delivery.json does not match the managed credentials.")
    return state


def first_configured_key():
    value = _read_json(SERVER_PATH, private=True)
    try:
        device_id = value["services"][0]["keys"][0]["id"]
    except (KeyError, IndexError, TypeError):
        raise HostError("server.yaml has no initial key.")
    if not isinstance(device_id, str) or not ID_PATTERN.fullmatch(device_id):
        raise HostError("server.yaml contains an invalid initial key.")
    return device_id


def metrics_key_count(content):
    for line in content.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "shadowsocks_keys":
            return float(fields[1])
    return None


def wait_for_key(device_id, attempts=40):
    config = _read_json(SERVER_PATH, private=True)
    keys = [key["id"] for service in config["services"] for key in service["keys"]]
    if device_id not in keys:
        raise HostError("Requested device is missing from the installed server configuration.")
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:9091/metrics", headers={"Connection": "close"}
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                content = response.read(MAX_FILE_BYTES + 1).decode("utf-8")
            if len(content) <= MAX_FILE_BYTES and metrics_key_count(content) == len(keys):
                return
        except (OSError, UnicodeError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.5)
    raise HostError("Outline did not report the expected number of configured keys.")


class ResponseStatus(Exception):
    def __init__(self, status):
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _grafana_request(path, access, viewer=False, body=None, method=None):
    username = "keel-viewer" if viewer else "keel-admin"
    password = access["viewer"] if viewer else access["admin"]
    credentials = base64.b64encode("{}:{}".format(username, password).encode()).decode()
    headers = {
        "Authorization": "Basic " + credentials,
        "Accept": "application/json",
        "Connection": "close",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "http://127.0.0.1:3000" + path,
        data=data,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=8) as response:
            raw = response.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HostError("Grafana returned an oversized response.")
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise ResponseStatus(error.code)
    except (urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
        raise HostError("Grafana authentication or API validation failed.")


def ensure_viewer():
    access = _valid_access(_read_json(ACCESS_PATH, private=True))
    try:
        _grafana_request("/api/users/lookup?loginOrEmail=keel-viewer", access)
    except ResponseStatus as error:
        if error.status != 404:
            raise HostError("Grafana rejected the managed administrator.")
        _grafana_request("/api/admin/users", access, body={
            "name": "Keel Viewer",
            "login": "keel-viewer",
            "email": "viewer@keel.invalid",
            "password": access["viewer"],
        })
    organization = _grafana_request("/api/org", access)
    organization_id = organization.get("id")
    if type(organization_id) is not int:
        raise HostError("Grafana returned an invalid organization.")
    users = _grafana_request("/api/org/users", access)
    if not isinstance(users, list):
        raise HostError("Grafana returned an invalid organization membership.")
    membership = next(
        (user for user in users if isinstance(user, dict) and user.get("login") == "keel-viewer"),
        None,
    )
    if membership is None:
        _grafana_request("/api/org/users", access, body={
            "loginOrEmail": "keel-viewer", "role": "Viewer",
        })
    else:
        user_id = membership.get("userId", membership.get("id"))
        if type(user_id) is not int:
            raise HostError("Grafana returned an invalid Viewer membership.")
        if membership.get("role") != "Viewer":
            _grafana_request(
                "/api/org/users/{}".format(user_id), access,
                body={"role": "Viewer"}, method="PATCH",
            )
    identity = _grafana_request("/api/user", access, viewer=True)
    if identity.get("login") != "keel-viewer" or identity.get("isGrafanaAdmin") is not False:
        raise HostError("The delivered Grafana identity is not a non-admin Viewer.")
    organizations = _grafana_request("/api/user/orgs", access, viewer=True)
    if not isinstance(organizations, list) or not any(
        isinstance(org, dict)
        and org.get("orgId") == organization_id
        and org.get("role") == "Viewer"
        for org in organizations
    ):
        raise HostError("The delivered Grafana identity lacks the actual Viewer role.")


def check_ports(ss_port):
    sockets = []
    try:
        for port in (80, 443, ss_port):
            for kind in ((socket.SOCK_STREAM, socket.SOCK_DGRAM) if port == ss_port else (socket.SOCK_STREAM,)):
                owned = socket.socket(socket.AF_INET, kind)
                sockets.append(owned)
                owned.bind(("0.0.0.0", port))
        for port in (3000, 9090, 9091, 9100):
            owned = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(owned)
            owned.bind(("127.0.0.1", port))
    except OSError:
        raise HostError("A required public or loopback port is already in use.")
    finally:
        for owned in sockets:
            owned.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Internal Keel host bootstrap operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("validate-install")
    install.add_argument("public_ip")
    install.add_argument("port")
    install.add_argument("device_id")
    install.add_argument("label")
    begin = commands.add_parser("begin")
    begin.add_argument("public_ip")
    begin.add_argument("port")
    begin.add_argument("device_id")
    begin.add_argument("label")
    client = commands.add_parser("validate-client")
    client.add_argument("device_id")
    client.add_argument("label")
    commands.add_parser("access")
    commands.add_parser("endpoint")
    for name in ("grafana-env", "nginx", "delivery"):
        command = commands.add_parser(name)
        command.add_argument("public_ip")
        if name == "nginx":
            command.add_argument("mode", choices=("http", "https"))
    ports = commands.add_parser("check-ports")
    ports.add_argument("port")
    publish = commands.add_parser("publish-install")
    publish.add_argument("directory")
    for name in ("publish-server", "publish-profile"):
        command = commands.add_parser(name)
        command.add_argument("source")
    for name in ("snapshot", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("directory")
    commands.add_parser("verify-managed")
    commands.add_parser("first-key")
    wait = commands.add_parser("wait-key")
    wait.add_argument("device_id")
    commands.add_parser("ensure-viewer")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-install":
            validate_install(args.public_ip, args.port, args.device_id, args.label)
        elif args.command == "begin":
            public_ip, port = validate_install(
                args.public_ip, args.port, args.device_id, args.label
            )
            ensure_state(STATE_PATH, public_ip, port, args.device_id, args.label)
        elif args.command == "validate-client":
            validate_device(args.device_id, args.label)
        elif args.command == "access":
            ensure_access()
        elif args.command == "endpoint":
            _read_json(INVENTORY_PATH, private=True)
            state = _read_json(STATE_PATH, private=True)
            print("{}\t{}".format(state["host"], state["port"]))
        elif args.command == "grafana-env":
            write_grafana_environment(args.public_ip)
        elif args.command == "nginx":
            write_nginx(args.public_ip, args.mode == "https")
        elif args.command == "delivery":
            write_delivery(args.public_ip)
        elif args.command == "check-ports":
            check_ports(validate_port(args.port))
        elif args.command == "publish-install":
            directory = Path(args.directory)
            publish_install(directory / "outline.yml", directory / "clash.yaml")
        elif args.command == "publish-server":
            publish_server(args.source)
        elif args.command == "publish-profile":
            publish_profile(args.source)
        elif args.command == "snapshot":
            snapshot_configs(args.directory)
        elif args.command == "rollback":
            rollback_configs(args.directory)
        elif args.command == "verify-managed":
            verify_managed()
        elif args.command == "first-key":
            print(first_configured_key())
        elif args.command == "wait-key":
            wait_for_key(args.device_id)
        elif args.command == "ensure-viewer":
            ensure_viewer()
        return 0
    except HostError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    except Exception:
        print("Error: host operation failed without publishing credentials.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
