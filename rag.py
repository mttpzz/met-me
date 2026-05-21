"""Document RAG over the rag/docs/ folder using LlamaIndex + Chroma + Ollama.

Layout:
  rag/docs/             user-managed source files (txt, md, pdf, docx)
  rag/index/            Chroma persistence (managed by this module)
  rag/index/manifest.json  per-file fingerprint (modified, size, nodes)

Files in docs/ are read via SimpleDirectoryReader (pypdf for .pdf, docx2txt
for .docx). An explicit SentenceSplitter chunks each document into nodes of
RAG_CHUNK_SIZE tokens with RAG_CHUNK_OVERLAP overlap; nodes are embedded via
Ollama (RAG_EMBED_NUM_CTX context window) and stored in Chroma.

Incremental ingest: on every folder sync we compare disk vs manifest. New
files are embedded, modified files (modification time or size changed) get
their old nodes deleted from Chroma and re-embedded, deleted files get their
nodes purged. Unchanged files are skipped -- this is what keeps bot startup fast
after the first run. Force a full rebuild by deleting rag/index/.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import chromadb
from llama_index.core import (
    Settings,
    SimpleDirectoryReader,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

log = logging.getLogger("rag")

COLLECTION_NAME = "documents"
MANIFEST_NAME = "manifest.json"


class Rag:
    def __init__(
        self,
        rag_path: Path,
        ollama_host: str,
        embedding_model: str,
        chunk_size: int,
        chunk_overlap: int,
        embed_num_ctx: int,
    ) -> None:
        self.docs_path = rag_path / "docs"
        self.index_path = rag_path / "index"
        self.docs_path.mkdir(parents=True, exist_ok=True)
        self.index_path.mkdir(parents=True, exist_ok=True)

        self._chroma = chromadb.PersistentClient(path=str(self.index_path))
        self._collection = self._chroma.get_or_create_collection(COLLECTION_NAME)
        self._vector_store = ChromaVectorStore(chroma_collection=self._collection)
        self._storage = StorageContext.from_defaults(vector_store=self._vector_store)

        # Global LlamaIndex settings: embed via Ollama, no synthesis LLM (we only
        # use the retriever — the bot's own provider handles generation).
        Settings.embed_model = OllamaEmbedding(
            model_name=embedding_model,
            base_url=ollama_host,
            ollama_additional_kwargs={"num_ctx": embed_num_ctx},
        )
        Settings.llm = None

        self._splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        self._index = VectorStoreIndex.from_vector_store(
            vector_store=self._vector_store,
            storage_context=self._storage,
        )

        self._manifest_path = self.index_path / MANIFEST_NAME
        self._manifest: dict[str, dict] = self._load_manifest()

    # ---- Manifest ----------------------------------------------------------

    def _load_manifest(self) -> dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("RAG: corrupt manifest, starting fresh")
            return {}

    def _save_manifest(self) -> None:
        self._manifest_path.write_text(
            json.dumps(self._manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _fingerprint(p: Path) -> dict:
        # ISO 8601 (seconds precision) is human-readable and still gives exact
        # equality comparison for change detection.
        st = p.stat()
        return {
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size": st.st_size,
        }

    # ---- Sync core ---------------------------------------------------------

    def _wipe_sync(self) -> None:
        ids = self._collection.get().get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        self._manifest = {}
        if self._manifest_path.exists():
            self._manifest_path.unlink()

    def _delete_file_nodes(self, file_name: str) -> None:
        # Chroma supports delete-by-metadata. SimpleDirectoryReader stamps each
        # node with file_name=<basename>, so this targets exactly that file.
        try:
            self._collection.delete(where={"file_name": file_name})
        except Exception:
            log.exception("RAG: failed to delete nodes for %s", file_name)

    def _ingest_folder_sync(self) -> tuple[int, int]:
        """Sync docs/ against the manifest: ingest new/modified, purge removed.

        Returns (files_on_disk, new_nodes_indexed). new_nodes counts only
        chunks added in this run; unchanged files contribute zero.
        """
        # Safety: empty manifest but a populated collection means leftover data
        # from a prior version. Wipe so we never accumulate duplicates.
        if not self._manifest and (self._collection.get().get("ids") or []):
            log.info("RAG: empty manifest but non-empty collection, wiping for consistency")
            self._wipe_sync()

        files_on_disk = {p.name: p for p in self.docs_path.iterdir() if p.is_file()}

        # Purge files that disappeared from disk.
        removed = set(self._manifest) - set(files_on_disk)
        for name in removed:
            self._delete_file_nodes(name)
            self._manifest.pop(name, None)
            log.info("RAG removed: file=%s", name)

        new_nodes = 0
        unchanged = 0
        for name, p in files_on_disk.items():
            fp = self._fingerprint(p)
            prev = self._manifest.get(name)
            if prev and prev.get("modified") == fp["modified"] and prev.get("size") == fp["size"]:
                unchanged += 1
                continue
            if prev:
                self._delete_file_nodes(name)
                log.info("RAG modified: file=%s", name)
            new_nodes += self._ingest_file_sync(p, save=False)

        self._save_manifest()
        log.info(
            "RAG ingest_folder done: files=%d unchanged=%d processed=%d new_nodes=%d removed=%d",
            len(files_on_disk), unchanged, len(files_on_disk) - unchanged, new_nodes, len(removed),
        )
        return (len(files_on_disk), new_nodes)

    def _ingest_file_sync(self, file_path: Path, save: bool = True) -> int:
        """Append (or replace) a single file in the index. Returns nodes added.

        `save=False` is used by the folder sync to batch manifest writes.
        """
        if file_path.name in self._manifest:
            # Replace path: drop the old chunks before adding the new ones.
            self._delete_file_nodes(file_path.name)
        docs = SimpleDirectoryReader(input_files=[str(file_path)]).load_data()
        nodes = self._splitter.get_nodes_from_documents(docs)
        self._index.insert_nodes(nodes)
        self._manifest[file_path.name] = {
            **self._fingerprint(file_path),
            "nodes": len(nodes),
        }
        if save:
            self._save_manifest()
        log.info("RAG ingest: file=%s docs=%d nodes=%d", file_path.name, len(docs), len(nodes))
        return len(nodes)

    def _query_sync(self, text: str, k: int) -> list[str]:
        retriever = self._index.as_retriever(similarity_top_k=k)
        nodes = retriever.retrieve(text)
        return [n.get_content() for n in nodes]

    # ---- Async API ---------------------------------------------------------

    async def ingest_folder(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._ingest_folder_sync)

    async def ingest_file(self, file_path: Path) -> int:
        return await asyncio.to_thread(self._ingest_file_sync, file_path)

    async def query(self, text: str, k: int) -> list[str]:
        return await asyncio.to_thread(self._query_sync, text, k)
