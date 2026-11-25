const apiUrl = (process.env.REACT_APP_API_URL || "http://localhost:5000").replace(/\/$/, "");
const streamType = "application/x-ndjson";

export const jewelryPreviewUrl = (id) => `${apiUrl}/api/chat/models/${encodeURIComponent(id)}`;

function chatError(message, requestId) {
  return Object.assign(new Error(message), { name: "SheCraftChatError", requestId });
}

async function readReply(response, onProgress) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "progress") onProgress(event.message);
        if (event.type === "error") throw chatError(event.message, event.requestId);
        if (event.type === "result") result = event;
      }
    }
    return result;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function askSheCraft(question, signal, onProgress = () => {}, conversationId = null) {
  let response;
  try {
    response = await fetch(`${apiUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: streamType },
      body: JSON.stringify({ question, conversationId }),
      signal,
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw chatError("Could not reach the SheCraft backend. Check that Express is running.");
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw chatError(error.message || "The jewelry assistant is temporarily unavailable.", error.requestId);
  }
  const data = response.headers.get("content-type")?.includes(streamType) && response.body
    ? await readReply(response, onProgress)
    : await response.json();
  if (typeof data?.answer !== "string" || !data.answer.trim()) {
    throw chatError("The reply was interrupted. Try your question again.", data?.requestId);
  }
  return {
    answer: data.answer.trim(),
    sources: Array.isArray(data.sources) ? data.sources : [],
    design: data.design,
    conversationId: data.conversationId,
    options: data.options,
  };
}
