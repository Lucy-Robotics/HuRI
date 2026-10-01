import argparse
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, List

import httpx
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

USER_ID_FILE = os.path.expanduser("~/.huri_user_id")


class RemoteEmbedder:
    """Embed via an OpenAI-compatible ``/v1/embeddings`` endpoint (e.g. llama.cpp).

    Drop-in for the subset of ``SentenceTransformer`` this tool uses: a single
    ``.encode(text, normalize_embeddings=...)`` returning a 1-D numpy array, so
    the existing ``.tolist()`` / ``len(...)`` call sites keep working unchanged.
    """

    def __init__(self, url: str, model_name: str):
        self.url = url.rstrip("/")
        self.model_name = model_name
        self._client = httpx.Client(timeout=60.0, verify=False)

    def encode(self, text: str, normalize_embeddings: bool = True) -> np.ndarray:
        resp = self._client.post(
            f"{self.url}/v1/embeddings",
            json={"model": self.model_name, "input": str(text)},
        )
        resp.raise_for_status()
        vec = np.asarray(resp.json()["data"][0]["embedding"], dtype=np.float32)
        if normalize_embeddings:
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
        return vec


def _split_sentences(text: str) -> list[str]:
    """Simple sentence splitter."""
    result: List = []
    sentences = re.split(r"(?<=[.!?])\s+", text)

    for s in sentences:
        parts = s.split("\n\n")
        result.extend(parts)
    return [s.strip() for s in result if s.strip()]


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """
    Fallback: fixed-size chunking by sentences.
    Used when --chunking=fixed.
    """
    chunks: List = []
    current_chunk: List = []
    current_length: int = 0
    sentences = _split_sentences(text)

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        sentence_length = len(sentence.split())

        if current_length + sentence_length > chunk_size and current_chunk:
            overlap_words: int = 0
            overlap_sentences: List = []
            chunks.append(" ".join(current_chunk))

            for s in reversed(current_chunk):
                overlap_words += len(s.split())
                overlap_sentences.insert(0, s)
                if overlap_words >= overlap:
                    break

            current_chunk = overlap_sentences
            current_length = overlap_words

        current_chunk.append(sentence)
        current_length += sentence_length

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return chunks


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text.strip()
    except ImportError:
        pass

    print("ERROR: Install a PDF library: pip install pymupdf  OR  pip install pypdf")
    sys.exit(1)


def get_user_id(provided_id: str | None = None) -> str:
    if provided_id:
        return provided_id
    if os.path.exists(USER_ID_FILE):
        with open(USER_ID_FILE) as f:
            uid = f.read().strip()
            if uid:
                return uid
    new_id = str(uuid.uuid4())
    with open(USER_ID_FILE, "w") as f:
        f.write(new_id)
    print(f"Generated new user_id: {new_id}")
    return new_id


def ensure_collection(client: QdrantClient, collection: str, vector_size: int):
    collections = [c.name for c in client.get_collections().collections]
    if collection not in collections:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"Created collection: {collection}")


def ingest_chunks(
    client: QdrantClient,
    model: Any,
    collection: str,
    chunks: list[str],
    _user_id: str,
    source: str,
    doc_type: str = "document",
):
    """Embed chunks and upsert into Qdrant."""
    points = []
    timestamp = datetime.now().isoformat()

    for i, chunk in enumerate(chunks):
        vector = model.encode(chunk, normalize_embeddings=True).tolist()
        points.append(
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "_user_id": _user_id,
                    "source": source,
                    "type": doc_type,
                    "chunk_index": i,
                    "timestamp": timestamp,
                },
            )
        )

    if points:
        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            client.upsert(collection_name=collection, points=batch)

    return len(points)


def chunk_strat(text: str, args, model: Any) -> list[str] | Any:
    """Pick the right chunking strategy based on args."""
    if args.chunking == "semantic":
        # Thomas: I need to import here, bceause it takes too much time earlier, or
        # use a jupyter notebook to do it instead
        from .semantic_chunker import SemanticChunker

        chunker = SemanticChunker(
            model=model,
            strategy=args.semantic_strategy,
        )
        return chunker.chunk(text)
    else:
        return chunk_text(text, chunk_size=args.chunk_size, overlap=args.overlap)


def cmd_pdf(args, client, model, _user_id):
    """Ingest PDF files."""
    files: List[Path] = []
    for path in args.files:
        p = Path(path)
        if p.is_dir():
            files.extend(p.glob("**/*.pdf"))
        elif p.suffix.lower() == ".pdf":
            files.append(p)
        else:
            print(f"Skipping non-PDF: {path}")

    if not files:
        print("No PDF files found.")
        return

    sample = model.encode("test", normalize_embeddings=True)
    ensure_collection(client, args.collection, len(sample))

    total = 0
    for pdf_path in files:
        print(f"\nProcessing: {pdf_path}")
        text = extract_text_from_pdf(str(pdf_path))

        if not text.strip():
            print(f"  WARNING: No text extracted from {pdf_path}")
            continue

        chunks = chunk_strat(text, args, model)
        count = ingest_chunks(
            client,
            model,
            args.collection,
            chunks,
            _user_id,
            source=pdf_path.name,
            doc_type="pdf",
        )
        print(f"  -> {count} chunks ingested")
        total += count

    print(f"\nDone. Total: {total} chunks from {len(files)} PDF(s)")


def cmd_text(args, client, model, _user_id):
    """Ingest text files."""
    sample = model.encode("test", normalize_embeddings=True)
    ensure_collection(client, args.collection, len(sample))

    total = 0
    for file_path in args.files:
        p = Path(file_path)
        if not p.exists():
            print(f"File not found: {file_path}")
            continue

        print(f"\nProcessing: {file_path}")
        text = p.read_text(encoding="utf-8")

        if not text.strip():
            print(f"  WARNING: File is empty: {file_path}")
            continue

        chunks = chunk_strat(text, args, model)
        count = ingest_chunks(
            client,
            model,
            args.collection,
            chunks,
            _user_id,
            source=p.name,
            doc_type="text",
        )
        print(f"  -> {count} chunks ingested")
        total += count

    print(f"\nDone. Total: {total} chunks from {len(args.files)} file(s)")


def cmd_write(args, client, model, _user_id):
    """Write text interactively and ingest it."""
    title = args.title or f"note_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"Write your text below (title: '{title}')")
    print("Press Ctrl+D (Linux/Mac) or Ctrl+Z then Enter (Windows) when done.")
    print("-" * 40)

    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass

    text = "\n".join(lines).strip()

    if not text:
        print("Nothing to ingest.")
        return

    print(f"\n{'-' * 40}")
    print(f"Received {len(text)} characters")

    sample = model.encode("test", normalize_embeddings=True)
    ensure_collection(client, args.collection, len(sample))

    chunks = chunk_strat(text, args, model)
    count = ingest_chunks(
        client,
        model,
        args.collection,
        chunks,
        _user_id,
        source=title,
        doc_type="manual",
    )

    print(f"Done. Ingested {count} chunks as '{title}'")


def cmd_profile(args, client, model, _user_id):
    """Store always-on profile facts about the user (name, etc.).

    Unlike regular documents, profile facts are NOT retrieved by vector
    similarity. The RAG handle pulls them by filter (_user_id + type=profile)
    on every query and injects them into the system prompt, so the character
    always knows them.
    """
    sample = model.encode("test", normalize_embeddings=True)
    ensure_collection(client, args.collection, len(sample))

    facts: List[str] = []
    if args.name:
        facts.append(f"The user's name is {args.name}.")
    for fact in args.fact or []:
        facts.append(fact)

    if not facts:
        print("Nothing to store. Use --name and/or --fact 'some fact'.")
        return

    # Replace the existing profile so facts don't pile up across runs.
    client.delete(
        collection_name=args.collection,
        points_selector=Filter(
            must=[
                FieldCondition(key="_user_id", match=MatchValue(value=_user_id)),
                FieldCondition(key="type", match=MatchValue(value="profile")),
            ]
        ),
    )

    count = ingest_chunks(
        client,
        model,
        args.collection,
        facts,
        _user_id,
        source="profile",
        doc_type="profile",
    )
    print(f"Stored {count} profile fact(s) for user {_user_id}")


def cmd_list(args, client, model, _user_id):
    """List what's in the database for this user."""

    try:
        info = client.get_collection(args.collection)
        print(f"Collection: {args.collection}")
        print(f"Total points: {info.points_count}")
    except Exception:
        print(f"Collection '{args.collection}' doesn't exist.")
        return

    results = client.scroll(
        collection_name=args.collection,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="_user_id", match=MatchValue(value=_user_id)),
            ]
        ),
        limit=100,
        with_payload=True,
        with_vectors=False,
    )

    points = results[0]
    if not points:
        print(f"No documents found for user {_user_id}")
        return

    sources = {}
    for p in points:
        source = p.payload.get("source", "unknown")
        doc_type = p.payload.get("type", "unknown")
        if source not in sources:
            sources[source] = {"count": 0, "type": doc_type}
        sources[source]["count"] += 1

    print(f"\nDocuments for user {_user_id}:")
    print(f"{'Source':<40} {'Type':<10} {'Chunks':<8}")
    print("-" * 60)
    for source, info in sorted(sources.items()):
        print(f"{source:<40} {info['type']:<10} {info['count']:<8}")
    print(f"\nTotal: {len(points)} chunks across {len(sources)} sources")


def cmd_delete(args, client, model, _user_id):
    """Delete documents by source name."""

    if not args.source:
        print("Specify --source to delete. Use 'list' command to see sources.")
        return

    filter_conditions: Any = [
        FieldCondition(key="_user_id", match=MatchValue(value=_user_id)),
        FieldCondition(key="source", match=MatchValue(value=args.source)),
    ]

    client.delete(
        collection_name=args.collection,
        points_selector=Filter(must=filter_conditions),
    )
    print(f"Deleted all chunks from source '{args.source}' for user {_user_id}")


def main():
    parser = argparse.ArgumentParser(description="HuRI RAG Ingestion Tool")
    parser.add_argument("--user-id", type=str, default=None)
    parser.add_argument("--collection", type=str, default="documents")
    parser.add_argument("--qdrant-url", type=str, default="http://localhost:6333")
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        default=False,
        help="Disable SSL certificate verification (needed for self-signed LAN certs).",
    )
    parser.add_argument("--embedding-model", type=str, default="BAAI/bge-large-en-v1.5")
    parser.add_argument(
        "--embedding-url",
        type=str,
        default="",
        help=(
            "OpenAI-compatible embedding endpoint (e.g. llama.cpp at "
            "http://localhost:8080). When set, embeddings are computed remotely "
            "instead of with a local SentenceTransformer. Requires --chunking fixed."
        ),
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Target chunk size in words (fixed mode)",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=50,
        help="Overlap between chunks in words (fixed mode)",
    )
    parser.add_argument(
        "--chunking",
        type=str,
        default="fixed",
        choices=["semantic", "fixed"],
        help="Chunking strategy: 'semantic' (default) or 'fixed'",
    )
    parser.add_argument(
        "--semantic-strategy",
        type=str,
        default="percentile",
        choices=["percentile", "threshold", "stddev"],
        help="Semantic chunking strategy (default: percentile)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_pdf = subparsers.add_parser("pdf", help="Ingest PDF files")
    p_pdf.add_argument("files", nargs="+", help="PDF files or directories")

    p_text = subparsers.add_parser("text", help="Ingest text files (.txt, .md)")
    p_text.add_argument("files", nargs="+", help="Text files")

    p_write = subparsers.add_parser("write", help="Write text interactively")
    p_write.add_argument("--title", type=str, default=None, help="Title/source name")

    p_profile = subparsers.add_parser(
        "profile", help="Store always-on profile facts (name, etc.)"
    )
    p_profile.add_argument("--name", type=str, default=None, help="User's name")
    p_profile.add_argument(
        "--fact",
        action="append",
        help="A fact about the user, e.g. --fact 'Likes cheese' (repeatable)",
    )

    subparsers.add_parser("list", help="List ingested documents")

    p_delete = subparsers.add_parser("delete", help="Delete documents by source")
    p_delete.add_argument(
        "--source", type=str, required=True, help="Source name to delete"
    )

    args = parser.parse_args()

    if args.embedding_url and args.chunking == "semantic":
        parser.error(
            "--chunking semantic needs a local SentenceTransformer model and "
            "cannot run over --embedding-url. Use --chunking fixed."
        )

    _user_id = get_user_id(args.user_id)
    print(f"User: {_user_id}")

    verify_ssl = not args.no_verify_ssl
    try:
        from .qdrant_utils import make_qdrant_client
    except ImportError:
        from qdrant_utils import make_qdrant_client  # type: ignore[no-redef]
    client = make_qdrant_client(args.qdrant_url, verify_ssl)

    # Lazy-load the model only if the command needs embeddings.
    # Commands that don't need it: list, delete, profile (doesn't use embeddings).
    model: RemoteEmbedder | SentenceTransformer | None
    needs_embeddings = args.command in ("pdf", "text", "write", "profile")

    if needs_embeddings:
        if args.embedding_url:
            print(
                f"Embedding remotely via {args.embedding_url} "
                f"(model={args.embedding_model})"
            )
            model = RemoteEmbedder(args.embedding_url, args.embedding_model)
        else:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(args.embedding_model)
    else:
        model = None

    commands = {
        "pdf": cmd_pdf,
        "text": cmd_text,
        "write": cmd_write,
        "profile": cmd_profile,
        "list": cmd_list,
        "delete": cmd_delete,
    }
    commands[args.command](args, client, model, _user_id)


if __name__ == "__main__":
    """
    Ingestion tool for HuRI RAG.

    Usage:
        # Ingest a PDF
        python ingestion.py pdf report.pdf

        # Ingest multiple PDFs
        python ingestion.py pdf doc1.pdf doc2.pdf doc3.pdf

        # Ingest a whole folder of PDFs
        # TODO: To verify and to add the support of hole paths
        python ingestion.py pdf ./my_documents/

        # Write text interactively (type, then Ctrl+D to save)
        python ingestion.py write --title "My meeting notes"

        # Ingest a text file
        python ingestion.py text notes.txt story.md

        # Specify a user ID (otherwise reads from ~/.huri_user_id)
        python ingestion.py --user-id "abc-123" pdf report.pdf

        # Use a different collection
        python ingestion.py --collection "my_docs" pdf report.pdf

        # Use a different ingestion strategy
        python src/modules/rag/ingestion.py \
--chunking semantic --semantic-strategy threshold pdf "EN.pdf"

    """
    main()
