import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { streamAssistant } from "../api/assistant";
import ChatbotWidget from "./ChatbotWidget";

jest.mock("../api/assistant");

beforeEach(() => {
  streamAssistant.mockReset();
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  Object.defineProperty(window, "crypto", { configurable: true, value: { randomUUID: () => "test-conversation" } });
});

test("a menu click and a typed edit use the same conversation and preserve the draft", async () => {
  const brief = {
    preferences: [{ field: "jewelry_type", value: "necklace", intent: "required" }],
    status: "collecting", option_field: "metal", options: ["Gold", "Silver"],
  };
  streamAssistant.mockResolvedValueOnce({ answer: "Which metal?", brief })
    .mockResolvedValueOnce({ answer: "Your name necklace brief is ready.", brief: {
      ...brief, options: [], status: "ready",
      preferences: [...brief.preferences, { field: "style", value: "name", intent: "required" },
        { field: "personalization", value: "Hala", intent: "required" }],
    } }).mockResolvedValueOnce({ answer: "Which birth month would you like?", brief: {
      ...brief, preferences: [...brief.preferences, { field: "style", value: "birthstone", intent: "required" }],
      option_field: "birth_month", options: ["February"],
    } });
  render(<ChatbotWidget />);
  fireEvent.click(screen.getByRole("button", { name: "Open jewelry assistant" }));
  const composer = screen.getByRole("textbox", { name: "Your question" });
  fireEvent.change(composer, { target: { value: "A silver 20-inch name necklace with Hala." } });
  fireEvent.click(screen.getByRole("button", { name: "necklace" }));
  expect(await screen.findByRole("button", { name: "Silver" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "necklace" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Send question" }));
  expect(await screen.findByText("Your name necklace brief is ready.")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Change style"));
  fireEvent.click(screen.getByRole("button", { name: "birthstone" }));
  expect(await screen.findByRole("button", { name: "February" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /explore birthstone/i })).toHaveAttribute("href", "/necklaces/birthstone");
  expect(streamAssistant.mock.calls.map(([, thread, message]) => [thread, message])).toEqual([
    ["test-conversation", "necklace"], ["test-conversation", "A silver 20-inch name necklace with Hala."],
    ["test-conversation", "Switch to birthstone"],
  ]);
});

test("restores focus and retries a failed question once without duplicating it", async () => {
  streamAssistant.mockRejectedValueOnce(new Error("Connection interrupted"))
    .mockResolvedValueOnce({ answer: "Use a ring sizer.", sources: [] });
  render(<ChatbotWidget />);
  const launcher = screen.getByRole("button", { name: "Open jewelry assistant" });
  fireEvent.click(launcher);
  expect(screen.getByRole("button", { name: "Close chat" })).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "How do I measure my ring size?" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection interrupted");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Use a ring sizer.")).toBeVisible();
  expect(screen.getAllByText("How do I measure my ring size?")).toHaveLength(1);
  expect(streamAssistant).toHaveBeenCalledTimes(2);
  fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
  expect(launcher).toHaveFocus();
  fireEvent.click(launcher);
  expect(screen.getByText("Use a ring sizer.")).toBeVisible();
});
