from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import User


def get_or_create_user(session: Session, email: str) -> User:
    """Return the user with this email, creating it if needed (safe under concurrency)."""
    email = email.strip().lower()
    user = session.scalars(select(User).where(User.email == email)).one_or_none()
    if user is not None:
        return user
    # ON CONFLICT DO NOTHING: two first requests racing to create the same user both succeed.
    session.execute(
        insert(User).values(email=email).on_conflict_do_nothing(index_elements=["email"])
    )
    session.commit()
    return session.scalars(select(User).where(User.email == email)).one()
