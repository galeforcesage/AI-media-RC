"""
semantic_index.py
Pre-built semantic index of recording metadata and transcripts.

Uses sentence-transformers for embeddings and ChromaDB for vector storage.
Provides sub-second retrieval of relevant context for any user query,
eliminating the need for slow MCP tool calls in most cases.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import hashlib

import aiohttp

from utils.logger import get_logger
from services.gpu import release_cuda_memory

logger = get_logger(__name__)

# Lightweight model — ~80MB, fast on CPU, good quality
DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
# Run embeddings on CPU by default. This model is small and CPU-fast, but
# sentence-transformers auto-selects CUDA when available, which pins a ~500MB
# CUDA context in the orchestrator process for the entire uptime of the
# service. The GPU is far better spent on Whisper/Ollama.
DEFAULT_EMBED_DEVICE = "cpu"
COLLECTION_NAME = "media_index"
CHROMA_DIR = os.path.join(os.path.expanduser("~"), ".llm-remote", "chroma_db")
CHANNELS_DVR_URL = "http://localhost:8089"

# How often to refresh the index (seconds)
REFRESH_INTERVAL = 1800  # 30 minutes (incremental, so lightweight)

# CPU throttling: limit threads used by sentence-transformers / PyTorch
# so we don't starve DVR services, Ollama, or Whisper.
# Use ~12% of cores (minimum 1).
_MAX_ENCODE_THREADS = max(1, (os.cpu_count() or 4) // 8)
DEFAULT_ENCODE_BATCH_SIZE = 64  # smaller batches = less CPU spikes
_STARTUP_DELAY = 30  # seconds to wait before first index build


class SemanticIndex:
    """
    Maintains a vector index of all recording metadata from Channels DVR
    and SageTV. Provides fast semantic search for pre-populating LLM context.
    """

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator
        self._collection = None
        self._embedder = None
        self._ready = False
        self._last_refresh = 0.0
        self._doc_count = 0
        self._refresh_task: Optional[asyncio.Task] = None
        self._doc_hashes: Dict[str, str] = {}  # id -> text hash for incremental skip

        # Pull tunables from config
        sem_cfg = getattr(orchestrator, "config", {}).get("semantic", {})
        self._embed_model = sem_cfg.get("embed_model", DEFAULT_EMBED_MODEL)
        self._embed_device = sem_cfg.get("embed_device", DEFAULT_EMBED_DEVICE)
        self._encode_batch_size = sem_cfg.get("encode_batch_size", DEFAULT_ENCODE_BATCH_SIZE)

        # Scale max_chars for format_context with LLM context window
        llm_ctx = getattr(orchestrator, "config", {}).get("llm", {}).get("num_ctx", 4096)
        self._default_max_chars = max(400, llm_ctx // 5)

    async def start(self) -> None:
        """Initialize the embedding model and ChromaDB, then build/refresh the index."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._init_sync)
            self._ready = True
            logger.info("Semantic index ready (%d documents)", self._doc_count)
            # Start background refresh loop
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        except Exception as exc:
            logger.warning("Semantic index unavailable: %s", exc)
            self._ready = False

    def _init_sync(self) -> None:
        """Synchronous init — loads model and ChromaDB (called in executor)."""
        import torch
        # Limit CPU threads so embedding doesn't starve the system
        torch.set_num_threads(_MAX_ENCODE_THREADS)
        os.environ["OMP_NUM_THREADS"] = str(_MAX_ENCODE_THREADS)
        os.environ["MKL_NUM_THREADS"] = str(_MAX_ENCODE_THREADS)

        from sentence_transformers import SentenceTransformer
        import chromadb

        logger.info("Loading embedding model: %s (device=%s, max %d threads)",
                    self._embed_model, self._embed_device, _MAX_ENCODE_THREADS)
        self._embedder = SentenceTransformer(self._embed_model, device=self._embed_device)

        os.makedirs(CHROMA_DIR, exist_ok=True)
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        self._collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._doc_count = self._collection.count()

    async def stop(self) -> None:
        """Cancel background refresh and release the embedding model."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._ready = False
        if self._embedder is not None:
            self._embedder = None
            release_cuda_memory()

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, query: str, n_results: int = 10, systems: list[str] | None = None) -> List[Dict[str, Any]]:
        """
        Semantic search — find recordings most relevant to the query.
        Returns list of dicts with metadata + relevance score.
        Optionally filter by source systems (e.g. ["channelsdvr"]).
        Runs in <100ms typically.
        """
        if not self._ready or not self._collection:
            return []

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, self._search_sync, query, n_results, systems)
        return results

    def _search_sync(self, query: str, n_results: int, systems: list[str] | None = None) -> List[Dict[str, Any]]:
        """Synchronous search in ChromaDB."""
        if self._collection.count() == 0:
            return []

        n_results = min(n_results, self._collection.count())
        embedding = self._embedder.encode(query).tolist()

        # Filter by active systems if not all are selected
        where_filter = None
        if systems and set(systems) != {"sagetv", "channelsdvr"}:
            if len(systems) == 1:
                where_filter = {"source": systems[0]}
            else:
                where_filter = {"source": {"$in": systems}}

        results = self._collection.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
            where=where_filter,
        )

        hits = []
        for i in range(len(results["ids"][0])):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            hits.append({
                "id": results["ids"][0][i],
                "text": results["documents"][0][i] if results["documents"] else "",
                "score": 1.0 - results["distances"][0][i],  # cosine distance → similarity
                **meta,
            })
        return hits

    def format_context(self, hits: List[Dict[str, Any]], max_chars: int | None = None) -> str:
        """Format search results as concise context for the LLM prompt."""
        if max_chars is None:
            max_chars = self._default_max_chars
        if not hits:
            return ""

        lines = []
        total = 0
        for h in hits:
            line = h.get("text", "")
            if total + len(line) > max_chars:
                break
            lines.append(f"- {line}")
            total += len(line)

        if not lines:
            return ""
        return "RELEVANT RECORDINGS FROM INDEX:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    async def refresh(self) -> int:
        """Pull fresh metadata from DVR APIs and update the index. Returns doc count."""
        if not self._ready:
            return 0

        logger.info("Refreshing semantic index...")
        t0 = time.time()

        docs: List[Dict[str, str]] = []

        # Fetch from Channels DVR (fast, direct HTTP)
        channels_docs = await self._fetch_channels_recordings()
        docs.extend(channels_docs)

        # Fetch from SageTV via MCP
        sagetv_docs = await self._fetch_sagetv_recordings()
        docs.extend(sagetv_docs)

        if not docs:
            logger.warning("No recordings fetched for indexing")
            return self._doc_count

        # Update ChromaDB in executor (CPU-bound embedding)
        loop = asyncio.get_event_loop()
        count = await loop.run_in_executor(None, self._upsert_docs, docs)

        elapsed = time.time() - t0
        self._doc_count = count
        self._last_refresh = time.time()
        logger.info("Semantic index refreshed: %d docs in %.1fs", count, elapsed)
        return count

    async def _fetch_channels_recordings(self) -> List[Dict[str, str]]:
        """Fetch all recordings from Channels DVR REST API."""
        docs = []
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{CHANNELS_DVR_URL}/dvr/files") as resp:
                    if resp.status != 200:
                        logger.warning("Channels DVR /dvr/files returned %d", resp.status)
                        return docs
                    recordings = await resp.json()

            for rec in recordings:
                doc = self._channels_rec_to_doc(rec)
                if doc:
                    docs.append(doc)
            logger.info("Fetched %d recordings from Channels DVR", len(docs))
        except Exception as exc:
            logger.warning("Failed to fetch Channels DVR recordings: %s", exc)
        return docs

    def _channels_rec_to_doc(self, rec: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Convert a Channels DVR recording to an indexable document."""
        airing = rec.get("Airing", {})

        title = airing.get("Title") or rec.get("Title") or rec.get("title") or ""
        if not title:
            return None

        episode = airing.get("EpisodeTitle") or rec.get("EpisodeTitle") or ""
        season = airing.get("SeasonNumber") or rec.get("SeasonNumber") or ""
        ep_num = airing.get("EpisodeNumber") or rec.get("EpisodeNumber") or ""
        channel = airing.get("Channel") or rec.get("Channel") or ""
        date = airing.get("OriginalDate") or rec.get("CreatedAt") or ""
        summary = airing.get("Summary") or airing.get("FullSummary") or rec.get("Summary") or ""
        duration = rec.get("Duration") or airing.get("Duration") or 0
        file_id = str(rec.get("ID") or rec.get("id") or "")
        path = rec.get("Path") or ""
        categories = airing.get("Categories") or []

        # Build searchable text
        parts = [f'"{title}"']
        if episode:
            parts.append(f'episode "{episode}"')
        if season and ep_num:
            parts.append(f"S{season:>02s}E{ep_num:>02s}" if isinstance(season, str) else f"S{season:02d}E{ep_num:02d}")
        if channel:
            parts.append(f"on {channel}")
        if date:
            parts.append(f"recorded {str(date)[:10]}")
        if summary:
            parts.append(f"— {summary[:150]}")
        if categories:
            parts.append(f"[{', '.join(str(c) for c in categories[:5])}]")

        text = " ".join(parts)

        # Metadata stored alongside for retrieval
        meta = {
            "source": "channelsdvr",
            "title": title[:200],
            "episode": episode[:200],
            "channel": str(channel)[:100],
            "date": str(date)[:20],
        }
        if file_id:
            meta["file_id"] = file_id[:50]
        if path:
            meta["path"] = path[:300]
        if duration:
            mins = int(float(duration)) // 60 if duration else 0
            meta["duration_min"] = str(mins)

        return {"id": f"channels_{file_id or hash(text)}", "text": text, "meta": meta}

    async def _fetch_sagetv_recordings(self) -> List[Dict[str, str]]:
        """Fetch recordings from SageTV via MCP client.

        The full library is fetched in pages because a single 5000-item
        response exceeds the MCP client's 1 MB line-read buffer
        ("Separator is not found, and chunk exceed the limit").
        """
        docs = []
        try:
            from services.mcp_client import MCPClient
            client = MCPClient(host="127.0.0.1", port=8766, name="sagetv")
            try:
                page = 400
                offset = 0
                fetched = 0
                while offset < 40000:  # safety cap
                    result = await client.call_tool(
                        "sagetv_get_recordings", {"limit": page, "offset": offset}
                    )
                    data = result.get("data", result) if isinstance(result, dict) else result
                    recordings = data.get("recordings", data) if isinstance(data, dict) else data
                    if not isinstance(recordings, list) or not recordings:
                        break
                    for rec in recordings:
                        doc = self._sagetv_rec_to_doc(rec)
                        if doc:
                            docs.append(doc)
                    fetched += len(recordings)
                    if len(recordings) < page:
                        break
                    offset += page
            finally:
                await client.close()

            logger.info("Fetched %d recordings from SageTV", len(docs))
        except Exception as exc:
            logger.warning("Failed to fetch SageTV recordings: %s", exc)
        return docs

    def _sagetv_rec_to_doc(self, rec: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Convert a SageTV recording to an indexable document.

        ``sagetv_get_recordings`` returns the *slimmed* recording shape
        (see mcp-sagetv ``_slim_recording``): flat keys like ``title``,
        ``episode_title``, ``season_episode``, ``channel``, ``record_date``
        (epoch seconds), ``description`` and ``genres`` — not the raw nested
        Airing/Show MediaFile. Parse that shape here.
        """
        if not isinstance(rec, dict):
            return None

        title = rec.get("title") or ""
        if not title:
            return None

        episode = rec.get("episode_title") or ""
        se = rec.get("season_episode") or ""
        channel = rec.get("channel") or ""
        description = rec.get("description") or ""
        genres = rec.get("genres") or ""
        media_id = str(rec.get("id") or "")
        record_date = rec.get("record_date")  # epoch seconds

        # Build searchable text
        parts = [f'"{title}"']
        if episode:
            parts.append(f'episode "{episode}"')
        if se:
            parts.append(se)
        if channel:
            parts.append(f"on {channel}")
        if record_date:
            try:
                from datetime import datetime
                parts.append(f"recorded {datetime.fromtimestamp(int(record_date)).strftime('%Y-%m-%d')}")
            except (ValueError, TypeError, OSError):
                pass
        if description:
            parts.append(f"— {description[:150]}")
        if genres:
            gtxt = genres if isinstance(genres, str) else ", ".join(str(g) for g in list(genres)[:5])
            if gtxt:
                parts.append(f"[{gtxt}]")

        text = " ".join(parts)
        meta = {
            "source": "sagetv",
            "title": title[:200],
            "episode": episode[:200],
            "channel": str(channel)[:100],
        }
        if se:
            meta["season_episode"] = str(se)[:16]
        if record_date:
            meta["date"] = str(record_date)[:20]
        if media_id:
            meta["media_id"] = media_id[:50]

        return {"id": f"sagetv_{media_id or hash(text)}", "text": text, "meta": meta}

    def _upsert_docs(self, docs: List[Dict[str, str]]) -> int:
        """Embed and upsert documents into ChromaDB (sync, runs in executor).

        Incremental: skips docs whose text hasn't changed since last refresh.
        Reconciliation: removes stale entries no longer present in source.
        """
        if not docs:
            return self._collection.count()

        import time

        # --- Incremental: filter to only new/changed docs ---
        new_hashes: Dict[str, str] = {}
        changed_docs = []
        for d in docs:
            text_hash = hashlib.md5(d["text"].encode()).hexdigest()
            new_hashes[d["id"]] = text_hash
            if self._doc_hashes.get(d["id"]) != text_hash:
                changed_docs.append(d)

        if changed_docs:
            logger.info("Incremental index: %d changed of %d total docs", len(changed_docs), len(docs))
            batch_size = self._encode_batch_size
            for i in range(0, len(changed_docs), batch_size):
                batch = changed_docs[i:i + batch_size]
                ids = [d["id"] for d in batch]
                texts = [d["text"] for d in batch]
                metas = [d.get("meta", {}) for d in batch]

                embeddings = self._embedder.encode(texts).tolist()

                self._collection.upsert(
                    ids=ids,
                    documents=texts,
                    embeddings=embeddings,
                    metadatas=metas,
                )
                # Yield CPU between batches so DVR services stay responsive
                time.sleep(0.5)
        else:
            logger.info("Incremental index: no changes detected, skipping embedding")

        # --- Reconciliation: remove stale entries ---
        fresh_ids = set(new_hashes.keys())
        stale_ids = [doc_id for doc_id in self._doc_hashes if doc_id not in fresh_ids]
        if stale_ids:
            logger.info("Removing %d stale entries from index", len(stale_ids))
            # ChromaDB delete accepts max ~5000 ids at a time
            for i in range(0, len(stale_ids), 5000):
                self._collection.delete(ids=stale_ids[i:i + 5000])

        # Update cached hashes
        self._doc_hashes = new_hashes

        return self._collection.count()

    async def _refresh_loop(self) -> None:
        """Background task: refresh index periodically."""
        # Delay initial refresh so the server finishes startup first
        logger.info("Semantic index: delaying first refresh by %ds", _STARTUP_DELAY)
        await asyncio.sleep(_STARTUP_DELAY)

        try:
            await self.refresh()
        except Exception as exc:
            logger.warning("Initial index refresh failed: %s", exc)

        while True:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
                await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Index refresh error: %s", exc)
