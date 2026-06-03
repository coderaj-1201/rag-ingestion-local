"""
test_pipeline_local.py
======================
Ingest local files (single file OR entire folder) into AI Search.
No SharePoint, no Docker, no agents running — runs end-to-end in one process.

Usage — single file:
  python scripts/test_pipeline_local.py --file "C:\\Docs\\Leave Policy.pdf" --domain hr

Usage — entire folder:
  python scripts/test_pipeline_local.py --folder "C:\\Docs\\HR Policies" --domain hr

Usage — parse only (no upload, just see chunks):
  python scripts/test_pipeline_local.py --file "C:\\Docs\\Leave Policy.pdf" --domain hr --parse-only

Usage — with test query after upload:
  python scripts/test_pipeline_local.py --folder "C:\\Docs\\HR" --domain hr --query "annual leave policy"

Supported file types: .pdf .docx .doc .xlsx .xls .pptx .ppt
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.logging_config import configure_logging
configure_logging()

from processors.dispatcher import detect_file_type, parse_document
from shared.azure_clients import get_openai_client, get_search_client
from shared.config import settings
from shared.models import RawChunk

_SUPPORTED = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def collect_files(path: str) -> list[Path]:
    """Return list of supported files from a file path or folder path."""
    p = Path(path)
    if not p.exists():
        print(f"❌ Path not found: {path}")
        sys.exit(1)
    if p.is_file():
        if p.suffix.lower() not in _SUPPORTED:
            print(f"❌ Unsupported file type: {p.suffix}. Supported: {_SUPPORTED}")
            sys.exit(1)
        return [p]
    # Folder — collect recursively
    files = [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _SUPPORTED]
    if not files:
        print(f"❌ No supported files found in folder: {path}")
        sys.exit(1)
    files.sort()
    return files


def print_chunk_summary(chunks: list[RawChunk], doc_name: str):
    parents  = [c for c in chunks if not c.parent_id]
    children = [c for c in chunks if c.parent_id]
    tables   = [c for c in chunks if c.chunk_type == "table"]
    print(f"\n  📄 {doc_name}")
    print(f"     Chunks : {len(chunks)} total  ({len(parents)} parents, {len(children)} children, {len(tables)} tables)")
    # Print first 5 chunks as preview
    for c in chunks[:5]:
        tag = f"[{c.chunk_type}] p.{c.page_number}"
        hdg = f" § {c.section_heading[:40]}" if c.section_heading else ""
        print(f"     {tag}{hdg}")
        print(f"       {c.content[:120].strip()}...")
        if c.table_raw:
            print(f"       TABLE: {c.table_raw[:80].strip()}...")
    if len(chunks) > 5:
        print(f"     ... and {len(chunks) - 5} more chunks")


def embed_and_upload(chunks: list[RawChunk], doc_name: str) -> tuple[int, int]:
    """Embed children, upload all to AI Search. Returns (ok, failed)."""
    oai    = get_openai_client()
    search = get_search_client()

    children = [c for c in chunks if c.parent_id]
    parents  = [c for c in chunks if not c.parent_id]

    docs = []

    # Parents — stored without vector (fetched by parent_id at query time)
    for p in parents:
        d = p.to_search_doc()
        d["content_vector"] = []
        docs.append(d)

    # Children — embed in batches of 16
    BATCH = 16
    for i in range(0, len(children), BATCH):
        batch = children[i : i + BATCH]
        resp  = oai.embeddings.create(
            input=[c.content for c in batch],
            model=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )
        for chunk, emb in zip(batch, resp.data):
            d = chunk.to_search_doc()
            d["content_vector"] = emb.embedding
            docs.append(d)

    results = search.upload_documents(docs)
    ok  = sum(1 for r in results if r.succeeded)
    bad = sum(1 for r in results if not r.succeeded)
    return ok, bad


def run_test_query(query: str, domain: str):
    """Run a search query and print results."""
    # Import here so parse-only mode doesn't need search client
    from tools.hybrid_search_tool import hybrid_search
    print(f"\n🔍 Query: \"{query}\"  domain={domain}")
    results = hybrid_search(query, domain, top_k=3)
    if not results:
        print("  No results found.")
        return
    for i, d in enumerate(results):
        print(f"\n  [{i+1}] score={d.score:.3f}  type={d.chunk_type}  page={d.page_number}")
        print(f"       doc     : {d.doc_name}")
        print(f"       heading : {d.section_heading}")
        print(f"       content : {d.content[:200]}...")
        if d.table_raw:
            print(f"       table   : {d.table_raw[:150]}...")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Ingest local files into AI Search (no SharePoint needed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single PDF
  python scripts/test_pipeline_local.py --file "C:\\Docs\\Leave Policy.pdf" --domain hr

  # Entire folder
  python scripts/test_pipeline_local.py --folder "C:\\Docs\\HR Policies" --domain hr

  # Parse and preview chunks only (no upload)
  python scripts/test_pipeline_local.py --file policy.pdf --domain hr --parse-only

  # Upload then immediately test a query
  python scripts/test_pipeline_local.py --folder "C:\\Docs\\IT" --domain it --query "how to reset VPN"
        """,
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--file",   help="Path to a single file (.pdf .docx .xlsx .pptx)")
    group.add_argument("--folder", help="Path to a folder — all supported files ingested recursively")

    ap.add_argument("--domain",     default="hr",  choices=["hr", "legal", "it"], help="Domain tag for all files (default: hr)")
    ap.add_argument("--query",      default="",    help="Test query to run after upload")
    ap.add_argument("--parse-only", action="store_true", help="Parse and preview chunks — skip upload to AI Search")

    args = ap.parse_args()

    source_path = args.file or args.folder
    files = collect_files(source_path)

    print(f"\n{'═'*60}")
    print(f"  RAG Local Ingestion")
    print(f"  Source : {source_path}")
    print(f"  Files  : {len(files)}")
    print(f"  Domain : {args.domain}")
    print(f"  Mode   : {'parse-only' if args.parse_only else 'parse + embed + upload'}")
    print(f"{'═'*60}")

    total_chunks   = 0
    total_uploaded = 0
    total_failed   = 0

    for file_path in files:
        doc_name = file_path.name
        print(f"\n▶ Processing: {doc_name}")

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        try:
            chunks = parse_document(
                file_bytes = file_bytes,
                doc_name   = doc_name,
                doc_url    = file_path.as_uri(),   # file:///C:/Docs/... — stored as metadata
                domain     = args.domain,
                blob_path  = f"{args.domain}/{doc_name}",
            )
        except ValueError as e:
            print(f"  ⚠️  Skipped: {e}")
            continue
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            continue

        print_chunk_summary(chunks, doc_name)
        total_chunks += len(chunks)

        if args.parse_only:
            continue

        ok, bad = embed_and_upload(chunks, doc_name)
        total_uploaded += ok
        total_failed   += bad
        status = "✅" if bad == 0 else "⚠️"
        print(f"  {status} Uploaded {ok} chunks" + (f" | {bad} failed" if bad else ""))

    # Summary
    print(f"\n{'═'*60}")
    print(f"  Done.")
    print(f"  Files processed : {len(files)}")
    print(f"  Total chunks    : {total_chunks}")
    if not args.parse_only:
        print(f"  Uploaded to AI Search : {total_uploaded}")
        if total_failed:
            print(f"  Failed uploads        : {total_failed}")

    # Optional test query
    if args.query and not args.parse_only:
        run_test_query(args.query, args.domain)
    elif not args.parse_only and not args.query:
        print(f"\n  Tip: add --query \"<your question>\" to test search immediately")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
