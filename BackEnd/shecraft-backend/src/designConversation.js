const { randomUUID } = require("node:crypto");

const sessions = new Map();
const choices = {
  kind: ["Ring", "Bracelet"],
  style: ["Solitaire", "Tennis", "Plain"],
  metal: ["Gold", "Silver", "Rose gold"],
  gem: ["Diamond", "Sapphire", "Ruby", "Emerald"],
  color: ["White", "Pink", "Blue", "Yellow", "Green"],
  count: ["12 stones", "20 stones", "28 stones"],
  size: ["US size 6", "US size 7", "US size 8"],
  diameter: ["55 mm", "60 mm", "65 mm"],
};
const questions = {
  kind: "Would you like a ring or a bracelet?",
  style: "Which style would you like: a single gemstone, a tennis band with stones around it, or a plain band?",
  metal: "Which metal would you like?",
  gem: "Which gemstone would you like?",
  color: "What color would you like the diamonds to be?",
  count: "How many stones would you like around the tennis band? Choose 8 to 32.",
  size: "What US ring size should I use? I can preview sizes 3 to 13, including half sizes.",
  diameter: "What inner diameter should the bracelet have? Choose 50 to 80 mm.",
};

function nextField(draft) {
  for (const field of ["kind", "style", "metal", "gem", "color", "count", "size", "diameter"]) {
    if ((field === "gem" && draft.style === "plain") ||
        (field === "color" && draft.gem !== "diamond") ||
        (field === "count" && draft.style !== "tennis") ||
        (field === "size" && draft.kind !== "ring") ||
        (field === "diameter" && draft.kind !== "bracelet")) continue;
    if (draft[field] === undefined) return field;
  }
  return "confirm";
}

function interview(question, sessionId) {
  const now = Date.now();
  for (const [id, session] of sessions) if (session.expires < now) sessions.delete(id);
  const previous = typeof sessionId === "string" ? sessions.get(sessionId) : null;
  const knowledge = /^(?:how|what|why|when|where|which)\b/i.test(question) &&
    !/\b(?:change|make|want|design|generate)\b/i.test(question);
  const designIntent = /\b(?:generate|make|create|design|change|add|tennis|solitaire|build|render)\b/i.test(question) ||
    /\bwant\b.*\b(?:ring|bracelet|diamond|color|colour)\b/i.test(question);
  const designReply = /^(?:ring|bracelet|bangle|gold|silver|rose[ -]gold|diamond|sapphire|ruby|emerald|white|clear|pink|blue|yellow|green|purple|plain|yes|confirm|cancel|stop|start over|new design|\d+(?:\.5)?(?:\s*(?:stones?|diamonds?|mm))?|(?:us\s+)?size\s*\d+(?:\.5)?)[.! ]*$/i.test(question.trim());
  const expired = Boolean(sessionId) && !previous;
  if ((!previous && !designIntent && !designReply && !expired) || knowledge) return null;
  if (/^(?:cancel|stop|cancel design)$/i.test(question.trim())) {
    sessions.delete(sessionId);
    return { answer: "Design cancelled. What would you like to create next?", sources: [], conversationId: null,
      options: ["Design a ring", "Design a bracelet"] };
  }
  const id = previous ? sessionId : randomUUID();
  const draft = /\b(?:start over|new design)\b/i.test(question) ? {} : { ...previous?.draft };
  let notice = expired ? "Your previous design session ended. Let’s collect the details again. " : "";
  const q = question.toLowerCase();
  const kind = q.match(/\b(ring|bracelet|bangle)\b/)?.[1];
  if (kind) {
    const selected = kind === "ring" ? "ring" : "bracelet";
    if (draft.kind && draft.kind !== selected) { delete draft.size; delete draft.diameter; }
    draft.kind = selected;
  }
  if (/\btennis\b/.test(q)) { draft.style = "tennis"; draft.gem = "diamond"; }
  if (/\b(?:solitaire|single gemstone)\b/.test(q)) draft.style = "solitaire";
  if (/\b(?:plain|no stones?|without (?:a )?stones?)\b/.test(q)) draft.style = "plain";
  const metal = q.match(/\brose[ -]gold\b|\bsilver\b|\bgold\b/)?.[0];
  if (metal) draft.metal = metal.replace(" ", "-");
  const gem = q.match(/\b(diamond|sapphire|ruby|emerald)s?\b/)?.[1];
  if (gem) {
    draft.gem = gem;
    if (draft.style === "plain") delete draft.style;
  }
  const color = q.match(/\b(white|clear|pink|blue|yellow|green)\b/)?.[1];
  if (color) draft.color = color === "clear" ? "white" : color;
  else if (/\b(?:change|different)\b.*\b(?:color|colour)\b|\b(?:color|colour)\b.*\bchange\b/.test(q)) delete draft.color;
  if (/\bmore\b.*\b(?:diamonds?|stones?)\b/.test(q)) {
    delete draft.count;
    if (draft.style !== "tennis") delete draft.style;
  }
  const pending = previous ? nextField(previous.draft) : null;
  const count = q.match(/\b(\d+)\s*(?:stones?|diamonds?|gems?)\b/)?.[1] ??
    (pending === "count" && /^\d+$/.test(q.trim()) ? q.trim() : undefined);
  if (count !== undefined) {
    if (+count >= 8 && +count <= 32) draft.count = +count;
    else notice = "The tennis preview supports 8 to 32 stones. ";
  }
  const size = q.match(/\b(?:us\s+)?size\s*(\d+(?:\.5)?)\b/)?.[1] ??
    (pending === "size" && /^\d+(?:\.5)?$/.test(q.trim()) ? q.trim() : undefined);
  if (size !== undefined) {
    if (+size >= 3 && +size <= 13) draft.size = +size;
    else notice = "Please choose a US size from 3 to 13. ";
  }
  const diameter = q.match(/\b(\d+)\s*mm\b/)?.[1];
  if (diameter !== undefined) {
    if (+diameter >= 50 && +diameter <= 80) draft.diameter = +diameter;
    else notice = "Please choose an inner diameter from 50 to 80 mm. ";
  }
  if (/\b(?:platinum|amethyst|opal|pearl|titanium|white gold|engraving|engraved)\b/.test(q)) {
    notice = "That option is not available in this preview. ";
    // Ask again at the relevant field instead of silently substituting.
    if (/\b(?:platinum|titanium|white gold)\b/.test(q)) delete draft.metal;
    else if (/\b(?:amethyst|opal|pearl)\b/.test(q)) delete draft.gem;
  }
  const field = nextField(draft);
  if (sessions.size >= 200 && !previous) sessions.delete(sessions.keys().next().value);
  sessions.set(id, { draft, expires: now + 60 * 60 * 1000 });
  if (field !== "confirm") return {
    answer: notice + questions[field], sources: [], conversationId: id, options: choices[field],
  };
  const summary = [
    draft.metal.replace("-", " "), draft.style, draft.kind,
    draft.style === "plain" ? "no stones" : draft.gem === "diamond" ? draft.color + " diamonds" : draft.gem,
    draft.style === "tennis" ? draft.count + " stones" : null,
    draft.kind === "ring" ? "US size " + draft.size : draft.diameter + " mm inner diameter",
  ].filter(Boolean).join(", ");
  if (previous && !notice && /^(?:yes|confirm|generate|generate it|build it|yes generate it)[.! ]*$/i.test(question.trim())) {
    return { conversationId: id, design: {
      kind: draft.kind, style: draft.style, metal: draft.metal,
      gem: draft.style === "plain" ? "none" : draft.gem,
      color: draft.color ?? "white", count: draft.count ?? 1,
      size: draft.size ?? 7, diameter: draft.diameter ?? 60,
    } };
  }
  return { answer: notice + "Your design is " + summary + ". Shall I generate this model, or would you like to change anything?",
    sources: [], conversationId: id, options: ["Generate it", "Start over", "Cancel"] };
}

module.exports = { interview };
