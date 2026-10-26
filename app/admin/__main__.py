"""Operator commands for accounts, run on the server with database access:

    python -m app.admin set-password --email dev@docmind.local
    python -m app.admin deactivate --email someone@example.com
    python -m app.admin activate --email someone@example.com

set-password is how a legacy, password-less account (created before Phase 8) is claimed by
whoever controls the server; it never creates an account. set-password and deactivate also
revoke every session of the account, so its refresh and access tokens stop working at once.
The password is read with a hidden prompt (twice), or from DOCMIND_ADMIN_NEW_PASSWORD for
automation; it is never printed or logged.
"""

import argparse
import getpass
import os
import sys

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import create_db_engine
from app.core.security import hash_password
from app.models import User
from app.schemas.auth import RegisterRequest, normalize_email
from app.services import sessions

PASSWORD_ENV = "DOCMIND_ADMIN_NEW_PASSWORD"


def _read_password() -> str:
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env is not None:
        return from_env
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match")
    return first


def _user(session: Session, email: str) -> User:
    user = session.scalars(select(User).where(User.email == normalize_email(email))).one_or_none()
    if user is None:
        raise SystemExit("No account with this email")
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("set-password", "deactivate", "activate"):
        sub.add_parser(name).add_argument("--email", required=True)
    args = parser.parse_args(argv)

    engine = create_db_engine(Settings())
    try:
        with Session(engine) as session:
            user = _user(session, args.email)
            if args.command == "set-password":
                password = _read_password()
                try:
                    # Same policy as registration.
                    RegisterRequest(email=user.email, password=password)
                except ValidationError:
                    raise SystemExit(
                        "Password does not meet the policy (12-1024 characters)"
                    ) from None
                user.password_hash = hash_password(password)
                # A new password ends every existing login.
                sessions.revoke_all(session, user.id, "password_changed")
            else:
                user.is_active = args.command == "activate"
                if not user.is_active:
                    sessions.revoke_all(session, user.id, "deactivated")
            session.commit()
            print(f"{args.command}: done for user {user.id}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
