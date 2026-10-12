"""Shared Qdrant client construction.

Centralises the URL→client parsing used by both the RAGHandle deployment
(``rag.py``) and the offline ingestion CLI (``ingestion.py``), so the port/SSL
handling lives in exactly one place instead of being copy-pasted.
"""

from urllib.parse import urlparse

from qdrant_client import QdrantClient


def make_qdrant_client(qdrant_url: str, verify_ssl: bool = True) -> QdrantClient:
    """Build a :class:`QdrantClient` from a URL.

    Parses the URL explicitly so the client gets the correct host/port/https.
    When given just ``https://host`` with no port, some qdrant-client versions
    silently fall back to their default port (6333) instead of 443, causing a
    timeout that looks like an SSL issue — so derive the port from the scheme.
    """
    parsed = urlparse(qdrant_url)
    is_https = parsed.scheme == "https"
    return QdrantClient(
        host=parsed.hostname,
        port=parsed.port or (443 if is_https else 6333),
        https=is_https,
        verify=verify_ssl,
        check_compatibility=verify_ssl,
    )
