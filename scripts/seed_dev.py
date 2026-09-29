"""
Create local development users. Only runs against a local SQLite database.

    SEED_PASSWORD=... python -m scripts.seed_dev
"""

import logging
import os
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db.init_db import create_tables
from app.db.models import User
from app.db.session import SessionLocal

logger = logging.getLogger("seed")

USERS = (
    ("Admin Local", "admin@local.test", "admin"),
    ("Colaborador Local", "colaborador@local.test", "collaborator"),
)


def main() -> int:
    configure_logging()
    settings = get_settings()
    if settings.is_deployed or not settings.database_url.startswith("sqlite"):
        logger.error("seed_refused", extra={"reason": "seed_dev only runs against a local SQLite database"})
        return 1
    password = os.getenv("SEED_PASSWORD", "")
    if len(password) < 8:
        logger.error("seed_refused", extra={"reason": "set SEED_PASSWORD (min 8 chars)"})
        return 1

    create_tables()
    with SessionLocal() as db:
        for name, email, role in USERS:
            if not db.query(User).filter(User.email == email).first():
                db.add(User(name=name, email=email, role=role, password_hash=hash_password(password)))
        db.commit()
    logger.info("seed_completed", extra={"users": [email for _, email, _ in USERS]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
