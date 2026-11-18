const apiUrl = (process.env.REACT_APP_API_URL || "http://localhost:5000").replace(/\/$/, "");

export async function askSheCraft(question, signal) {
  const response = await fetch(`${apiUrl}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });

  if (!response.ok) {
    throw new Error("Chat request failed");
  }

  const data = await response.json();
  if (typeof data.answer !== "string" || !data.answer.trim()) {
    throw new Error("Chat response was empty");
  }
  return {
    answer: data.answer.trim(),
    sources: Array.isArray(data.sources) ? data.sources : [],
  };
}
