"""drop degenerate ivfflat index on knowledge_chunks

The ix_knowledge_chunks_embedding_ivfflat index (lists=100) was created in
the initial migration on an empty table. pgvector's ivfflat index computes
its cluster centroids from whatever data exists at CREATE INDEX time — on
an empty table, that produces a degenerate index that approximates nearest
neighbors incorrectly, to the point of silently returning zero rows for
`ORDER BY embedding <=> :query LIMIT :n` for any query vector that isn't
(near-)identical to an already-indexed one. Verified directly against a
live Postgres instance: dropping the index and falling back to an exact
sequential scan fixed similarity search immediately (see Phase 4 report).

At the row counts a small-business knowledge base will actually have
(tens to low hundreds of chunks), an exact scan is fast enough that an
approximate index isn't worth the risk — pgvector's own guidance is to
build ivfflat only once there's real data to cluster on. Re-add an ANN
index (ivfflat with lists tuned to sqrt(row_count), or hnsw) in a future
migration if/when the corpus grows large enough to need it.

Revision ID: 5e5509c26b5e
Revises: 0d2ab6fab908
Create Date: 2026-09-08 22:22:29.321404

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e5509c26b5e"
down_revision: Union[str, None] = "0d2ab6fab908"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_knowledge_chunks_embedding_ivfflat", table_name="knowledge_chunks")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_ivfflat ON knowledge_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
