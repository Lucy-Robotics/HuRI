"""
Semantic Chunking for RAG.

Three strategies:
  1. percentile   - cut where similarity is below the Nth percentile (default)
  2. threshold    - cut where similarity drops below a fixed value
  3. stddev       - cut where similarity is more than N std devs below the mean

Usage:
    from semantic_chunker import SemanticChunker

    chunker = SemanticChunker(embedding_model)
    chunks = chunker.chunk(text)
"""

import re
from dataclasses import dataclass, field

import numpy as np
from sentence_transformers import SentenceTransformer


@dataclass
class Chunk:
    text: str
    sentences: list[str] = field(default_factory=list)
    start_idx: int = 0
    end_idx: int = 0


class SemanticChunker:
    def __init__(
        self,
        model: SentenceTransformer,
        strategy: str = "percentile",  # "percentile", "threshold", "stddev"
        percentile_cutoff: float = 25,  # for percentile strategy
        threshold_cutoff: float = 0.5,  # for threshold strategy
        stddev_cutoff: float = 1.0,  # for stddev strategy (N std devs below mean)
        min_chunk_size: int = 2,  # minimum sentences per chunk
        max_chunk_size: int = 50,  # maximum sentences per chunk
        buffer_size: int = 1,  # sentences to look around for context
    ):
        self.model = model
        self.strategy = strategy
        self.percentile_cutoff = percentile_cutoff
        self.threshold_cutoff = threshold_cutoff
        self.stddev_cutoff = stddev_cutoff
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        self.buffer_size = buffer_size

    def chunk(self, text: str) -> list[str]:
        """Main entry point. Returns list of chunk texts."""
        sentences = self._split_sentences(text)

        if len(sentences) <= self.min_chunk_size:
            return [text.strip()] if text.strip() else []

        combined = self._combine_with_buffer(sentences)
        embeddings = self.model.encode(combined, normalize_embeddings=True)
        similarities = self._calculate_similarities(np.asarray(embeddings))
        breakpoints = self._find_breakpoints(similarities)
        chunks = self._create_chunks(sentences, breakpoints)

        return chunks

    def chunk_detailed(self, text: str) -> list[Chunk]:
        """Returns detailed Chunk objects with metadata."""
        sentences = self._split_sentences(text)

        if len(sentences) <= self.min_chunk_size:
            return [
                Chunk(
                    text=text.strip(),
                    sentences=sentences,
                    start_idx=0,
                    end_idx=len(sentences),
                )
            ]

        combined = self._combine_with_buffer(sentences)
        embeddings = self.model.encode(combined, normalize_embeddings=True)
        similarities = self._calculate_similarities(np.asarray(embeddings))
        breakpoints = self._find_breakpoints(similarities)

        chunks = []
        start = 0
        for bp in breakpoints:
            end = bp + 1
            chunk_sentences = sentences[start:end]
            chunks.append(
                Chunk(
                    text=" ".join(chunk_sentences),
                    sentences=chunk_sentences,
                    start_idx=start,
                    end_idx=end,
                )
            )
            start = end

        if start < len(sentences):
            chunk_sentences = sentences[start:]
            chunks.append(
                Chunk(
                    text=" ".join(chunk_sentences),
                    sentences=chunk_sentences,
                    start_idx=start,
                    end_idx=len(sentences),
                )
            )

        return chunks

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences, respecting paragraph boundaries."""
        paragraphs = text.split("\n\n")
        sentences = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            parts = re.split(r"(?<=[.!?])\s+", para)
            for part in parts:
                part = part.strip()
                if part:
                    sentences.append(part)
        return sentences

    def _combine_with_buffer(self, sentences: list[str]) -> list[str]:
        """
        Combine each sentence with its neighbors for richer embeddings.
        Sentence at index i gets combined with sentences [i-buffer, i+buffer].
        This gives the embedding model more context to understand each sentence.
        """
        combined = []
        for i in range(len(sentences)):
            start = max(0, i - self.buffer_size)
            end = min(len(sentences), i + self.buffer_size + 1)
            window = " ".join(sentences[start:end])
            combined.append(window)
        return combined

    def _calculate_similarities(self, embeddings: np.ndarray) -> list[float]:
        """Calculate cosine similarity between consecutive sentence embeddings."""
        similarities = []
        for i in range(len(embeddings) - 1):
            sim = np.dot(embeddings[i], embeddings[i + 1])
            similarities.append(float(sim))
        return similarities

    def _find_breakpoints(self, similarities: list[float]) -> list[int]:
        """Find where to split based on the chosen strategy."""
        if not similarities:
            return []

        sims = np.array(similarities)

        if self.strategy == "percentile":
            cutoff = np.percentile(sims, self.percentile_cutoff)
            candidate_indices = [i for i, s in enumerate(similarities) if s < cutoff]

        elif self.strategy == "threshold":
            candidate_indices = [
                i for i, s in enumerate(similarities) if s < self.threshold_cutoff
            ]

        elif self.strategy == "stddev":
            mean = np.mean(sims)
            std = np.std(sims)
            cutoff = mean - (self.stddev_cutoff * std)
            candidate_indices = [i for i, s in enumerate(similarities) if s < cutoff]

        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

        breakpoints = self._enforce_chunk_sizes(
            candidate_indices, len(similarities) + 1
        )

        return breakpoints

    def _enforce_chunk_sizes(
        self, candidates: list[int], num_sentences: int
    ) -> list[int]:
        """Ensure chunks respect min and max size constraints."""
        if not candidates:
            breakpoints = []
            pos = self.max_chunk_size - 1
            while pos < num_sentences - 1:
                breakpoints.append(pos)
                pos += self.max_chunk_size
            return breakpoints

        breakpoints = []
        last_break = -1

        for candidate in sorted(candidates):
            chunk_size = candidate - last_break

            if chunk_size < self.min_chunk_size:
                continue

            if chunk_size > self.max_chunk_size:
                pos = last_break + self.max_chunk_size
                while pos < candidate:
                    breakpoints.append(pos)
                    last_break = pos
                    pos += self.max_chunk_size

            breakpoints.append(candidate)
            last_break = candidate

        remaining = num_sentences - 1 - last_break
        if remaining > self.max_chunk_size:
            pos = last_break + self.max_chunk_size
            while pos < num_sentences - 1:
                breakpoints.append(pos)
                pos += self.max_chunk_size

        return breakpoints

    def _create_chunks(self, sentences: list[str], breakpoints: list[int]) -> list[str]:
        """Group sentences into chunks based on breakpoints."""
        chunks = []
        start = 0

        for bp in breakpoints:
            end = bp + 1
            chunk_text = " ".join(sentences[start:end]).strip()
            if chunk_text:
                chunks.append(chunk_text)
            start = end

        if start < len(sentences):
            chunk_text = " ".join(sentences[start:]).strip()
            if chunk_text:
                chunks.append(chunk_text)

        return chunks


def create_chunker(
    model: SentenceTransformer | None = None,
    model_name: str = "BAAI/bge-large-en-v1.5",
    strategy: str = "percentile",
    **kwargs,
) -> SemanticChunker:
    """Create a chunker with defaults."""
    if model is None:
        model = SentenceTransformer(model_name)
    return SemanticChunker(model=model, strategy=strategy, **kwargs)
