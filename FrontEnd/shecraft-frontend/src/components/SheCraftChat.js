import { useEffect, useRef, useState } from "react";
import { LoaderCircle, MessageCircle, Send, X } from "lucide-react";
import { askSheCraft } from "../api/chat";
import "../styles/chat.css";

const prompts = [
  "How do I measure my ring size?",
  "What is the birthstone for May?",
  "How should I care for pearl jewelry?",
];

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
const maxQuestionLength = 1000;

function ChatMessage({ message }) {
  const isAssistant = message.role === "assistant";

  return (
    <article className={`sc-chat-message sc-chat-message--${message.role}`}>
      <div className="sc-chat-byline">
        <span>{isAssistant ? "SheCraft" : "You"}</span>
        <time dateTime={message.time.toISOString()}>{timeFormat.format(message.time)}</time>
      </div>
      <p>{message.text}</p>
      {message.sources?.length > 0 && (
        <details className="sc-chat-sources">
          <summary>Sources ({message.sources.length})</summary>
          <ul>
            {message.sources.map((source, index) => (
              <li key={index}>
                <strong>{source.reference} · {source.title}{source.page ? ` · p. ${source.page}` : ""}</strong>
                {source.excerpt && <p>{source.excerpt}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

export default function SheCraftChat() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [isPending, setIsPending] = useState(false);
  const [failedQuestion, setFailedQuestion] = useState("");
  const dialogRef = useRef(null);
  const logRef = useRef(null);
  const requestRef = useRef(null);

  useEffect(() => () => requestRef.current?.abort(), []);

  useEffect(() => {
    if (isOpen && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [isOpen, messages, isPending, failedQuestion]);

  useEffect(() => {
    const viewport = window.visualViewport;
    if (!isOpen || !viewport) return;

    const dialog = dialogRef.current;
    function fitViewport() {
      dialog.style.setProperty("--sc-chat-viewport-height", `${viewport.height}px`);
      dialog.style.setProperty("--sc-chat-viewport-bottom",
        `${Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)}px`);
    }

    fitViewport();
    viewport.addEventListener("resize", fitViewport);
    viewport.addEventListener("scroll", fitViewport);
    return () => {
      viewport.removeEventListener("resize", fitViewport);
      viewport.removeEventListener("scroll", fitViewport);
    };
  }, [isOpen]);

  function openChat() {
    dialogRef.current.showModal();
    setIsOpen(true);
  }

  function closeChat() {
    dialogRef.current.close();
  }

  async function send(question, retry = false) {
    const text = question.trim();
    if (!text || text.length > maxQuestionLength || requestRef.current) return;

    const request = new AbortController();
    requestRef.current = request;
    setIsPending(true);
    setFailedQuestion("");
    if (!retry) {
      setDraft("");
      setMessages((previous) => [...previous, { role: "user", text, time: new Date() }]);
    }

    try {
      const result = await askSheCraft(text, request.signal);
      setMessages((previous) => [...previous, {
        role: "assistant",
        text: result.answer,
        sources: result.sources,
        time: new Date(),
      }]);
    } catch {
      if (!request.signal.aborted) setFailedQuestion(text);
    } finally {
      if (!request.signal.aborted) {
        requestRef.current = null;
        setIsPending(false);
      }
    }
  }

  return (
    <>
      <button
        type="button"
        className="sc-button sc-chat-launcher"
        aria-haspopup="dialog"
        aria-controls="shecraft-chat"
        aria-expanded={isOpen}
        onClick={openChat}
      >
        <MessageCircle size={20} aria-hidden="true" />
        Ask SheCraft
      </button>

      <dialog
        id="shecraft-chat"
        className="sc-chat"
        aria-labelledby="shecraft-chat-title"
        ref={dialogRef}
        onClose={() => setIsOpen(false)}
      >
        <header className="sc-chat-header">
          <span className="sc-chat-avatar"><MessageCircle size={21} aria-hidden="true" /></span>
          <div>
            <h2 id="shecraft-chat-title">Ask SheCraft</h2>
            <p>Jewelry design assistant</p>
          </div>
          <button type="button" className="sc-chat-close" aria-label="Close chat" onClick={closeChat}>
            <X size={21} aria-hidden="true" />
          </button>
        </header>

        <div className="sc-chat-scroll" ref={logRef} tabIndex={0} aria-label="Chat conversation">
          <div className="sc-chat-message sc-chat-message--assistant">
            <div className="sc-chat-byline">SheCraft</div>
            <p>Ask me about gemstones, jewelry sizing, or caring for a piece you love.</p>
          </div>

          {messages.length === 0 && (
            <div className="sc-chat-prompts" aria-label="Suggested questions">
              {prompts.map((prompt) => (
                <button type="button" key={prompt} onClick={() => { logRef.current.focus(); send(prompt); }}>
                  {prompt}
                </button>
              ))}
            </div>
          )}

          <div role="log" aria-label="Messages" className="sc-chat-messages">
            {messages.map((message, index) => <ChatMessage key={index} message={message} />)}
          </div>
          <div role="status">
            {isPending && (
              <p className="sc-chat-status"><LoaderCircle size={16} aria-hidden="true" /> Looking up your question…</p>
            )}
          </div>
          {failedQuestion && (
            <div className="sc-chat-error">
              <p role="alert">The jewelry assistant is temporarily unavailable. Please try again.</p>
              <button type="button" onClick={() => { logRef.current.focus(); send(failedQuestion, true); }}>
                Try again
              </button>
            </div>
          )}
        </div>

        <footer className="sc-chat-footer">
          <form className="sc-chat-form" onSubmit={(event) => { event.preventDefault(); send(draft); }}>
            <input
              aria-label="Your jewelry question"
              placeholder="Ask a jewelry question"
              maxLength={maxQuestionLength}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="sc-button" disabled={isPending || !draft.trim()} aria-label="Send message">
              <Send size={18} aria-hidden="true" />
            </button>
          </form>
        </footer>
      </dialog>
    </>
  );
}
