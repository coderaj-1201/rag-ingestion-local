"""
test_pipeline_local.py
======================
Test the full parsing → embedding → search pipeline with a local file.
No SharePoint needed — just drop a file in test_docs/ and run this.

Usage:
  mkdir test_docs
  # copy a PDF/DOCX/XLSX/PPTX into test_docs/
  cd ingestion-pipeline
  python scripts/test_pipeline_local.py --file test_docs/your_file.pdf --domain hr

This script:
  1. Parses the file using the appropriate parser
  2. Prints the chunks (content, type, heading, page)
  3. Embeds and uploads to AI Search
  4. Runs a test query against the index
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from shared.logging_config import configure_logging
configure_logging()

from processors.dispatcher import parse_document
from shared.azure_clients import get_openai_client, get_search_client
from shared.config import settings
from shared.models import RawChunk


def print_chunk_summary(chunks: list[RawChunk]):
    print(f"\n{'─'*60}")
    print(f"Total chunks: {len(chunks)}")
    parents  = [c for c in chunks if not c.parent_id]
    children = [c for c in chunks if c.parent_id]
    tables   = [c for c in chunks if c.chunk_type == "table"]
    print(f"  Parents  : {len(parents)}")
    print(f"  Children : {len(children)}")
    print(f"  Tables   : {len(tables)}")
    print(f"{'─'*60}")
    for i, c in enumerate(chunks[:20]):   # print first 20
        print(f"\n[{i+1}] type={c.chunk_type} page={c.page_number} parent={'YES' if not c.parent_id else 'child'}")
        print(f"  heading : {c.section_heading[:60]}")
        print(f"  subhead : {c.section_subheading[:60]}")
        print(f"  content : {c.content[:150]}...")
        if c.table_raw:
            print(f"  table   : {c.table_raw[:100]}...")
    if len(chunks) > 20:
        print(f"\n  ... and {len(chunks)-20} more chunks")


async def embed_and_upload(chunks: list[RawChunk], doc_name: str):
    print(f"\n▶ Embedding {len(chunks)} chunks...")
    oai    = get_openai_client()
    search = get_search_client()

    # Only embed children
    children = [c for c in chunks if c.parent_id]
    parents  = [c for c in chunks if not c.parent_id]

    docs = []

    # Parents — no vector
    for p in parents:
        doc = p.to_search_doc()
        doc["content_vector"] = []
        docs.append(doc)

    # Children — embed in batches
    BATCH = 16
    embedded_children = []
    for i in range(0, len(children), BATCH):
        batch = children[i:i+BATCH]
        resp  = oai.embeddings.create(
            input=[c.content for c in batch],
            model=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )
        for chunk, emb in zip(batch, resp.data):
            d = chunk.to_search_doc()
            d["content_vector"] = emb.embedding
            docs.append(d)
        print(f"  Embedded batch {i}–{i+len(batch)}")

    # Upload all
    results = search.upload_documents(docs)
    ok  = sum(1 for r in results if r.succeeded)
    bad = sum(1 for r in results if not r.succeeded)
    print(f"  ✅ Uploaded: {ok} | ❌ Failed: {bad}")


async def test_query(query: str, domain: str):
    print(f"\n▶ Testing query: '{query}' domain={domain}")
    from tools.hybrid_search_tool import hybrid_search
    docs = hybrid_search(query, domain, top_k=3)
    if not docs:
        print("  ❌ No results")
        return
    for i, d in enumerate(docs):
        print(f"\n  Result {i+1} (score={d.score:.3f})")
        print(f"    type    : {d.chunk_type}")
        print(f"    page    : {d.page_number}")
        print(f"    heading : {d.section_heading}")
        print(f"    content : {d.content[:200]}...")
        if d.table_raw:
            print(f"    table   : {d.table_raw[:150]}...")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",   required=True, help="Path to local file")
    parser.add_argument("--domain", default="hr",  help="hr | legal | it")
    parser.add_argument("--query",  default="",    help="Optional test query after upload")
    parser.add_argument("--parse-only", action="store_true", help="Parse only, skip upload")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"File not found: {args.file}")
        sys.exit(1)

    doc_name = os.path.basename(args.file)
    with open(args.file, "rb") as f:
        file_bytes = f.read()

    print(f"\n▶ Parsing {doc_name} ({len(file_bytes):,} bytes) as domain={args.domain}...")
    chunks = parse_document(
        file_bytes=file_bytes,
        doc_name=doc_name,
        doc_url=f"file://local/{doc_name}",
        domain=args.domain,
        blob_path=f"{args.domain}/{doc_name}",
    )

    print_chunk_summary(chunks)

    if args.parse_only:
        return

    await embed_and_upload(chunks, doc_name)

    query = args.query or f"What is this document about?"
    await test_query(query, args.domain)


if __name__ == "__main__":
    asyncio.run(main())
