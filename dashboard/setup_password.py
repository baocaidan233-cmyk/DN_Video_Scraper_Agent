"""
Generate a PBKDF2-SHA256 password hash for config.yaml dashboard.password_hash.

Usage:
    python3 -m dashboard.setup_password
    # → paste the output into config.yaml under dashboard.password_hash
"""
from __future__ import annotations

import getpass
import hashlib
import os
import sys


def hash_password(password: str) -> str:
    salt = os.urandom(32).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2:sha256:100000:{salt}:{dk.hex()}"


if __name__ == "__main__":
    try:
        password = getpass.getpass("Enter dashboard password: ")
        confirm = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    if password != confirm:
        print("Passwords don't match.", file=sys.stderr)
        sys.exit(1)

    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    print("\nAdd this to config.yaml under dashboard:")
    print(f"  password_hash: \"{hash_password(password)}\"")
