const router = require("express").Router();

const ragBaseUrl = (process.env.RAG_API_URL || "http://127.0.0.1:8001").replace(/\/$/, "");

router.post("/", async (req, res) => {
  const question = typeof req.body?.question === "string" ? req.body.question.trim() : "";
  if (!question || question.length > 1000) {
    return res.status(400).json({ message: "Enter a question between 1 and 1000 characters." });
  }

  const controller = new AbortController();
  const abortOnClose = () => controller.abort();
  res.once("close", abortOnClose);

  try {
    const response = await fetch(`${ragBaseUrl}/v1/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, experience: "shecraft" }),
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45_000)]),
    });

    if (!response.ok) throw new Error(`RAG HTTP ${response.status}`);

    const result = await response.json();
    if (typeof result.answer !== "string" || !result.answer.trim()) {
      throw new Error("Empty RAG answer");
    }

    const sources = Array.isArray(result.text_sources) ? result.text_sources : [];
    return res.json({
      answer: result.answer,
      sources: sources.flatMap((source, index) => typeof source?.file_name === "string" ? [{
        reference: `T${index + 1}`,
        title: source.file_name,
        page: source.page_number ?? null,
        excerpt: typeof source.text === "string" ? source.text : "",
      }] : []),
    });
  } catch (error) {
    if (controller.signal.aborted) return;
    console.warn("SheCraft chat unavailable", error.message);
    return res.status(503).json({ message: "The jewelry assistant is temporarily unavailable." });
  } finally {
    res.off("close", abortOnClose);
  }
});

module.exports = router;
