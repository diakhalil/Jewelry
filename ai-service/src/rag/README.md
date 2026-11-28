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

## Customer requirements and supervisor

Install the updated `requirements.txt` in the same virtual environment and
start the same Uvicorn app. The React **Ask SheCraft** widget uses
`POST /api/assistant/chat` with a UUID `conversation_id` and a `message`.
The response streams newline-delimited JSON progress events followed by a
result containing the answer, structured design brief and guide sources, or
an error. The original `POST /api/chat` endpoint remains available for RAG.

The supervisor delegates design requests to Customer Requirements and factual
questions to the existing knowledge service. Requirements chooses among
required-field, consistency and supported-option tools. Its Gemini structured
output updates only the changed preferences. The graph publishes the brief at
the end of a successful turn. A ready brief is available for a future Design
Agent. The current graph does not submit orders or run final validation.

Use a Gemini Developer API project on the free tier. The supervisor and
requirements agent share `REQUIREMENTS_MODEL` (default `gemini-3.5-flash-lite`).
Material and stone lookups use `SHECRAFT_BACKEND_URL` (default
`http://localhost:5000`). Guide questions use the existing RAG configuration.
Start one Uvicorn worker for the course demo. LangGraph's in-memory checkpointer
keeps each conversation during the process lifetime. Closing and reopening the
widget keeps the conversation; refreshing the page starts a new one.

For a demo, set `SHECRAFT_AGENT_TRACE=true` in `ai-service/.env` and restart
Uvicorn. Use fictional inputs. The server prints LangChain message types,
public message content, model-selected tool arguments, tool results and the
structured state update. The widget shows live activity and an expandable brief.
Try “I want a silver necklace under USD 150” followed by “Actually, USD 100,
and you can choose the chain.” Then ask a standalone jewelry-care question.

Run the offline regression tests from `ai-service`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_requirements_agent.py
```

### Connecting a teammate’s agent

The handoff is the Pydantic `Brief` in [brief.py](../agents/brief.py). Within a
turn, read `state.draft` to see edits already applied by Customer Requirements.
At the end of a successful turn, `finish` publishes that draft as `state.brief`.
The HTTP client receives the same object under `brief` in the final result event.
For example, after selecting a Celeste Solitaire, part of the result can look like this:

```json
{
  "status": "collecting",
  "preferences": [
    {"field": "style", "value": "Celeste Solitaire", "intent": "preferred"}
  ],
  "option_field": "stone_count",
  "options": ["0", "1", "2"]
}
```

Each preference carries its intent (`required`, `preferred`, `delegated`, or
`excluded`). Budget and measurement preferences can also carry a numeric
`amount` and a `unit`. Keep the preference list intact so exclusions and
multiple values survive the handoff. Use `missing`, `conflicts`, and
`next_question` to continue intake. A `ready` brief can inform design.
For `needs_review`, inspect `review_reasons` before proceeding.

1. In [graph.py](../agents/graph.py), define a Pydantic delegation tool with a
   clear description and add it to the supervisor’s `bind_tools` list. Describe
   the specialist’s remit and handoff conditions in [prompts.py](../agents/prompts.py).
2. Register the agent as a graph node. Extend `route`, including its return
   annotation, to map the model-selected tool name to that node. Read the
   current draft and check the inputs your agent needs before acting.
3. End the node with `observation(state, ...)` and route back through `route`
   to consume the next queued delegation or finish. If your agent produces a
   design or a validation report, declare a typed state field for it and
   update `finish` and the [API result](../agents/api.py) to expose it as needed.

The supervisor currently delegates to Requirements and Knowledge. Completing
a brief ends the intake turn, including when a menu click supplies the final
choice. A later request such as “design this ring” can be routed to a newly
registered specialist using the same conversation ID. An automatic same-turn
handoff would require changing the completion edges in the graph.
