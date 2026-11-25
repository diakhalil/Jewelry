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
  let conversationId;
  async function say(question) {
    const response = await fetch(base, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify({ question, conversationId }),
      signal: AbortSignal.timeout(50_000),
    });
    const events = (await response.text()).trim().split("\n").map(JSON.parse);
    const result = events.at(-1);
    assert.equal(result.type, "result", JSON.stringify(result));
    conversationId = result.conversationId;
    return result;
  }
  async function verify(result, count, color) {
    assert.equal(result.design.count, count);
    assert.equal(result.design.color, color);
    const response = await fetch(base + "/models/" + result.design.id);
    const { scene } = await new GLTFLoader().parseAsync(await response.arrayBuffer(), "");
    const gems = [];
    scene.traverse(node => {
      if (node.isMesh && node.material.name === "Diamond") gems.push(node);
    });
    assert.equal(gems.length, count);
    scene.updateMatrixWorld(true);
    const ys = gems.map(mesh => mesh.matrixWorld.elements[13]);
    assert(Math.min(...ys) < -0.005 && Math.max(...ys) > 0.005, "Stones should surround the ring");
    for (const gem of gems) assert([...gem.matrixWorld.elements].every(Number.isFinite));
    return gems[0].material.color.toArray();
  }
  try {
    let reply = await say("want to change the color of the diamond ad also add more diamodsn make it a tennis ring");
    assert.match(reply.answer, /metal/i);
    assert(!reply.design);
    assert.match((await say("Rose gold")).answer, /color/i);
    assert.match((await say("Pink")).answer, /How many/i);
    assert.match((await say("20 stones")).answer, /size/i);
    assert.match((await say("US size 7")).answer, /Shall I generate/i);
    const pink = await verify(await say("Generate it"), 20, "pink");
    assert.match((await say("change the diamond color and add more diamonds")).answer, /color/i);
    assert.match((await say("Blue")).answer, /How many/i);
    assert.match((await say("28 stones")).answer, /Shall I generate/i);
    const blue = await verify(await say("Generate it"), 28, "blue");
    assert.notDeepEqual(pink, blue);
    console.log("PASS customer conversation, retained size/metal, confirmation, 20 pink diamonds, and revision to 28 blue diamonds");
  } finally {
    server.close();
    server.closeAllConnections();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
