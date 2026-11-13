#!/usr/bin/env python3
"""
Matrix (Conduwuit) bootstrap and administration for ACE.

Handles one-time system init (admin user, ops room) and per-dispatch
bot user/room creation. Uses Conduwuit admin CLI + Matrix Client API.

Usage:
    python3 scripts/matrix-bootstrap.py init                    # one-time setup
    python3 scripts/matrix-bootstrap.py status                  # health check
    python3 scripts/matrix-bootstrap.py register-human <user>   # create human user

Exit codes:
    0  Success
    1  Conduwuit not reachable or operation failed
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_ENV_PATH = REPO_ROOT / "40-tools" / "10-docker" / "matrix" / ".matrix-env"
HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER", "http://localhost:8448")
SERVER_NAME = "ace.local"
ADMIN_USER = "ace-admin"
REGISTRATION_TOKEN = os.environ.get("MATRIX_REGISTRATION_TOKEN", "ace-local-bootstrap")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only, no requests/httpx)
# ---------------------------------------------------------------------------

def _matrix_request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    homeserver: str | None = None,
) -> dict:
    """Make an HTTP request to the Matrix Client API."""
    url = f"{homeserver or HOMESERVER_URL}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        try:
            error_data = json.loads(error_body)
        except Exception:
            error_data = {"errcode": "UNKNOWN", "error": error_body}
        raise MatrixError(e.code, error_data) from None


class MatrixError(Exception):
    def __init__(self, status: int, data: dict):
        self.status = status
        self.data = data
        self.errcode = data.get("errcode", "UNKNOWN")
        self.error = data.get("error", str(data))
        super().__init__(f"Matrix {status}: {self.errcode} - {self.error}")


# ---------------------------------------------------------------------------
# User registration via Matrix API + registration token
# ---------------------------------------------------------------------------

def _get_bootstrap_token(retries: int = 5) -> str | None:
    """Extract the one-time bootstrap token from Conduwuit container logs.

    On first startup, Continuwuity prints a one-time token that must be used
    to create the first (admin) user. The configured REGISTRATION_TOKEN only
    becomes active after this first user exists.

    Retries because podman log output may not be flushed immediately after
    the container starts.
    """
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["podman", "logs", "conduwuit"],
                capture_output=True, text=True, timeout=10,
            )
            # Conduwuit may log to stdout or stderr depending on version
            logs = r.stdout + "\n" + r.stderr
            # Match: "using the registration token <TOKEN>" (with ANSI codes)
            m = re.search(r"registration token\s+\x1b\[1;32m(\S+)\x1b\[0m", logs)
            if m:
                return m.group(1)
            # Fallback without ANSI codes
            m = re.search(r"using the registration token\s+(\S+)", logs)
            if m:
                return m.group(1)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        if attempt < retries - 1:
            time.sleep(2)
    return None


def _register_user(username: str, password: str, token: str | None = None) -> dict:
    """Register a user via the Matrix registration token flow.

    Uses the two-step UIA flow:
      1. POST /register with username+password → get session ID
      2. POST /register with session + m.login.registration_token auth
    """
    reg_token = token or REGISTRATION_TOKEN
    path = "/_matrix/client/v3/register"
    reg_body = {
        "username": username,
        "password": password,
        "inhibit_login": False,
    }

    # Step 1: initiate — expect 401 with session
    try:
        return _matrix_request("POST", path, body=reg_body)
    except MatrixError as e:
        if e.status == 401 and "session" in e.data:
            session = e.data["session"]
        elif e.status == 400 and e.errcode == "M_USER_IN_USE":
            raise  # caller handles
        else:
            raise

    # Step 2: complete with registration token
    reg_body["auth"] = {
        "type": "m.login.registration_token",
        "token": reg_token,
        "session": session,
    }
    return _matrix_request("POST", path, body=reg_body)


# ---------------------------------------------------------------------------
# Wait for Conduwuit
# ---------------------------------------------------------------------------

def wait_for_conduwuit(timeout: int = 60) -> bool:
    """Wait for Conduwuit to be reachable. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _matrix_request("GET", "/_matrix/client/versions")
            return True
        except (URLError, OSError, MatrixError):
            time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Matrix operations
# ---------------------------------------------------------------------------

def matrix_login(username: str, password: str) -> str:
    """Login and return access token."""
    resp = _matrix_request("POST", "/_matrix/client/v3/login", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    })
    return resp["access_token"]


def matrix_create_room(
    *,
    alias: str,
    name: str,
    topic: str = "",
    invite: list[str] | None = None,
    token: str,
) -> str:
    """Create a Matrix room and return its room_id."""
    try:
        resp = _matrix_request("POST", "/_matrix/client/v3/createRoom", body={
            "room_alias_name": alias,
            "name": name,
            "topic": topic,
            "preset": "private_chat",
            "invite": invite or [],
        }, token=token)
        return resp["room_id"]
    except MatrixError as e:
        if e.errcode == "M_ROOM_IN_USE":
            # Room already exists — resolve alias to room_id
            resp = _matrix_request(
                "GET",
                f"/_matrix/client/v3/directory/room/%23{alias}:{SERVER_NAME}",
                token=token,
            )
            return resp["room_id"]
        raise


def matrix_invite(room_id: str, user_id: str, *, token: str) -> None:
    """Invite a user to a room."""
    try:
        _matrix_request("POST", f"/_matrix/client/v3/rooms/{room_id}/invite", body={
            "user_id": user_id,
        }, token=token)
    except MatrixError as e:
        # Already joined or invited — not an error
        if e.errcode in ("M_FORBIDDEN", "M_BAD_STATE"):
            return
        raise


# ---------------------------------------------------------------------------
# .matrix-env file
# ---------------------------------------------------------------------------

def load_matrix_env() -> dict:
    """Load .matrix-env file. Returns dict with admin_token, ops_room_id."""
    if not MATRIX_ENV_PATH.exists():
        return {}
    result = {}
    for line in MATRIX_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def save_matrix_env(data: dict) -> None:
    """Write .matrix-env file."""
    MATRIX_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-generated by matrix-bootstrap.py — do not edit manually",
        f"MATRIX_ADMIN_TOKEN={data.get('admin_token', '')}",
        f"MATRIX_OPS_ROOM_ID={data.get('ops_room_id', '')}",
        f"MATRIX_ADMIN_USER=@{ADMIN_USER}:{SERVER_NAME}",
        f"MATRIX_HOMESERVER={HOMESERVER_URL}",
    ]
    MATRIX_ENV_PATH.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    """One-time system initialization: admin user + ops room."""
    print("Waiting for Conduwuit...")
    if not wait_for_conduwuit(timeout=60):
        print("ERROR: Conduwuit not reachable at", HOMESERVER_URL, file=sys.stderr)
        sys.exit(1)
    print("  Conduwuit is ready.")

    # Check if already initialized
    existing = load_matrix_env()
    if existing.get("MATRIX_ADMIN_TOKEN") and existing.get("MATRIX_OPS_ROOM_ID"):
        print("  Already initialized. Verifying...")
        try:
            _matrix_request(
                "GET", "/_matrix/client/v3/account/whoami",
                token=existing["MATRIX_ADMIN_TOKEN"],
            )
            print(f"  Admin token valid. Ops room: {existing['MATRIX_OPS_ROOM_ID']}")
            print("Init complete (idempotent).")
            return
        except MatrixError:
            print("  Existing token invalid. Re-initializing...")

    # Create admin user via Matrix registration API.
    # On first startup Continuwuity issues a one-time bootstrap token;
    # the configured REGISTRATION_TOKEN only activates after the first user.
    password = secrets.token_urlsafe(24)
    print(f"Creating admin user @{ADMIN_USER}:{SERVER_NAME}...")

    bootstrap_token = _get_bootstrap_token()
    if bootstrap_token:
        print(f"  Found bootstrap token from Conduwuit logs.")

    try:
        # Try bootstrap token first (needed for first user), fall back to config token
        resp = _register_user(ADMIN_USER, password, token=bootstrap_token)
        admin_token = resp["access_token"]
        print(f"  User created and logged in.")
    except MatrixError as e:
        if e.errcode == "M_USER_IN_USE":
            # User already exists — try logging in
            print("  User already exists, logging in...")
            try:
                admin_token = matrix_login(ADMIN_USER, password)
            except MatrixError:
                print(
                    f"ERROR: Admin user @{ADMIN_USER}:{SERVER_NAME} exists but "
                    f"password is unknown.\n"
                    f"Delete the volume and re-init:\n"
                    f"  make matrix-stop && podman volume rm ace-conduwuit-db "
                    f"&& make matrix-init",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            print(f"ERROR: Registration failed: {e}", file=sys.stderr)
            sys.exit(1)
    print(f"  Token: {admin_token[:20]}...")

    # Create ops room
    print("Creating #ops:ace.local room...")
    ops_room_id = matrix_create_room(
        alias="ops",
        name="ACE Operations",
        topic="System-wide agent lifecycle events",
        token=admin_token,
    )
    print(f"  Room ID: {ops_room_id}")

    # Save .matrix-env
    save_matrix_env({
        "admin_token": admin_token,
        "ops_room_id": ops_room_id,
    })
    print(f"  Saved to {MATRIX_ENV_PATH.relative_to(REPO_ROOT)}")

    print("\nInit complete. Connect via Element:")
    print(f"  Homeserver: {HOMESERVER_URL}")
    print(f"  Register a human: make matrix-register-human USER=yourname")


def cmd_status(args: argparse.Namespace) -> None:
    """Check Conduwuit health, list rooms and users."""
    # Health check
    try:
        versions = _matrix_request("GET", "/_matrix/client/versions")
        print(f"Conduwuit: OK ({HOMESERVER_URL})")
        print(f"  Versions: {', '.join(versions.get('versions', []))}")
    except (URLError, OSError) as e:
        print(f"Conduwuit: UNREACHABLE ({HOMESERVER_URL})")
        print(f"  Error: {e}")
        sys.exit(1)
    except MatrixError as e:
        print(f"Conduwuit: ERROR ({e})")
        sys.exit(1)

    # Check admin token
    env = load_matrix_env()
    admin_token = env.get("MATRIX_ADMIN_TOKEN")
    if not admin_token:
        print("  Admin: not initialized (run 'make matrix-init')")
        return

    try:
        whoami = _matrix_request(
            "GET", "/_matrix/client/v3/account/whoami",
            token=admin_token,
        )
        print(f"  Admin: {whoami.get('user_id', 'unknown')}")
    except MatrixError:
        print("  Admin: token invalid")

    # List joined rooms
    try:
        rooms = _matrix_request(
            "GET", "/_matrix/client/v3/joined_rooms",
            token=admin_token,
        )
        room_ids = rooms.get("joined_rooms", [])
        print(f"  Rooms: {len(room_ids)} joined")
        for rid in room_ids[:10]:
            print(f"    {rid}")
        if len(room_ids) > 10:
            print(f"    ... and {len(room_ids) - 10} more")
    except MatrixError:
        print("  Rooms: unable to list")

    # Ops room
    ops_room_id = env.get("MATRIX_OPS_ROOM_ID")
    if ops_room_id:
        print(f"  Ops room: {ops_room_id}")


def cmd_register_human(args: argparse.Namespace) -> None:
    """Create a human user for Element access."""
    username = args.username
    if not username:
        print("ERROR: username required", file=sys.stderr)
        sys.exit(1)

    # Generate password
    password = secrets.token_urlsafe(16)

    print(f"Creating user @{username}:{SERVER_NAME}...")
    try:
        _register_user(username, password)
    except MatrixError as e:
        if e.errcode == "M_USER_IN_USE":
            print(f"  User already exists (login with existing password).")
        else:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # Invite to ops room
    env = load_matrix_env()
    admin_token = env.get("MATRIX_ADMIN_TOKEN")
    ops_room_id = env.get("MATRIX_OPS_ROOM_ID")
    if admin_token and ops_room_id:
        user_id = f"@{username}:{SERVER_NAME}"
        matrix_invite(ops_room_id, user_id, token=admin_token)
        print(f"  Invited to #ops:{SERVER_NAME}")

    print(f"\nUser created:")
    print(f"  Username: {username}")
    print(f"  Password: {password}")
    print(f"  Homeserver: {HOMESERVER_URL}")
    print(f"\nConnect with Element Desktop:")
    print(f"  1. Set homeserver to {HOMESERVER_URL}")
    print(f"  2. Login as {username} with the password above")
    print(f"  3. Accept the invite to #ops:{SERVER_NAME}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Matrix (Conduwuit) bootstrap for ACE",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="One-time system initialization")
    sub.add_parser("status", help="Health check and room listing")

    p_human = sub.add_parser("register-human", help="Create a human user")
    p_human.add_argument("username", help="Username for the new user")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "register-human":
        cmd_register_human(args)


if __name__ == "__main__":
    main()
