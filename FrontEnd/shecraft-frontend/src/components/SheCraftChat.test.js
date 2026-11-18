import { fireEvent, render, screen } from "@testing-library/react";
import SheCraftChat from "./SheCraftChat";
import { askSheCraft } from "../api/chat";

jest.mock("../api/chat");

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
  expect(screen.getByRole("status")).toHaveTextContent("Looking up your question");
  expect(askSheCraft).toHaveBeenCalledWith(question, expect.any(AbortSignal));
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
