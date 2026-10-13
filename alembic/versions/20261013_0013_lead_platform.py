"""Which platform a lead came from: messenger, instagram or web.

``chatbot_leads.psid`` holds an Instagram customer's IGSID as well as a Messenger PSID, so
the number alone no longer says where to reach them. Every lead that existed before this
column is a Messenger one when it has a psid and a web chat one when it does not.

Guarded like every revision here, so running it twice does nothing the second time.
"""
import sqlalchemy as sa
from alembic import op

from src.migration_utils import has_column

revision = "20261013_0013"
down_revision = "20261006_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("chatbot_leads", "platform"):
        op.add_column(
            "chatbot_leads",
            sa.Column("platform", sa.String(16), nullable=False, server_default="messenger"),
        )
        op.execute("UPDATE chatbot_leads SET platform = 'web' WHERE psid IS NULL")


def downgrade() -> None:
    if has_column("chatbot_leads", "platform"):
        op.drop_column("chatbot_leads", "platform")
