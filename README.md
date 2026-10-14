# DocQA — Ask questions about your own documents

Upload your PDFs, Word files or notes, then chat with them. DocQA finds the
relevant passages in **your** files and answers with an AI model, showing the
sources it used. Every user's documents are private to them.

**🔗 Live demo:** https://docqa-60091697131.development.catalystserverless.in/app/index.html

> I built this on a student offer which is valid for 6 months. If the link doesnt work, the free tier has come to an end.
> The demo runs in a test (development) environment. Sign up with any email,
> and please don't upload anything sensitive.

Built entirely on [Zoho Catalyst](https://catalyst.zoho.com/).

---

## Features

- **Private per user** — you only ever see and search your own documents.
- **Upload several files at once** — `.pdf`, `.docx`, `.txt`, `.md` (up to 5 MB each).
- **Auto summary** — each upload gets a short summary and 3 suggested questions.
- **Answers with sources** — every answer shows which passages it came from.
- **Follow-up questions** — the chat remembers the conversation.
- **Ask selected documents** — tick specific files to search only those.
- **Smarter search** — the AI rewrites your question into keywords before searching.

## How it works

```
Browser (client/)                 Catalyst function (functions/docqa_api/)
  login, upload, chat   ───────▶  1. check who is logged in
                                  2. upload: extract text → split into passages → save
                                  3. ask: find best passages (BM25 keyword search)
                                          → send them + chat history to the AI
                                          → return answer + sources
```

| Part | Catalyst service |
|------|------------------|
| Login / sign-up | Authentication (hosted login) |
| Web page | Web Client Hosting |
| Backend API | Functions (Python 3.13, Advanced I/O) |
| Original files | Stratus (encrypted bucket) |
| Text passages, chats | Data Store |
| AI answers | QuickML — GLM-4.7 Flash |

Every database row is tagged with its owner's user ID, and the backend filters
on it for every request. Users have no direct access to the tables.

## Project layout

```
client/                    the web page (HTML, CSS, JavaScript)
functions/docqa_api/
  main.py                  API routes: /me, /documents, /conversations, /ask
  extract.py               reads PDF/DOCX/TXT and splits text into passages
  search.py                BM25 keyword ranking
  llm.py                   talks to the QuickML AI model
  catalyst-config.example.json   template for the function's settings
```

## Run your own copy

You need a [Zoho Catalyst](https://catalyst.zoho.com/) account, Python 3.13
and the Catalyst CLI (`npm install -g zcatalyst-cli`).

1. **Create a Catalyst project** and in the console set up:
   - **Authentication:** hosted login, public sign-up on.
   - **Data Store tables** (names/status are varchar; content, summary,
     suggested_questions and sources are text; ids are bigint):
     - `Documents`: owner_id (bigint), file_name, file_key, status, chunk_count (int), summary, suggested_questions
     - `Chunks`: owner_id (bigint), document_id (bigint), chunk_index (int), content
     - `Conversations`: owner_id (bigint), title
     - `Messages`: owner_id (bigint), conversation_id (bigint), role, content, sources
   - **Stratus bucket:** `docqa-files`.
   - **QuickML:** an LLM endpoint for GLM-4.7 Flash. Copy its endpoint key.
2. **Clone and connect:**
   ```
   git clone https://github.com/tataNikhil/docqa.git
   cd docqa
   catalyst login
   catalyst init      # pick your project
   ```
3. **Add your secret key:** copy
   `functions/docqa_api/catalyst-config.example.json` to
   `functions/docqa_api/catalyst-config.json` and paste your QuickML endpoint key.
   This file is in `.gitignore`, so it is never uploaded.
4. In `functions/docqa_api/llm.py`, change `GENERATE_PATH` and `CATALYST_ORG`
   to your endpoint's path and your org ID.
5. **Deploy:** `catalyst deploy`

## Security notes

- Secrets live only in `catalyst-config.json`, which Git ignores.
- All API routes require login; unauthenticated calls get `401`.
- Uploaded files are stored in an encrypted, non-public bucket.
