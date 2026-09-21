"""Create chat sessions and messages.

Revision ID: 20260921_01
Revises:
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260921_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    message_role = postgresql.ENUM(
        "user", "assistant", "system", name="message_role", create_type=False
    )
    message_role.create(op.get_bind())
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mcp_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    postgresql.ENUM(name="message_role").drop(op.get_bind())
