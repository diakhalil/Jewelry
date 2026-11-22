# Jewelry RAG ingestion

Run the complete incremental ingestion from the `ai-service` directory:

```powershell
py -m pip install -r requirements.txt
py src/rag/scripts/pipeline.py
```

Place PDFs under `rag_documents/`. Add a new PDF there and run the same command
again. The pipeline reuses extraction, semantic chunk, VLM description, and
embedding caches per PDF. It sends only missing or changed PDF content to
Gemini and uploads only new or changed points to Qdrant. Existing documents
remain indexed.

The pipeline connects to a Qdrant **server** at `QDRANT_URL` (default:
`http://localhost:6333`) and writes separate text and image collections there.
It does not use Qdrant's embedded local database. Start a local Qdrant server
with Docker if one is not already running:

```powershell
docker run -d --name jewelry-qdrant -p 6333:6333 -p 6334:6334 `
  -v jewelry_qdrant_storage:/qdrant/storage qdrant/qdrant
```

For a shared team database, configure every teammate with the same hosted or
network-accessible Qdrant server URL and API key in their ignored
`ai-service/.env` file. `localhost` points to each teammate's own computer, so
it does not share data across machines. `QDRANT_API_KEY` can be blank for a
local server without authentication. Keep `GEMINI_API_KEY` in that same
`.env` file; keep the VLM model setting used for existing image descriptions
configured as `GEMINI_VLM_MODEL`.

Use `--force` only when intentionally rebuilding cached document processing,
VLM descriptions, and embeddings. Normal runs are incremental.

## Run the chatbot RAG service locally

1. Start the Qdrant server and make sure `ai-service/.env` has
   `GEMINI_API_KEY` and the correct `QDRANT_URL` / `QDRANT_API_KEY`.
2. From `ai-service/`, run the ingestion command above once so both RAG
   collections are populated in that Qdrant server.
3. Install Python dependencies if needed, including FastAPI and Uvicorn:

   ```powershell
   py -m pip install -r requirements.txt
   ```

4. Start the API from `ai-service/`:

   ```powershell
   py -m uvicorn app:app --app-dir src/rag/service --host 127.0.0.1 --port 8000 --reload
   ```

   `GET http://localhost:8000/health` reports service and collection status;
   `POST http://localhost:8000/api/chat` accepts `{"message":"..."}` and
   returns a generated answer with retrieved text and image-description sources.
   Retrieved image files are served by `GET /api/images/{image_id}` from the
   local processed-document cache.

5. Start the React frontend as usual. The chatbot button appears at the bottom
   right of each page and calls `http://localhost:8000`. To change the URL, set
   `REACT_APP_RAG_API_URL` in the frontend environment before starting React.

The API uses Gemini Embedding 2 for query vectors, retrieves from the text and
image Qdrant collections, then calls the configured Gemini generation model.
Gemini and Qdrant credentials remain in the backend `.env`; they are never
sent to the browser. This local development service binds to `127.0.0.1`.
