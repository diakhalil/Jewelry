# SheCraft 💍✨
## Full-Stack Jewelry Web Application

SheCraft is a full-stack jewelry web application built with modern web technologies.

---
## Contributors 
- **Lana Fliti** — [@Lana-fl](https://github.com/Lana-fl)
- **Michelle Baalbaky** — [@michelle-baalbaky](https://github.com/michelle-baalbaky)
- **Rajaa Ghanem** — [@RDGH5825825](https://github.com/RDGH5825825)

## Live Development Ports 🌐
- **Frontend:** http://localhost:3000
- **Backend:** http://localhost:5000
- **Database:** MySQL (`jewelry`)
- **Images:** http://localhost:5000/images/...

---

## 📚 Table of Contents
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Database Setup](#database-setup)
- [Backend Setup (Port 5000)](#backend-setup-port-5000)
- [Frontend Setup (Port 3000)](#frontend-setup-port-3000)
- [Environment Variables](#environment-variables)
- [API Routes](#api-routes)
- [Static Images](#static-images)
- [Health Check](#health-check)
- [Troubleshooting](#troubleshooting)
- [Contributors](#contributors)

---

## Project Structure📁 


```text
DB_SHECRAFT/
├─ BackEnd/
│  ├─ shecraft-backend/
│  │  ├─ controllers/
│  │  ├─ models/
│  │  ├─ routes/
│  │  ├─ config/
│  │  ├─ middleware/
│  │  ├─ public/
│  │  │  └─ images/
│  │  ├─ app.js
│  │  └─ package.json
│  │
│  ├─ sql/
│  │  ├─ Database jewelry.sql
│  │  └─ Seed Data.sql
│  │
│  └─ .env (NOT committed)
│
└─ FrontEnd/
   └─ shecraft-frontend/
      ├─ src/
      │  ├─ api/
      │  ├─ context/
      │  ├─ hooks/
      │  └─ pages/
      └─ package.json
```

## Tech Stack🛠 
- **Frontend:** React
- **Backend:** Node.js, Express
- **Database:** MySQL (8+)
- **Security:** JWT Authentication
- **Other:** CORS enabled for `http://localhost:3000`

---

## Prerequisites✅
- Node.js (LTS)
- npm
- MySQL Server (running)

---

## Quick Start

### 1️⃣ Database
- Import schema
- Import seed data

### 2️⃣ Backend (Port 5000)

```bash
cd BackEnd
npm ci
npm run dev
```
### 3️⃣ Frontend (Port 3000)
```bash
cd FrontEnd/shecraft-frontend
npm install
npm start
```
## Database Setup🗄
SQL scripts location:

- BackEnd/sql/Database jewelry.sql
- BackEnd/sql/Seed Data.sql
  
  These scripts should be executed in mysql in order to create the database in mysql (using the mysql command line)
  you can run in mysql:
  ```bash
  SOURCE <your-path-where-put-the-project>/BackEnd/sql/Database jewelry.sql;
  ```

✔ Safe to rerun
✔ Uses ON DUPLICATE KEY UPDATE and INSERT IGNORE

DO NOTE: the database is initially empty in terms of customers and designers so you would need to populate it in terms of designers before ordering as a customer. 

## Backend Setup (Port 5000)🔌
Create a `.env` file in `BackEnd`, next to `package.json`.
```bash
env
PORT=5000
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=jewelry
JWT_SECRET=your_secret
```

Run the backend:

```bash
npm i
npm run dev
```
Backend URL:
http://localhost:5000

## Frontend Setup (Port 3000)🖥 

Be sure to have a .env file in shecraft-frontend with the following code
```bash
REACT_APP_API_URL=http://localhost:5000
VITE_API_URL=http://localhost:5000
```
Then run the following
```bash
cd FrontEnd/shecraft-frontend
npm install
npm start
```
Frontend URL:

http://localhost:3000
## Environment Variables🔐
❌ Never commit .env

✅ Use .env.example

## Ask SheCraft

### Generate a ring or bracelet

In a **VS Code PowerShell terminal at the Jewelry repository root**, start Express or restart the existing process to load the new routes.

```powershell
Set-Location .\BackEnd
npm.cmd run dev
```

In a second terminal from **Jewelry**, run `cd .\FrontEnd\shecraft-frontend` then `npm.cmd start`. Open `http://localhost:3000`, click **Ask SheCraft**, and try **Generate a gold sapphire ring** or **Make a silver bracelet**. Express generates a GLB with Blender and the chat links to a rotatable preview. Ring and bracelet generation uses Express and Blender. Knowledge questions use the Python RAG service.

Express checks the Windows Blender installation folder and the `PATH`. For a custom installation, set `$env:BLENDER_PATH` to the full path of `blender.exe` in the Express terminal before you start it. Generated previews live in the server's bounded cache for up to an hour.

The Express terminal logs request IDs, routing, Blender output, exports, and errors. The Python terminal logs retrieval, Gemini generation, and failures for requests sent to this branch's RAG service. Match the request ID in a chat error with the logs. You can also stream the progress events in PowerShell while Express is running.

```powershell
$body = @{question = 'Generate a gold sapphire ring'} | ConvertTo-Json -Compress
$body | curl.exe -N http://localhost:5000/api/chat -H 'Content-Type: application/json' -H 'Accept: application/x-ndjson' --data-binary '@-'
```

### Run jewelry knowledge questions locally

From the **Jewelry** folder in a new PowerShell terminal, start the local knowledge service. It indexes the repository PDFs in an in-process Qdrant collection on startup. With no Gemini key, chat returns matching passages with page citations. Add `GEMINI_API_KEY` to `ai-service/.env` and restart the service to use Gemini for answers based on those passages.

```powershell
Set-Location .\ai-service
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = "$env:USERPROFILE\anaconda3\python.exe" }
if (-not (Test-Path .\.venv\Scripts\python.exe)) { & $python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
$env:RAG_MODE = 'local'
& .\.venv\Scripts\python.exe -m uvicorn doc_rag.api:app --app-dir src/rag --host 127.0.0.1 --port 8001 --log-level info
```

Keep that terminal open and ask **How do I measure my ring size?** or **What is the birthstone for May?** in Ask SheCraft. Run `Invoke-RestMethod http://127.0.0.1:8001/health` from another PowerShell terminal to see the number of indexed PDFs and the active answer mode.

### Enable the full Docker-backed RAG service

With [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) running, start Qdrant from PowerShell. Use `docker start jewelry-qdrant` if the named container already exists.

```powershell
docker info
docker run -d --name jewelry-qdrant --restart unless-stopped -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 -v jewelry_qdrant_storage:/qdrant/storage qdrant/qdrant
Invoke-RestMethod http://127.0.0.1:6333/collections
```

To follow the container logs from another terminal, run `docker logs -f jewelry-qdrant`.

From **Jewelry**, prepare the Python environment and index the PDFs. On a fresh machine this downloads the embedding models. Run the indexing step again when the documents change or when you start with an empty Qdrant volume.

```powershell
Set-Location .\ai-service
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = "$env:USERPROFILE\anaconda3\python.exe" }
& $python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -u .\src\rag\doc_rag\pipeline.py .\rag_documents --no-images
```

Save your `GEMINI_API_KEY` in `ai-service/.env`. From **ai-service**, run the Python server in its own terminal and keep it open.

```powershell
$env:RAG_TEXT_ONLY = '1'
$env:RAG_MODE = 'server'
& .\.venv\Scripts\python.exe -m uvicorn doc_rag.api:app --app-dir src/rag --host 127.0.0.1 --port 8001 --log-level info
```

From another terminal, run `Invoke-RestMethod http://127.0.0.1:8001/health` and `Invoke-RestMethod http://localhost:5000/api/chat/health`. Ask **How do I measure my ring size?** when both report ready.

If you already have the team's newer service running with `app:app --app-dir src/rag/service` on port `8000`, set `RAG_API_URL=http://127.0.0.1:8000` in `BackEnd/.env` and restart Express. The proxy accepts both versions of the Python chat API. The newer service uses its own document index.

## Standalone 3D ring demo

Run the 3D checks from the repository root.

```powershell
node --test .\BackEnd\test\jewelryPreview.test.js
node .\tools\test-3d-http.cjs
```

The HTTP test generates six Blender models, loads each through Three.js, checks materials and finite geometry, and verifies the two-worker limit with three simultaneous requests. The unit tests cover supported commands, unsupported materials and features, invalid questions, missing previews, unavailable RAG, missing Blender, and cancellation before export.

Ask SheCraft collects the item type, style, metal, gemstone, diamond color, tennis stone count, and ring size or bracelet diameter before asking for confirmation. Follow-up changes retain the other choices. Choose **Generate it** after reviewing the summary. Tennis previews support 8–32 stones, diamond colors white/pink/blue/yellow/green, and US ring sizes 3–13 in half-size increments. Sizes are preview dimensions.

Run `node .\tools\test-design-conversation.cjs` from the repository root to verify a complete conversation that creates a 20-stone pink-diamond tennis ring, then revises it to 28 blue diamonds. The test loads both exported GLBs and checks the stone counts, colors, and positions.

The original agent brief still requires a component manifest with attachment metadata, three registered LangChain tools, an LLM planner, Pydantic assembly validation, LangGraph shared state and supervisor/validation routing, and frontend execution logs with real agent/tool-call metadata. The current guided design conversation uses deterministic choice extraction.

Terminal logs contain request IDs, validation, Blender output, export size, completion, cancellation, and errors. Chat reports actual execution stages. Preview links expire after one hour or a backend restart. Remote verification requires the deployed host's URL and runtime access.

On a machine with Docker Desktop running, use the Compose configuration from the repository root.

```powershell
docker compose config
docker compose up -d qdrant
docker compose ps
docker compose logs -f qdrant
```

Compose uses the named volume `jewelry_qdrant_storage`. Use `RAG_MODE=server` and the full RAG setup above to connect to Docker. The `local` mode uses an in-process Qdrant collection.

The demo uses a Blender-generated gold band, four-prong setting, and faceted sapphire. The generator works in millimeters and exports a meter-scale GLB with gold and sapphire PBR materials. The storefront loads the GLB directly and provides orbit, zoom, reset, and auto-rotate. The demo runs independently of Express, MySQL, and the AI service.

In a **VS Code PowerShell terminal at the repository root**, locate Blender, regenerate the model, and verify it by importing the exported GLB into a clean Blender scene.

```powershell
$blender = (Get-Command blender.exe -ErrorAction SilentlyContinue).Source
if (-not $blender) {
    $blender = 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe'
}
& $blender --version
& $blender --background --factory-startup --python-exit-code 1 --python .\tools\generate-sapphire-ring.py
& $blender --background --factory-startup --python-exit-code 1 --python .\tools\test-sapphire-ring.py
Get-Item .\FrontEnd\shecraft-frontend\public\models\gold-sapphire-ring.glb
```

If your Blender is installed elsewhere, set `$blender` to the full path to `blender.exe`. The scripts use Blender's bundled Python.

In a **second PowerShell terminal at the repository root**, start the existing frontend.

```powershell
node --version
npm.cmd --version
Set-Location .\FrontEnd\shecraft-frontend
npm.cmd ci
npm.cmd start
```

Open `http://localhost:3000/rings/sapphire-preview`, or select **View the gold & sapphire ring in 3D** from **Rings**. To verify the storefront, run `npm.cmd test -- --watchAll=false --runInBand` and `npm.cmd run build` from `FrontEnd/shecraft-frontend`.

## API Routes🔗
Base URL:
## 🔗 API Routes

**Base URL:**  
`http://localhost:5000/api`

| Route                     | Description                |
|---------------------------|----------------------------|
| `/api/auth`               | Authentication             |
| `/api/customers`          | Customers                  |
| `/api/accessories`        | Accessories                |
| `/api/orders`             | Orders                     |
| `/api/designers`          | Designers                  |
| `/api/charms`             | Charms                     |
| `/api/materials`          | Materials                  |
| `/api/stones`             | Stones                     |
| `/api/accessory-instance` | Custom accessory builds    |

## Static Images🖼 
Images are served from:

```swift
BackEnd/shecraft-backend/public/images
```
Example:

```bash
http://localhost:5000/images/Charms/colorful/A001.jpg
```
## Health Check
### Endpoint
```http
GET http://localhost:5000/api/health/db
```

Response:

```json
{ "db": "up" }
```
## Troubleshooting🧯

#### Frontend cannot reach backend
- Backend running on port 5000
- Frontend running on port 3000
- Correct API base URL
- CORS enabled

#### Database errors
- MySQL server running
- Correct .env credentials
- Schema imported successfully

#### Images not loading
- Images exist in ```bash public/images```
- Database paths start with ```bash /images/ ```

