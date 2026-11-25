const { randomUUID } = require("node:crypto");
const router = require("express").Router();
const { detectDesign, generatePreview, previewModel } = require("../jewelryPreview");
const { interview } = require("../designConversation");

const ragBaseUrl = (process.env.RAG_API_URL || "http://127.0.0.1:8001").replace(/\/$/, "");
const streamType = "application/x-ndjson";
const pythonRoutes = [
  { path: "/v1/ask", body: (question) => ({ question, experience: "shecraft" }) },
  { path: "/api/chat", body: (question) => ({ message: question }) },
];

function log(requestId, event, details = {}) {
  console.info(JSON.stringify({ time: new Date().toISOString(), requestId, event, ...details }));
}

async function askRag(question, requestId, signal) {
  const options = {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Request-ID": requestId },
    signal: AbortSignal.any([signal, AbortSignal.timeout(45_000)]),
  };
  let response;
  for (const { path, body } of pythonRoutes) {
    log(requestId, "rag.requested", { path });
    response = await fetch(ragBaseUrl + path, { ...options, body: JSON.stringify(body(question)) });
    log(requestId, "rag.responded", { path, status: response.status });
    if (response.status !== 404 || path === pythonRoutes.at(-1).path) break;
    await response.body?.cancel();
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`RAG HTTP ${response.status}`);
  }

  const result = await response.json();
  if (typeof result.answer !== "string" || !result.answer.trim()) throw new Error("Empty RAG answer");
  const sources = result.text_sources ?? result.sources;
  return {
    answer: result.answer,
    sources: Array.isArray(sources) ? sources.flatMap((source, index) => {
      if (typeof source?.file_name !== "string") return [];
      return [{
        reference: source.label ?? `T${index + 1}`,
        title: source.file_name,
        page: source.page_number ?? null,
        excerpt: source.text ?? source.excerpt ?? "",
      }];
    }) : [],
  };
}

router.get("/health", async (_req, res) => {
  try {
    const response = await fetch(`${ragBaseUrl}/health`, { signal: AbortSignal.timeout(3000) });
    await response.body?.cancel();
    if (response.ok) return res.json({ chat: "up", rag: "up" });
  } catch {
    // Keep the chat proxy health response useful when Python cannot be reached.
  }
  return res.status(503).json({ chat: "up", rag: "unavailable" });
});

router.get("/models/:id", (req, res) => {
  const { id } = req.params;
  if (!/^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/.test(id)) {
    return res.status(404).json({ message: "3D preview not found." });
  }
  const model = previewModel(id);
  log(id, model ? "preview.served" : "preview.unavailable");
  if (!model) return res.status(404).json({ message: "This 3D preview has expired. Generate it again in chat." });
  return res.type("model/gltf-binary").set("Cache-Control", "private, no-store").send(model);
});

router.post("/", async (req, res) => {
  const requestId = randomUUID();
  const started = Date.now();
  const question = typeof req.body?.question === "string" ? req.body.question.trim() : "";
  res.set("X-Request-ID", requestId);
  if (!question || question.length > 1000) {
    log(requestId, "chat.rejected", { code: "INVALID_QUESTION" });
    return res.status(400).json({ message: "Enter a question between 1 and 1000 characters.", requestId });
  }

  const stream = req.get("Accept")?.includes(streamType);
  const controller = new AbortController();
  const abortOnClose = () => {
    if (!res.writableEnded) controller.abort();
  };
  res.once("close", abortOnClose);
  if (stream) {
    res.type(streamType).set("Cache-Control", "no-store");
    res.flushHeaders();
  }
  const sendEvent = (event) => {
    if (stream && !controller.signal.aborted) res.write(JSON.stringify({ ...event, requestId }) + "\n");
  };
  const progress = (message) => {
    log(requestId, "chat.progress", { message });
    sendEvent({ type: "progress", message });
  };

  let design;
  try {
    log(requestId, "chat.started", { characters: question.length });
    progress("Understanding your request…");
    const conversation = interview(question, req.body?.conversationId);
    if (conversation && !conversation.design) {
      log(requestId, "design.question", { conversationId: conversation.conversationId });
      if (!stream) return res.json({ ...conversation, requestId });
      sendEvent({ type: "result", ...conversation });
      return res.end();
    }
    design = conversation?.design ?? detectDesign(question);
    if (design) log(requestId, "design.selected", design);
    else progress("Looking up jewelry references and preparing your answer…");
    const result = design
      ? await generatePreview(requestId, design, {
          signal: controller.signal,
          log: (event, details) => log(requestId, event, details),
          progress,
        })
      : await askRag(question, requestId, controller.signal);
    if (conversation) result.conversationId = conversation.conversationId;
    if (controller.signal.aborted) return;
    log(requestId, "chat.completed", { elapsedMs: Date.now() - started, sources: result.sources.length });
    if (!stream) return res.json({ ...result, requestId });
    sendEvent({ type: "result", ...result });
    return res.end();
  } catch (error) {
    if (controller.signal.aborted) {
      log(requestId, "chat.cancelled", { elapsedMs: Date.now() - started });
      return;
    }
    const code = error.code ?? error.cause?.code ?? error.name;
    log(requestId, "chat.failed", {
      step: code === "UNSUPPORTED_DESIGN" ? "design.validation" : design ? "blender" : "rag",
      code, elapsedMs: Date.now() - started,
      reason: error.message.split("\n")[0].slice(0, 250),
    });
    let message = "Jewelry answers are unavailable. Check that the Python RAG service and Qdrant are running.";
    if (code === "UNSUPPORTED_DESIGN") message = error.message;
    if (design) {
      message = "The 3D preview could not be generated. Try again and check the Blender logs if it persists.";
      if (code === "ENOENT") message = "Blender could not be found. Set BLENDER_PATH in the backend and restart it.";
      if (code === "DESIGN_BUSY") message = "Blender is busy with other previews. Try again in a moment.";
    }
    if (!stream) return res.status(code === "UNSUPPORTED_DESIGN" ? 422 : 503).json({ message, requestId });
    sendEvent({ type: "error", message });
    return res.end();
  } finally {
    res.off("close", abortOnClose);
  }
});

module.exports = router;
