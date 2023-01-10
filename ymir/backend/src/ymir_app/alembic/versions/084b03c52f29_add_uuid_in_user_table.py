"""add uuid in User table

Revision ID: 084b03c52f29
Revises: b88055a18c30
Create Date: 2022-12-16 15:31:23.411505

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "084b03c52f29"
down_revision = "b88055a18c30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    conn = op.get_bind()
    if conn.engine.name == "sqlite":
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(sa.Column("uuid", sa.String(length=36), nullable=True))
        return
    else:
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(sa.Column("uuid", sa.String(length=36), nullable=False))
    try:
        conn.execute("UPDATE user SET uuid = uuid()")
    except Exception as e:
        print("Could not add uuid for user: %s" % e)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("uuid")

    # ### end Alembic commands ###
