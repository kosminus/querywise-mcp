"""Custom SQLAlchemy column types for the SQLite metadata store.

The original app used pgvector ``Vector`` columns. On SQLite we store the
embedding as a packed float32 BLOB (the exact byte layout sqlite-vec expects),
transparently converting to/from ``list[float]`` so the rest of the codebase
keeps assigning and reading plain Python lists.
"""

from array import array

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator


def serialize_floats(vec: list[float]) -> bytes:
    """Pack a vector into the little-endian float32 layout sqlite-vec uses."""
    return array("f", vec).tobytes()


def deserialize_floats(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


class Embedding(TypeDecorator):
    """A vector column stored as a float32 BLOB.

    Assign a ``list[float]`` and read back a ``list[float]``; ``None`` round-trips
    as ``None`` (used to mark "needs embedding").
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return serialize_floats(list(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return deserialize_floats(value)
