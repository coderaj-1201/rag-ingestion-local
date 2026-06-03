"""
PDF Parser
==========

Strategy (no scanned docs):
  1. Azure Document Intelligence — prebuilt-layout model
     → detects paragraphs, tables, headings, page numbers natively
     → returns structured AnalyzeResult with bounding boxes
  2. Header/footer removal via bounding box y-position threshold
  3. Per-page light LLM pass (gpt-4o-mini / phi-3-mini)
     → cleans garbled text, normalises whitespace, confirms chunk boundaries
  4. Table serialisation via LLM
     → converts each table to NL summary (embedded) + markdown (stored as table_raw)
  5. Parent-child chunking
     → parent = full section under a heading (~1000 tokens)
     → children = paragraphs / tables within that section (~200 tokens each)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentTable

from shared.azure_clients import get_document_intelligence_client, get_openai_client
from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)


# ── Light LLM helpers ─────────────────────────────────────────────────────────

def _llm_clean_page_text(raw_text: str, page_num: int) -> str:
    """
    Light LLM pass to clean a page's extracted text.
    Fixes hyphenation, removes artefacts, normalises whitespace.
    Uses the cheapest deployment (gpt-4o-mini / phi-3-mini).
    """
    if not raw_text.strip():
        return ""
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a document cleaning assistant. "
                    "Fix broken hyphenation, remove repeated header/footer artefacts, "
                    "normalise whitespace. Return ONLY the cleaned text, nothing else."
                ),
            },
            {
                "role": "user",
                "content": f"Page {page_num} text:\n\n{raw_text[:4000]}",
            },
        ],
        temperature=0,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def _llm_serialise_table(table_markdown: str, context_heading: str) -> str:
    """
    Convert a markdown table to a natural language summary for embedding.
    The original markdown is preserved as table_raw.
    """
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "Convert the table into 2-5 clear natural language sentences "
                    "that capture all key data. Be factual and complete. "
                    "Return ONLY the sentences, no preamble."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Section: {context_heading}\n\nTable:\n{table_markdown}"
                ),
            },
        ],
        temperature=0,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()


# ── Table conversion ──────────────────────────────────────────────────────────

def _table_to_markdown(table: DocumentTable) -> str:
    """Convert Document Intelligence DocumentTable to markdown string."""
    if not table.cells:
        return ""

    rows: dict[int, dict[int, str]] = {}
    header_row = 0
    for cell in table.cells:
        rows.setdefault(cell.row_index, {})[cell.column_index] = (cell.content or "").strip()
        if cell.kind == "columnHeader":
            header_row = cell.row_index

    if not rows:
        return ""

    col_count = max(max(r.keys()) for r in rows.values()) + 1
    lines = []
    for row_idx in sorted(rows.keys()):
        row = rows[row_idx]
        cells = [row.get(c, "") for c in range(col_count)]
        lines.append("| " + " | ".join(cells) + " |")
        if row_idx == header_row:
            lines.append("| " + " | ".join(["---"] * col_count) + " |")

    return "\n".join(lines)


# ── Header/footer detection ───────────────────────────────────────────────────

def _is_header_footer(polygon: list[float] | None, page_height: float) -> bool:
    """
    Returns True if bounding box is in top or bottom margin zone.
    polygon is [x0,y0, x1,y1, x2,y2, x3,y3] in inches from DI.
    """
    if not polygon or not page_height:
        return False
    ys = [polygon[i] for i in range(1, len(polygon), 2)]
    top_y    = min(ys)
    bottom_y = max(ys)
    margin   = page_height * settings.HEADER_FOOTER_MARGIN_PCT
    return top_y < margin or bottom_y > (page_height - margin)


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_pdf(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """
    Full PDF parsing pipeline.
    Returns list of RawChunk (parent + child chunks).
    """
    ingested_at = datetime.now(timezone.utc).isoformat()
    di_client   = get_document_intelligence_client()

    logger.info("Analysing PDF with Document Intelligence: %s", doc_name)
    poller = di_client.begin_analyze_document(
        "prebuilt-layout",
        analyze_request=AnalyzeDocumentRequest(bytes_source=file_bytes),
        output_content_format="markdown",   # get markdown output for tables
    )
    result = poller.result()

    # ── Build page height map ─────────────────────────────────────────────────
    page_heights: dict[int, float] = {}
    if result.pages:
        for page in result.pages:
            page_heights[page.page_number] = page.height or 11.0  # default letter

    # ── Extract document title from first paragraph or heading ────────────────
    doc_title = doc_name.replace(".pdf", "").replace("_", " ")
    if result.paragraphs:
        for para in result.paragraphs[:5]:
            role = getattr(para, "role", None)
            if role in ("title", "sectionHeading") and para.content:
                doc_title = para.content.strip()
                break

    # ── Walk paragraphs in page order ─────────────────────────────────────────
    chunks: list[RawChunk] = []
    current_heading    = ""
    current_subheading = ""
    current_parent_id  = str(uuid4())
    current_parent_content: list[str] = []
    current_parent_page = 1

    def _flush_parent():
        """Create the parent chunk from accumulated content."""
        nonlocal current_parent_id, current_parent_content
        if not current_parent_content:
            return
        parent_text = "\n\n".join(current_parent_content)
        parent = RawChunk(
            chunk_id          = current_parent_id,
            parent_id         = "",
            chunk_type        = ChunkType.HEADING if current_heading else ChunkType.PARAGRAPH,
            domain            = domain,
            doc_name          = doc_name,
            source            = doc_name,
            doc_url           = doc_url,
            file_type         = "pdf",
            blob_path         = blob_path,
            ingested_at       = ingested_at,
            page_number       = current_parent_page,
            title             = doc_title,
            section_heading   = current_heading,
            section_subheading= current_subheading,
            content           = parent_text,
            table_raw         = "",
        )
        chunks.append(parent)
        current_parent_id      = str(uuid4())
        current_parent_content = []

    # Track which tables we've already processed (DI reports them separately)
    processed_table_ids: set[int] = set()

    # Build table lookup: (page_number, row, col bounds) → DocumentTable
    table_by_page: dict[int, list[DocumentTable]] = {}
    if result.tables:
        for tbl in result.tables:
            for region in (tbl.bounding_regions or []):
                table_by_page.setdefault(region.page_number, []).append(tbl)

    if result.paragraphs:
        for para in result.paragraphs:
            page_num = 1
            polygon  = None
            if para.bounding_regions:
                page_num = para.bounding_regions[0].page_number
                polygon  = para.bounding_regions[0].polygon

            page_height = page_heights.get(page_num, 11.0)

            # Skip header/footer zones
            if _is_header_footer(polygon, page_height):
                continue

            role    = getattr(para, "role", None)
            content = (para.content or "").strip()
            if not content:
                continue

            # Heading → flush parent, start new section
            if role in ("title", "sectionHeading", "heading1", "heading2"):
                _flush_parent()
                current_parent_page = page_num
                if role in ("title",):
                    doc_title = content
                    current_heading    = content
                    current_subheading = ""
                elif role in ("heading1", "sectionHeading"):
                    current_heading    = content
                    current_subheading = ""
                else:
                    current_subheading = content

                # Heading itself becomes a child chunk
                heading_chunk = RawChunk(
                    chunk_id           = str(uuid4()),
                    parent_id          = current_parent_id,
                    chunk_type         = ChunkType.HEADING,
                    domain             = domain,
                    doc_name           = doc_name,
                    source             = doc_name,
                    doc_url            = doc_url,
                    file_type          = "pdf",
                    blob_path          = blob_path,
                    ingested_at        = ingested_at,
                    page_number        = page_num,
                    title              = doc_title,
                    section_heading    = current_heading,
                    section_subheading = current_subheading,
                    content            = content,
                )
                chunks.append(heading_chunk)
                current_parent_content.append(content)
                continue

            # Clean text with light LLM
            cleaned = _llm_clean_page_text(content, page_num)
            if not cleaned:
                continue

            current_parent_content.append(cleaned)

            # Paragraph child chunk
            child = RawChunk(
                chunk_id           = str(uuid4()),
                parent_id          = current_parent_id,
                chunk_type         = ChunkType.PARAGRAPH,
                domain             = domain,
                doc_name           = doc_name,
                source             = doc_name,
                doc_url            = doc_url,
                file_type          = "pdf",
                blob_path          = blob_path,
                ingested_at        = ingested_at,
                page_number        = page_num,
                title              = doc_title,
                section_heading    = current_heading,
                section_subheading = current_subheading,
                content            = cleaned,
            )
            chunks.append(child)

    _flush_parent()

    # ── Process tables ────────────────────────────────────────────────────────
    if result.tables:
        for idx, table in enumerate(result.tables):
            if idx in processed_table_ids:
                continue

            page_num = 1
            polygon  = None
            if table.bounding_regions:
                page_num = table.bounding_regions[0].page_number
                polygon  = table.bounding_regions[0].polygon

            if _is_header_footer(polygon, page_heights.get(page_num, 11.0)):
                continue

            table_md = _table_to_markdown(table)
            if not table_md:
                continue

            # LLM → NL summary for embedding
            nl_summary = _llm_serialise_table(table_md, current_heading)

            table_chunk = RawChunk(
                chunk_id           = str(uuid4()),
                parent_id          = current_parent_id,
                chunk_type         = ChunkType.TABLE,
                domain             = domain,
                doc_name           = doc_name,
                source             = doc_name,
                doc_url            = doc_url,
                file_type          = "pdf",
                blob_path          = blob_path,
                ingested_at        = ingested_at,
                page_number        = page_num,
                title              = doc_title,
                section_heading    = current_heading,
                section_subheading = current_subheading,
                content            = nl_summary,     # ← embedded
                table_raw          = table_md,        # ← returned to LLM at query time
            )
            chunks.append(table_chunk)
            processed_table_ids.add(idx)

    logger.info("PDF parsed: %s → %d chunks", doc_name, len(chunks))
    return chunks
