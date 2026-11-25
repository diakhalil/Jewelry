const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const { once } = require("node:events");
const { test } = require("node:test");
const express = require("express");
const { detectDesign, generatePreview, previewModel } = require("../shecraft-backend/src/jewelryPreview");

test("recognizes supported commands and keeps knowledge questions in RAG", () => {
  for (const [question, expected] of [
    ["generate for me a ring", { kind: "ring", metal: "gold", gem: "sapphire" }],
    ["Please make a plain silver bracelet", { kind: "bracelet", metal: "silver", gem: "none" }],
    ["Could you create a rose gold ruby ring?", { kind: "ring", metal: "rose-gold", gem: "ruby" }],
    ["MAKE A GOLD DIAMOND BANGLE", { kind: "bracelet", metal: "gold", gem: "diamond" }],
    ["How do I measure my ring size?", null],
    ["Show me how to make a ring", null],
  ]) assert.deepEqual(detectDesign(question), expected, question);
});

test("rejects unsupported features and ambiguous combinations", () => {
  for (const question of [
    "Make a platinum ring", "Make an amethyst ring", "Make a white gold ring",
    "Make a ring size 7", "Make an engraved ring", "Make a ring and bracelet",
    "Make a gold silver ring", "Make a ruby emerald ring", "Make an oval ring",
  ]) assert.throws(() => detectDesign(question), { code: "UNSUPPORTED_DESIGN" }, question);
});

test("HTTP validation, streamed errors, preview expiry, and unavailable RAG", async () => {
  process.env.RAG_API_URL = "http://127.0.0.1:1";
  const app = express();
  app.use(express.json());
  app.use("/api/chat", require("../shecraft-backend/src/routes/chatRoutes"));
  const server = app.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = "http://127.0.0.1:" + server.address().port + "/api/chat";
  const post = (body, stream = false) => fetch(base, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: stream ? "application/x-ndjson" : "application/json" },
    body: JSON.stringify(body),
  });
  try {
    for (const question of [undefined, null, 42, "", "   ", "a".repeat(1001)]) {
      const response = await post({ question });
      assert.equal(response.status, 400);
      assert.match((await response.json()).requestId, /^[a-f0-9-]{36}$/);
    }
    const unsupported = await post({ question: "Make a platinum ring" });
    assert.equal(unsupported.status, 200);
    assert((await unsupported.json()).conversationId);
    const stream = await post({ question: "Make a platinum ring" }, true);
    const events = (await stream.text()).trim().split("\n").map(JSON.parse);
    assert.deepEqual(events.map(event => event.type), ["progress", "result"]);
    assert.equal(events[0].requestId, events[1].requestId);
    assert.match(events[1].answer, /not available/);
    assert.equal((await fetch(base + "/models/" + randomUUID())).status, 404);
    assert.equal((await fetch(base + "/models/invalid")).status, 404);
    assert.equal((await fetch(base + "/health")).status, 503);
    assert.equal((await post({ question: "How should I clean silver?" })).status, 503);
  } finally {
    server.close();
    server.closeAllConnections();
  }
});

test("missing Blender and cancellation return worker slots for retries", async () => {
  const old = process.env.BLENDER_PATH;
  process.env.BLENDER_PATH = "__shecraft_missing_blender__";
  const options = { signal: new AbortController().signal, log: () => {}, progress: () => {} };
  try {
    for (let i = 0; i < 3; i++) {
      await assert.rejects(generatePreview(randomUUID(), detectDesign("Make a ring"), options), { code: "ENOENT" });
    }
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(generatePreview(randomUUID(), detectDesign("Make a ring"), {
      ...options, signal: controller.signal,
    }), { name: "AbortError" });
    assert.equal(previewModel(randomUUID()), null);
  } finally {
    if (old === undefined) delete process.env.BLENDER_PATH;
    else process.env.BLENDER_PATH = old;
  }
});
