"""DocQA backend: the single entry point for every request to /server/docqa_api/...

Security model
--------------
* Who is asking comes ONLY from Catalyst's login (get_current_user), never from
  anything the browser sends.
* App Users have no direct table permissions, so all data access goes through
  this function using admin access - and every query is filtered by owner_id.
"""
import json
import logging
import os
import re
import uuid

from flask import Request, jsonify, make_response
import zcatalyst_sdk

import llm
from extract import ALLOWED_EXTENSIONS, chunk_text, extract_text, get_extension
from search import pick_terms, rank_chunks

BUCKET = "docqa-files"
MAX_FILE_BYTES = 5 * 1024 * 1024    # 5 MB
MAX_CHUNKS_PER_DOC = 1000
TEXT_LIMIT = 9900                   # text columns hold 10,000 characters
HISTORY_TURNS = 6                   # earlier messages sent along with a follow-up
TOP_K = 5                           # excerpts sent to the model per question
SNIPPET_CHARS = 1400                # excerpt length stored for citations
# ZCQL allows at most 10 WHERE conditions. owner_id, document_id and ROWID
# paging use 3, leaving 7 "content LIKE" words per query.
TERMS_PER_QUERY = 7
MAX_SEARCH_TERMS = 14               # so at most 2 search queries per question

logger = logging.getLogger()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def handler(request: Request):
    # 1. Identify the user. initialize() itself fails when nobody is logged in.
    try:
        user_app = zcatalyst_sdk.initialize()
        user = user_app.user_management().get_current_user()
        owner_id = int(user["user_id"])
    except Exception:
        return _error(401, "Please log in.")

    # 2. A second, admin-scoped connection for the database, files and AI.
    app = zcatalyst_sdk.initialize(name="admin", scope="admin")

    path = request.path.rstrip("/") or "/"
    method = request.method

    try:
        if path == "/me" and method == "GET":
            return _ok({
                "user_id": str(owner_id),
                "first_name": user.get("first_name", ""),
                "email": user.get("email_id", ""),
            })

        if path == "/documents" and method == "GET":
            return _ok(list_documents(app, owner_id))
        if path == "/documents" and method == "POST":
            return _ok(upload_document(app, owner_id, request), 201)
        match = re.fullmatch(r"/documents/(\d+)", path)
        if match and method == "DELETE":
            delete_document(app, owner_id, match.group(1))
            return _ok({"deleted": True})

        if path == "/conversations" and method == "GET":
            return _ok(list_conversations(app, owner_id))
        match = re.fullmatch(r"/conversations/(\d+)/messages", path)
        if match and method == "GET":
            return _ok(list_messages(app, owner_id, match.group(1)))
        match = re.fullmatch(r"/conversations/(\d+)", path)
        if match and method == "DELETE":
            delete_conversation(app, owner_id, match.group(1))
            return _ok({"deleted": True})

        if path == "/ask" and method == "POST":
            return _ok(ask(app, owner_id, request.get_json(silent=True) or {}))

        return _error(404, "Not found")
    except ApiError as e:
        return _error(e.status, e.message)
    except llm.LLMError as e:
        return _error(502, str(e))
    except Exception:
        logger.exception("Unhandled error on %s %s", method, path)
        return _error(500, "Something went wrong. Please try again.")


# ---------------------------------------------------------------- responses

def _ok(data, status=200):
    return make_response(jsonify(data), status)


def _error(status, message):
    return make_response(jsonify({"error": message}), status)


# ---------------------------------------------------------------- database helpers

def _query(app, sql):
    """Run ZCQL and return plain row dicts (ZCQL wraps each row as {"Table": {...}})."""
    rows = app.zcql().execute_query(sql) or []
    return [next(iter(r.values())) for r in rows]


def _select_all(app, table, where):
    """Fetch every matching row. ZCQL returns at most 300 rows per query,
    so page through by ROWID."""
    rows, last_id = [], 0
    while True:
        batch = _query(
            app,
            f"SELECT * FROM {table} WHERE {where} AND ROWID > {last_id} "
            f"ORDER BY ROWID ASC LIMIT 300",
        )
        rows.extend(batch)
        if len(batch) < 300:
            return rows
        last_id = int(batch[-1]["ROWID"])


def _get_owned(app, table, row_id, owner_id):
    """Fetch one row by ROWID, but only if it belongs to owner_id.
    Someone else's row looks exactly like a missing one: 404."""
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        raise ApiError(404, "Not found")
    rows = _query(
        app,
        f"SELECT * FROM {table} WHERE ROWID = {row_id} AND owner_id = {owner_id}",
    )
    if not rows:
        raise ApiError(404, "Not found")
    return rows[0]


def _insert_many(app, table, rows):
    """Insert rows 200 at a time (Catalyst's per-request limit)."""
    inserted = []
    for i in range(0, len(rows), 200):
        inserted.extend(app.datastore().table(table).insert_rows(rows[i:i + 200]))
    return inserted


def _delete_many(app, table, row_ids):
    for i in range(0, len(row_ids), 100):
        app.datastore().table(table).delete_rows(row_ids[i:i + 100])


def _load_json_list(value):
    try:
        data = json.loads(value) if value else []
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------- documents

def _document_out(row):
    return {
        "id": str(row["ROWID"]),
        "file_name": row["file_name"],
        "status": row.get("status") or "",
        "chunk_count": int(row.get("chunk_count") or 0),
        "summary": row.get("summary") or "",
        "suggested_questions": _load_json_list(row.get("suggested_questions")),
        "created_time": row.get("CREATEDTIME", ""),
    }


def list_documents(app, owner_id):
    rows = _select_all(app, "Documents", f"owner_id = {owner_id}")
    rows.reverse()  # newest first
    return [_document_out(r) for r in rows]


def _safe_file_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ()]", "_", name)
    return name[:200] or "document"


def upload_document(app, owner_id, request):
    upload = request.files.get("file")
    if upload is None:
        raise ApiError(400, "No file was sent.")

    file_name = _safe_file_name(upload.filename)
    ext = get_extension(file_name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ApiError(400, "Only PDF, Word (.docx), .txt and .md files are supported.")

    data = upload.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(400, "File is larger than 5 MB.")
    if not data:
        raise ApiError(400, "The file is empty.")

    try:
        text = extract_text(file_name, data)
    except Exception:
        logger.exception("Extraction failed for %s", file_name)
        raise ApiError(400, "Couldn't read this file. Is it damaged or password-protected?")

    chunks = chunk_text(text)
    if not chunks:
        raise ApiError(400, "No text found in this file. Scanned PDFs (images of pages) aren't supported yet.")
    if len(chunks) > MAX_CHUNKS_PER_DOC:
        raise ApiError(400, "This document is too long. Please split it into smaller files.")

    # Files live under the owner's own folder, with a random name so two
    # uploads called "report.pdf" never overwrite each other.
    file_key = f"users/{owner_id}/{uuid.uuid4().hex}{ext}"
    app.stratus().bucket(BUCKET).put_object(file_key, data)

    doc_id = None
    try:
        doc = app.datastore().table("Documents").insert_row({
            "owner_id": owner_id,
            "file_name": file_name,
            "file_key": file_key,
            "status": "processing",
            "chunk_count": len(chunks),
        })
        doc_id = int(doc["ROWID"])

        _insert_many(app, "Chunks", [
            {"owner_id": owner_id, "document_id": doc_id, "chunk_index": i, "content": c}
            for i, c in enumerate(chunks)
        ])

        summary, questions = llm.summarize_document(app, file_name, text)
        updated = app.datastore().table("Documents").update_row({
            "ROWID": doc_id,
            "status": "ready",
            "summary": summary,
            "suggested_questions": json.dumps(questions),
        })
        return _document_out({**doc, **updated})
    except Exception:
        # Don't leave half-saved data behind.
        logger.exception("Upload failed; cleaning up %s", file_key)
        _cleanup_document(app, owner_id, doc_id, file_key)
        raise


def _cleanup_document(app, owner_id, doc_id, file_key):
    try:
        app.stratus().bucket(BUCKET).delete_object(file_key)
    except Exception:
        logger.exception("Could not delete file %s", file_key)
    if doc_id:
        chunk_rows = _select_all(app, "Chunks", f"owner_id = {owner_id} AND document_id = {doc_id}")
        if chunk_rows:
            _delete_many(app, "Chunks", [int(r["ROWID"]) for r in chunk_rows])
        app.datastore().table("Documents").delete_row(doc_id)


def delete_document(app, owner_id, doc_id):
    doc = _get_owned(app, "Documents", doc_id, owner_id)
    _cleanup_document(app, owner_id, int(doc["ROWID"]), doc["file_key"])


# ---------------------------------------------------------------- conversations

def list_conversations(app, owner_id):
    rows = _select_all(app, "Conversations", f"owner_id = {owner_id}")
    rows.reverse()
    return [
        {"id": str(r["ROWID"]), "title": r.get("title") or "Untitled", "created_time": r.get("CREATEDTIME", "")}
        for r in rows
    ]


def _message_out(row):
    return {
        "id": str(row["ROWID"]),
        "role": row["role"],
        "content": row["content"],
        "sources": _load_json_list(row.get("sources")),
        "created_time": row.get("CREATEDTIME", ""),
    }


def list_messages(app, owner_id, conversation_id):
    conv = _get_owned(app, "Conversations", conversation_id, owner_id)
    rows = _select_all(
        app, "Messages", f"owner_id = {owner_id} AND conversation_id = {int(conv['ROWID'])}"
    )
    return [_message_out(r) for r in rows]


def delete_conversation(app, owner_id, conversation_id):
    conv = _get_owned(app, "Conversations", conversation_id, owner_id)
    conv_id = int(conv["ROWID"])
    rows = _select_all(app, "Messages", f"owner_id = {owner_id} AND conversation_id = {conv_id}")
    if rows:
        _delete_many(app, "Messages", [int(r["ROWID"]) for r in rows])
    app.datastore().table("Conversations").delete_row(conv_id)


# ---------------------------------------------------------------- asking

def _sources_json(sources):
    """Citations must fit in the 10,000-character column; trim snippets until they do."""
    limit = SNIPPET_CHARS
    while True:
        trimmed = [{**s, "text": s["text"][:limit]} for s in sources]
        encoded = json.dumps(trimmed)
        if len(encoded) <= TEXT_LIMIT or limit <= 100:
            return encoded, trimmed
        limit -= 200


def _candidate_chunks(app, owner_id, doc_ids, search_query):
    """Let the database pick out only the passages that contain at least one
    search word, instead of loading every passage the user has."""
    owned = f"owner_id = {owner_id} AND document_id IN ({', '.join(str(d) for d in doc_ids)})"
    terms = pick_terms(search_query, MAX_SEARCH_TERMS)
    if not terms:
        return []
    try:
        found = {}
        for i in range(0, len(terms), TERMS_PER_QUERY):
            likes = " OR ".join(f"content LIKE '*{t}*'" for t in terms[i:i + TERMS_PER_QUERY])
            for row in _select_all(app, "Chunks", f"{owned} AND ({likes})"):
                found[row["ROWID"]] = row
        return list(found.values())
    except Exception:
        # Slower, but always works: load every passage and let BM25 sort them out.
        logger.exception("Keyword pre-filter failed; loading all chunks")
        return _select_all(app, "Chunks", owned)


def ask(app, owner_id, body):
    question = str(body.get("question", "")).strip()[:2000]
    if not question:
        raise ApiError(400, "Please type a question.")

    # Which documents to search: the user's own, optionally narrowed to the ones they ticked.
    documents = {int(r["ROWID"]): r for r in _select_all(app, "Documents", f"owner_id = {owner_id}")}
    if not documents:
        raise ApiError(400, "Upload a document first.")

    requested = body.get("document_ids") or []
    if requested:
        try:
            requested_ids = {int(x) for x in requested}
        except (TypeError, ValueError):
            raise ApiError(400, "Invalid document selection.")
        search_ids = [d for d in requested_ids if d in documents]  # silently drops others' IDs
        if not search_ids:
            raise ApiError(400, "None of the selected documents were found.")
    else:
        search_ids = list(documents)

    # Continue an existing conversation, or start a new one.
    conversation_id = body.get("conversation_id")
    history = []
    if conversation_id:
        conv = _get_owned(app, "Conversations", conversation_id, owner_id)
        conversation_id = int(conv["ROWID"])
        history = _query(
            app,
            f"SELECT * FROM Messages WHERE owner_id = {owner_id} AND conversation_id = {conversation_id} "
            f"ORDER BY ROWID DESC LIMIT {HISTORY_TURNS}",
        )
        history.reverse()
    else:
        conv = app.datastore().table("Conversations").insert_row({
            "owner_id": owner_id,
            "title": question[:80],
        })
        conversation_id = int(conv["ROWID"])

    # Find the best passages, searching ONLY this user's chunks.
    search_query = llm.rewrite_query(app, question, history)
    chunks = _candidate_chunks(app, owner_id, search_ids, search_query)
    top = rank_chunks(search_query, chunks, top_k=TOP_K)

    excerpts = [
        {
            "document_id": str(c["document_id"]),
            "file_name": documents[int(c["document_id"])]["file_name"],
            "chunk_index": int(c["chunk_index"]),
            "content": c["content"],
        }
        for c in top
    ]
    answer = llm.answer_question(app, question, excerpts, history)[:TEXT_LIMIT]

    sources_text, sources = _sources_json([
        {
            "n": i,
            "document_id": e["document_id"],
            "file_name": e["file_name"],
            "chunk_index": e["chunk_index"],
            "text": e["content"],
        }
        for i, e in enumerate(excerpts, start=1)
    ])

    messages = app.datastore().table("Messages").insert_rows([
        {"owner_id": owner_id, "conversation_id": conversation_id, "role": "user", "content": question},
        {"owner_id": owner_id, "conversation_id": conversation_id, "role": "assistant",
         "content": answer, "sources": sources_text},
    ])

    return {
        "conversation_id": str(conversation_id),
        "question": _message_out(messages[0]),
        "answer": _message_out(messages[1]),
        "sources": sources,
    }
