import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.asset import Asset
    from app.models.institution import Institution
    from app.models.user import User


class AssetGroup(Base):
    """A user-facing "wallet" that bundles related assets under one total.

    Groups can be manually created and linked to an investment account, or
    created by a provider for an attributable brokerage account. Positions
    collapse into a single expandable row instead of sibling cards.

    Assets link via nullable `group_id` — deleting a group leaves its
    assets behind ungrouped rather than cascading away real user data.
    """

    __tablename__ = "asset_groups"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_asset_groups_account_id"),
        CheckConstraint(
            "tax_treatment IN ('taxable', 'roth', 'traditional', 'hsa', 'other')",
            name="ck_asset_groups_tax_treatment",
        ),
        # Mirrors migration 071. Declared here so the SQLite test database
        # enforces it too — while this index lived only in the migration, a
        # cross-workspace duplicate passed every test and failed on Postgres.
        Index(
            "ux_asset_groups_workspace_source_external",
            "workspace_id",
            "user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    icon: Mapped[str] = mapped_column(String(50), default="wallet")
    color: Mapped[str] = mapped_column(String(7), default="#0EA5E9")
    position: Mapped[int] = mapped_column(Integer, default=0)

    # The tax character of the real account this wallet stands for. No provider
    # exposes it, so it is user-set. Existing and synced wallets default to
    # `taxable` — the treatment that keeps lot tracking and gain figures on;
    # defaulting the other way would silently hide tax consequences.
    tax_treatment: Mapped[str] = mapped_column(
        String(20), default="taxable", server_default="taxable"
    )

    # Provenance fields — mirror Asset's sync fields so sync code can
    # upsert groups idempotently by (user_id, source, external_id).
    source: Mapped[str] = mapped_column(String(50), default="manual")
    connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_connections.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Explicit identity for a manual portfolio. Provider wallets resolve the
    # account through their holdings instead; names are never join keys.
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # The institution backing a synced wallet (issue #345); null for manual
    # wallets. Renders the "Synced from …" subtitle without falling back to
    # the connection's first institution.
    institution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    user: Mapped["User"] = relationship()
    assets: Mapped[list["Asset"]] = relationship(back_populates="group")
    institution: Mapped[Optional["Institution"]] = relationship(lazy="selectin")
