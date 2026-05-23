"""Vector similarity search over the SQLite metadata store.

Embeddings are stored as float32 BLOBs (see ``db.types.Embedding``). When the
sqlite-vec extension is loaded we let SQLite compute cosine distance with
``vec_distance_cosine``; otherwise we fall back to an in-process cosine over the
candidate rows. The dataset (a connection's tables/columns/glossary/metrics) is
small enough that the Python fallback is effectively instant, which keeps the
server correct even where the native extension can't load.
"""

import logging
import math

from sqlalchemy import LargeBinary, bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from querywise_mcp.db.types import serialize_floats

logger = logging.getLogger(__name__)

# Set to True once the sqlite-vec extension successfully loads on a connection.
vec_enabled = False


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def knn(
    db: AsyncSession,
    model,
    embedding_attr: str,
    query_embedding: list[float],
    limit: int,
    *filters,
) -> list[tuple[object, float]]:
    """Return up to ``limit`` ``(instance, similarity)`` pairs, best first.

    ``similarity`` is cosine similarity in ``[-1, 1]`` (higher = closer).
    Rows with a NULL embedding are skipped.
    """
    col = getattr(model, embedding_attr)
    base_filters = (col.isnot(None), *filters)

    if vec_enabled:
        try:
            qparam = bindparam(
                "q", serialize_floats(query_embedding), type_=LargeBinary
            )
            distance = func.vec_distance_cosine(col, qparam)
            stmt = (
                select(model, distance.label("distance"))
                .where(*base_filters)
                .order_by(distance.asc())
                .limit(limit)
            )
            rows = (await db.execute(stmt)).all()
            return [(row[0], 1.0 - float(row[1])) for row in rows]
        except Exception:
            logger.warning(
                "sqlite-vec distance query failed; using in-process cosine.",
                exc_info=True,
            )
            await db.rollback()

    # In-process fallback.
    rows = (await db.execute(select(model).where(*base_filters))).scalars().all()
    scored: list[tuple[object, float]] = []
    for row in rows:
        vec = getattr(row, embedding_attr)
        if vec:
            scored.append((row, _cosine(query_embedding, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]
