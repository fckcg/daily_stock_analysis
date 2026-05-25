"""baseline (anchor for stamp)

Revision ID: 0001
Revises:
Create Date: 2026-05-25 00:00:00

The project shipped without alembic for ~3 years; existing installations
have tables created by ``Base.metadata.create_all()``. This revision is
the anchor used by ``alembic stamp`` so that those installations can opt
in to the migration system without forcing a destructive recreate.

The ``upgrade()`` body is intentionally empty: any actual schema delta
introduced from this point forward must live in a *new* revision file.
"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Anchor migration: no-op. See module docstring.
    pass


def downgrade() -> None:
    # Downgrading past the anchor is unsupported; we never created tables
    # via alembic in the first place, and dropping ~20 application tables
    # would destroy user data.
    raise NotImplementedError(
        "downgrade past the alembic baseline is unsupported; restore from backup instead"
    )
