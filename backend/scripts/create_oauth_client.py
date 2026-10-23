#!/usr/bin/env python
"""Register a new OAuth 2.1 client in the HomePilot database.

Usage (from the backend/ directory with the venv active):

    python scripts/create_oauth_client.py \\
        --name "Alexa+" \\
        --redirect-uri https://alexa.amazon.com/callback \\
        --redirect-uri https://pitangui.amazon.com/callback \\
        --public

    python scripts/create_oauth_client.py \\
        --name "Internal Dashboard" \\
        --redirect-uri https://dashboard.example.com/callback
        # (no --public → confidential; secret is printed once)

The script prints the client_id and, for confidential clients, the client_secret.
The secret is shown only once; it is stored only as a SHA-256 hash.
"""
import argparse
import hashlib
import os
import secrets
import sys

# Allow running from repo root or backend/
_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.join(_here, "..")
if _backend not in sys.path:
    sys.path.insert(0, _backend)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an OAuth 2.1 client")
    parser.add_argument("--name", required=True, help="Human-readable client name")
    parser.add_argument(
        "--redirect-uri",
        dest="redirect_uris",
        action="append",
        required=True,
        metavar="URI",
        help="Allowed redirect URI (repeatable)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        default=False,
        help="Register as a public client (no secret, e.g. mobile/SPA). Default: confidential.",
    )
    args = parser.parse_args()

    from app.db import SessionLocal, engine
    from app.models import Base, OAuthClient

    # Ensure tables exist (creates them if AUTO_CREATE_TABLES was not run)
    Base.metadata.create_all(engine)

    is_public = args.public
    client_id = f"client_{secrets.token_urlsafe(16)}"
    raw_secret: str | None = None
    secret_hash: str | None = None

    if not is_public:
        raw_secret = f"cs_{secrets.token_urlsafe(32)}"
        secret_hash = _sha256(raw_secret)

    with SessionLocal() as db:
        existing = db.query(OAuthClient).filter_by(name=args.name).first()
        if existing:
            print(f"WARNING: a client named {args.name!r} already exists (id={existing.id}). Creating a new one anyway.")

        client = OAuthClient(
            id=client_id,
            name=args.name,
            secret_hash=secret_hash,
            redirect_uris=args.redirect_uris,
            is_public=is_public,
        )
        db.add(client)
        db.commit()

    print(f"\n✓ OAuth client created")
    print(f"  client_id    : {client_id}")
    print(f"  name         : {args.name}")
    print(f"  type         : {'public' if is_public else 'confidential'}")
    print(f"  redirect_uris: {args.redirect_uris}")
    if raw_secret:
        print(f"\n  client_secret: {raw_secret}")
        print("  (stored as hash only; this is the ONLY time it will be shown)")
    print()


if __name__ == "__main__":
    main()
