// Run from the repository root with node tools/test-3d-http.cjs.
const assert = require("node:assert/strict");
const { once } = require("node:events");
const { pathToFileURL } = require("node:url");
const { resolve } = require("node:path");
const express = require("../BackEnd/node_modules/express");

async function main() {
  const { GLTFLoader } = await import(pathToFileURL(resolve(
    __dirname, "../FrontEnd/shecraft-frontend/node_modules/three/examples/jsm/loaders/GLTFLoader.js"
  )).href);
  const app = express();
  app.use(express.json());
  app.use("/api/chat", require("../BackEnd/shecraft-backend/src/routes/chatRoutes"));
  const server = app.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = "http://127.0.0.1:" + server.address().port + "/api/chat";
  async function askTurn(question, conversationId) {
    const response = await fetch(base, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify({ question, conversationId }), signal: AbortSignal.timeout(50_000),
    });
    return (await response.text()).trim().split("\n").map(JSON.parse);
  }
  async function ask(question) {
    let events = await askTurn(question);
    for (let step = 0; step < 10; step++) {
      const result = events.at(-1);
      if (!result.options) return events;
      const answer = result.options.includes("Generate it") ? "Generate it" :
        result.options.includes("Solitaire") ? "Solitaire" : result.options[0];
      const next = await askTurn(answer, result.conversationId);
      events = [...events.filter(event => event.type !== "result"), ...next];
    }
    throw new Error("Design interview did not complete");
  }
  try {
    for (const [command, metal, gem, meshCount] of [
      ["Generate a gold sapphire ring", "Yellow gold", "Sapphire", 11],
      ["Make a rose gold ruby ring", "Rose gold", "Ruby", 11],
      ["Make a silver ruby solitaire ring US size 8", "Silver", "Ruby", 11],
      ["Create a silver emerald bracelet", "Silver", "Emerald", 11],
      ["Make a gold diamond ring", "Yellow gold", "Diamond", 11],
      ["Make a plain silver bracelet", "Silver", null, 1],
      ["Make a ring without a stone", "Yellow gold", null, 1],
    ]) {
      const events = await ask(command);
      const result = events.at(-1);
      assert.equal(result.type, "result", JSON.stringify(result));
      assert(events.some(event => event.message?.startsWith("Generating your")));
      assert(result.requestId);
      const response = await fetch(base + "/models/" + result.design.id);
      assert.equal(response.status, 200);
      const buffer = await response.arrayBuffer();
      const { scene } = await new GLTFLoader().parseAsync(buffer, "");
      const materials = new Set();
      let count = 0;
      scene.traverse(node => {
        if (!node.isMesh) return;
        count++;
        materials.add(node.material.name);
        const positions = node.geometry.attributes.position;
        for (const value of positions.array) assert(Number.isFinite(value));
      });
      assert.equal(count, meshCount);
      assert.deepEqual([...materials].sort(), [metal, gem].filter(Boolean).sort());
      console.log("PASS", command, count + " meshes", buffer.byteLength + " bytes");
    }
    const concurrent = await Promise.all([
      ask("Make a silver ring"), ask("Make a gold bracelet"), ask("Make a ruby ring"),
    ]);
    const outcomes = concurrent.map(events => events.at(-1));
    assert.equal(outcomes.filter(event => event.type === "result").length, 2);
    assert.equal(outcomes.filter(event => event.type === "error" && /busy/.test(event.message)).length, 1);
    console.log("PASS concurrent requests respect the two-worker limit");
  } finally {
    server.close();
    server.closeAllConnections();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
