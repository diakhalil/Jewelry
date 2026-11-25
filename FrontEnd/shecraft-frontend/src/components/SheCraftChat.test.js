import { act, fireEvent, render, screen } from "@testing-library/react";
import SheCraftChat from "./SheCraftChat";
import { askSheCraft } from "../api/chat";

jest.mock("../api/chat");
jest.mock("react-router-dom", () => ({
  Link: ({ to, children, onClick, className }) => (
    <a href={to} className={className} onClick={(event) => { event.preventDefault(); onClick(); }}>
      {children}
    </a>
  ),
}), { virtual: true });

beforeEach(() => {
  askSheCraft.mockReset();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
});

function openChat() {
  render(<SheCraftChat />);
  const launcher = screen.getByRole("button", { name: "Ask SheCraft" });
  fireEvent.click(launcher);
  expect(launcher).toHaveAttribute("aria-expanded", "true");
  expect(screen.queryByRole("navigation", { name: "Browse collections" })).not.toBeInTheDocument();
  return launcher;
}

test("sends a suggested question and shows an answer with sources", async () => {
  askSheCraft.mockResolvedValue({
    answer: "Sizing guide response [T1]",
    sources: [{ reference: "T1", title: "Sizing guide.pdf", page: 2, excerpt: "Measure before ordering." }],
  });
  const launcher = openChat();
  const question = "How do I measure my ring size?";

  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: question }));
  expect(screen.getByRole("status")).toHaveTextContent("Sending your request");
  expect(askSheCraft).toHaveBeenCalledWith(question, expect.any(AbortSignal), expect.any(Function), null);
  expect(await screen.findByText("Sizing guide response [T1]")).toBeVisible();
  expect(screen.getByText("Sources (1)")).toBeVisible();
  expect(screen.getByText("T1 · Sizing guide.pdf · p. 2")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
  expect(launcher).toHaveAttribute("aria-expanded", "false");
});

test("retries a failed typed question without duplicating it", async () => {
  askSheCraft.mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce({
    answer: "Pearl care guide response",
    sources: [],
  });
  openChat();
  const input = screen.getByRole("textbox", { name: "Your jewelry question" });
  fireEvent.change(input, { target: { value: "  How do I clean pearls?  " } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");

  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("Pearl care guide response")).toBeVisible();
  expect(askSheCraft).toHaveBeenCalledTimes(2);
  expect(screen.getAllByText("How do I clean pearls?")).toHaveLength(1);
});

test("closes with Escape or the backdrop and restores focus without losing the draft", () => {
  const launcher = openChat();
  const dialog = screen.getByRole("dialog", { name: "Ask SheCraft" });
  const input = screen.getByRole("textbox", { name: "Your jewelry question" });
  expect(screen.getByRole("button", { name: "Close chat" })).toHaveFocus();
  fireEvent.change(input, { target: { value: "Which metal suits sapphires?" } });
  fireEvent.pointerDown(input);
  fireEvent.click(dialog);
  expect(dialog).toHaveAttribute("open");

  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(dialog).not.toHaveAttribute("open");
  expect(launcher).toHaveFocus();

  fireEvent.click(launcher);
  expect(input).toHaveValue("Which metal suits sapphires?");
  fireEvent.pointerDown(dialog);
  fireEvent.click(dialog);
  expect(dialog).not.toHaveAttribute("open");
  expect(launcher).toHaveAttribute("aria-expanded", "false");
  expect(launcher).toHaveFocus();
});

test("shows a generation stage and links to the finished preview", async () => {
  let complete;
  askSheCraft.mockImplementation((_question, _signal, progress) => {
    progress("Generating your bracelet…");
    return new Promise((resolve) => { complete = resolve; });
  });
  render(<SheCraftChat />);
  fireEvent.click(screen.getByRole("button", { name: "Ask SheCraft" }));
  fireEvent.click(screen.getByRole("button", { name: "Make a silver bracelet" }));
  expect(screen.getByRole("status")).toHaveTextContent("Generating your bracelet");
  await act(async () => complete({
    answer: "Your silver bracelet is ready to preview in 3D.",
    sources: [],
    design: { id: "design-1", kind: "bracelet", title: "Silver bracelet" },
  }));
  const link = screen.getByRole("link", { name: "View your bracelet in 3D" });
  expect(link).toHaveAttribute("href", "/designs/design-1");
  fireEvent.click(link);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("keeps the design conversation and sends the customer's choice", async () => {
  askSheCraft.mockResolvedValueOnce({
    answer: "What color would you like the diamonds to be?",
    conversationId: "conversation-1",
    options: ["Pink", "Blue"],
  }).mockResolvedValueOnce({
    answer: "How many stones would you like?",
    conversationId: "conversation-1",
    options: ["20 stones"],
  });
  openChat();
  fireEvent.click(screen.getByRole("button", { name: "Generate a gold sapphire ring" }));
  fireEvent.click(await screen.findByRole("button", { name: "Pink" }));
  expect(await screen.findByText("How many stones would you like?")).toBeVisible();
  expect(askSheCraft).toHaveBeenLastCalledWith(
    "Pink", expect.any(AbortSignal), expect.any(Function), "conversation-1"
  );
});
