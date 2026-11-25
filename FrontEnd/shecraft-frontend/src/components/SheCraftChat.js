import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, LoaderCircle, MessageCircle, Send, X } from "lucide-react";
import { Link } from "react-router-dom";
import { askSheCraft } from "../api/chat";
import "../styles/chat.css";

const prompts = [
  "Generate a gold sapphire ring",
  "Make a silver bracelet",
  "How do I measure my ring size?",
];

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
const maxQuestionLength = 1000;

function ChatMessage({ message, onPreview }) {
  const isAssistant = message.role === "assistant";

  return (
    <article className={`sc-chat-message sc-chat-message--${message.role}`}>
      <div className="sc-chat-byline">
        <span>{isAssistant ? "SheCraft" : "You"}</span>
        <time dateTime={message.time.toISOString()}>{timeFormat.format(message.time)}</time>
      </div>
      <p>{message.text}</p>
      {message.design && (
        <Link
          className="sc-button sc-chat-preview"
          to={`/designs/${message.design.id}`}
          state={{ title: message.design.title }}
          onClick={onPreview}
        >
          View your {message.design.kind} in 3D
          <ArrowUpRight size={16} aria-hidden="true" />
        </Link>
      )}
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
  const [progress, setProgress] = useState("");
  const [failure, setFailure] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const isPending = Boolean(progress);
  const launcherRef = useRef(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const logRef = useRef(null);
  const requestRef = useRef(null);
  const pressedBackdrop = useRef(false);

  useEffect(() => () => requestRef.current?.abort(), []);

  useEffect(() => {
    if (isOpen && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [isOpen, messages, progress, failure]);

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
    pressedBackdrop.current = false;
    dialogRef.current.showModal();
    setIsOpen(true);
    closeRef.current.focus();
  }

  function closeChat() {
    dialogRef.current.close();
    setIsOpen(false);
    launcherRef.current.focus();
  }

  async function send(question, retry = false) {
    const text = question.trim();
    if (!text || text.length > maxQuestionLength || requestRef.current) return;

    const request = new AbortController();
    requestRef.current = request;
    setProgress("Sending your request…");
    setFailure(null);
    if (!retry) {
      setDraft("");
      setMessages((previous) => [...previous, { role: "user", text, time: new Date() }]);
    }

    try {
      const result = await askSheCraft(text, request.signal, setProgress, conversationId);
      if (result.conversationId !== undefined) setConversationId(result.conversationId);
      setMessages((previous) => [...previous, {
        role: "assistant",
        text: result.answer,
        sources: result.sources,
        design: result.design,
        options: result.options,
        time: new Date(),
      }]);
    } catch (error) {
      if (!request.signal.aborted) {
        setFailure({
          question: text,
          message: error.name === "SheCraftChatError" ? error.message : null,
          requestId: error.requestId,
        });
      }
    } finally {
      if (!request.signal.aborted) {
        requestRef.current = null;
        setProgress("");
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
        ref={launcherRef}
      >
        <MessageCircle size={20} aria-hidden="true" />
        Ask SheCraft
      </button>

      <dialog
        id="shecraft-chat"
        className="sc-chat"
        aria-labelledby="shecraft-chat-title"
        ref={dialogRef}
        onCancel={(event) => { event.preventDefault(); closeChat(); }}
        onPointerDown={(event) => {
          pressedBackdrop.current = event.target === event.currentTarget;
        }}
        onClick={(event) => {
          if (pressedBackdrop.current && event.target === event.currentTarget) closeChat();
          pressedBackdrop.current = false;
        }}
      >
        <header className="sc-chat-header">
          <span className="sc-chat-avatar"><MessageCircle size={21} aria-hidden="true" /></span>
          <div>
            <h2 id="shecraft-chat-title">Ask SheCraft</h2>
            <p>Jewelry design assistant</p>
          </div>
          <button type="button" className="sc-chat-close" aria-label="Close chat" onClick={closeChat} ref={closeRef}>
            <X size={21} aria-hidden="true" />
          </button>
        </header>

        <div className="sc-chat-scroll" ref={logRef} tabIndex={0} aria-label="Chat conversation">
          <div className="sc-chat-message sc-chat-message--assistant">
            <div className="sc-chat-byline">SheCraft</div>
            <p>Generate a ring or bracelet in 3D, or ask me about gemstones, sizing, and jewelry care.</p>
          </div>

          {messages.length === 0 && (
            <div className="sc-chat-prompts" aria-label="Suggested questions">
              {prompts.map((prompt) => (
                <button type="button" key={prompt} onClick={() => { logRef.current.focus(); send(prompt); }}>
                  {prompt}
                  <ArrowUpRight size={15} aria-hidden="true" />
                </button>
              ))}
            </div>
          )}

          <div role="log" aria-label="Messages" className="sc-chat-messages">
            {messages.map((message, index) => <ChatMessage key={index} message={message} onPreview={closeChat} />)}
          </div>
          {messages.at(-1)?.options?.length > 0 && (
            <div className="sc-chat-prompts" aria-label="Design choices">
              {messages.at(-1).options.map((option) => (
                <button key={option} type="button" disabled={isPending} onClick={() => send(option)}>
                  {option}
                </button>
              ))}
            </div>
          )}
          <div role="status">
            {isPending && (
              <p className="sc-chat-status"><LoaderCircle size={16} aria-hidden="true" /> {progress}</p>
            )}
          </div>
          {failure && (
            <div className="sc-chat-error">
              <p role="alert">{failure.message || "The jewelry assistant is temporarily unavailable."}</p>
              {failure.requestId && <p className="sc-chat-request-id">Request {failure.requestId}</p>}
              <button type="button" onClick={() => { logRef.current.focus(); send(failure.question, true); }}>
                Try again
              </button>
            </div>
          )}
        </div>

        <footer className="sc-chat-footer">
          <form className="sc-chat-form" onSubmit={(event) => { event.preventDefault(); send(draft); }}>
            <input
              type="text"
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
