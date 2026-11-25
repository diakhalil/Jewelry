const assert = require("node:assert/strict");
const { test } = require("node:test");
const { interview } = require("../shecraft-backend/src/designConversation");

test("the customer's tennis request leads to questions, confirmation, and a complete design", () => {
  let turn = interview("want to change the color of the diamond ad also add more diamodsn make it a tennis ring");
  const id = turn.conversationId;
  assert.match(turn.answer, /metal/i);
  assert(!turn.design);
  turn = interview("Rose gold", id);
  assert.match(turn.answer, /color/i);
  turn = interview("Pink", id);
  assert.match(turn.answer, /How many/i);
  turn = interview("20 stones", id);
  assert.match(turn.answer, /US ring size/i);
  turn = interview("7", id);
  assert.match(turn.answer, /Shall I generate/i);
  assert(!turn.design);
  turn = interview("Generate it", id);
  assert.deepEqual(turn.design, {
    kind: "ring", style: "tennis", metal: "rose-gold", gem: "diamond",
    color: "pink", count: 20, size: 7, diameter: 60,
  });
  turn = interview("change the diamond color", id);
  assert.match(turn.answer, /color/i);
  assert.match(interview("Blue", id).answer, /blue diamonds/i);
  assert(!interview("Blue", id).design);
});

test("invalid counts and sizes keep asking, knowledge questions preserve the design", () => {
  const start = interview("Make a gold tennis ring with blue diamonds");
  const id = start.conversationId;
  assert.match(interview("100 stones", id).answer, /8 to 32/);
  interview("12", id);
  assert.match(interview("US size 20", id).answer, /3 to 13/);
  assert.equal(interview("How do I measure my ring size?", id), null);
  assert.match(interview("US size 6.5", id).answer, /6.5/);
  assert(interview("Generate it", id).design);
  assert.equal(interview("Cancel", id).conversationId, null);
});

test("two customers have separate designs and new requests need missing details", () => {
  const first = interview("Make a ring");
  const second = interview("Make a silver bracelet");
  assert.notEqual(first.conversationId, second.conversationId);
  assert.match(interview("Tennis", first.conversationId).answer, /metal/);
  assert.match(interview("Plain", second.conversationId).answer, /diameter/);
});

test("a short choice after cancellation and expired sessions stay in the design flow", () => {
  const start = interview("Make a ring");
  const cancelled = interview("Cancel", start.conversationId);
  assert.deepEqual(cancelled.options, ["Design a ring", "Design a bracelet"]);
  const resumed = interview("Silver", cancelled.conversationId);
  assert.match(resumed.answer, /ring or a bracelet/);
  assert.match(interview("Ring", resumed.conversationId).answer, /style/);
  const expired = interview("US size 8", "missing-session");
  assert.match(expired.answer, /previous design session ended/);
  assert(expired.conversationId);
  assert.equal(interview("What is the birthstone for May?", expired.conversationId), null);
});
