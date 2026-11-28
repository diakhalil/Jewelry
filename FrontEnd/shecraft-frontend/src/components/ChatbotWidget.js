import React, { useEffect, useRef, useState } from "react";
import { ArrowUpRight, LoaderCircle, MessageCircle, Send, X } from "lucide-react";
import { streamAssistant } from "../api/assistant";
import jewelryOptions from "../config/jewelryOptions.json";
import CharmChoices from "./CharmChoices";
import "../styles/chat.css";
import "../styles/chatbot-widget.css";

const RAG_API_URL = process.env.REACT_APP_RAG_API_URL || "http://localhost:8000";
const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

const builderPages = {
  ring: { overview: "/rings" },
  necklace: { overview: "/necklaces", name: "/necklacespage", charm: "/necklaces/charm", birthstone: "/necklaces/birthstone" },
  bracelet: { overview: "/bracelets", name: "/braceletspage", charm: "/bracelet/charm", birthstone: "/bracelet/birthstone" },
  earrings: { overview: "/earrings", charm: "/earrings/charm", birthstone: "/earrings/birthstone" },
};

function selectedValue(brief, field) {
  return brief.preferences.find(p => p.field === field && ["required", "preferred"].includes(p.intent))?.value.toLowerCase();
}

function StyleSwitcher({ brief, onSelect }) {
  const kind = selectedValue(brief, "jewelry_type");
  const style = selectedValue(brief, "style");
  if (brief.option_field === "style" || !["necklace", "bracelet", "earrings"].includes(kind) || !style) return null;
  return <details className="rag-chat-sources">
    <summary>Change style</summary>
    <div className="sc-chat-prompts rag-chat-options">
      {jewelryOptions[kind].styles.filter(option => option !== style).map(option => (
        <button key={option} type="button" onClick={() => onSelect(`Switch to ${option}`)}>{option}</button>
      ))}
    </div>
  </details>;
}

function builderLink(brief) {
  if (!brief) return null;
  const kind = selectedValue(brief, "jewelry_type");
  if (!Object.hasOwn(builderPages, kind)) {
    return brief.option_field ? <a href="/orderpage" target="_blank" rel="noreferrer">Browse jewelry options ↗</a> : null;
  }
  const pages = builderPages[kind];
  const style = brief.option_field === "charm" ? "charm" : selectedValue(brief, "style");
  const specific = brief.option_field !== "style" && style && style !== "overview" && Object.hasOwn(pages, style);
  const label = specific && style === "charm" ? "See charm photos and options" : `Explore ${specific ? style + " " : ""}${kind} options`;
  return <a href={specific ? pages[style] : pages.overview} target="_blank" rel="noreferrer">{label} ↗</a>;
}

const renderInlineMarkdown = (text) => text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).map((part, index) => {
  if (part.startsWith("**") && part.endsWith("**")) {
    return <strong key={index}>{part.slice(2, -2)}</strong>;
  }
  if (part.startsWith("*") && part.endsWith("*")) {
    return <em key={index}>{part.slice(1, -1)}</em>;
  }
  return part;
});

const FormattedAnswer = ({ content }) => {
  const lines = String(content || "").replace(/\r/g, "").split("\n");
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index].trim();
    if (!line) {
      index += 1;
      continue;
    }
    if (/^[-*_]{3,}$/.test(line)) {
      blocks.push(<hr key={`rule-${index}`} />);
      index += 1;
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const boldHeading = line.match(/^\*\*(.+)\*\*:?$/);
    if (heading || boldHeading) {
      blocks.push(
        <h4 key={`heading-${index}`}>
          {renderInlineMarkdown(heading ? heading[2] : boldHeading[1].replace(/:$/, ""))}
        </h4>
      );
      index += 1;
      continue;
    }

    const listItem = lines[index].match(/^(\s*)([-*]|\d+[.)])\s+(.+)$/);
    if (listItem) {
      const items = [];
      while (index < lines.length) {
        const match = lines[index].match(/^(\s*)([-*]|\d+[.)])\s+(.+)$/);
        if (!match) break;
        items.push(
          <li key={`item-${index}`} style={{ marginLeft: `${Math.min(match[1].length, 6) * 3}px` }}>
            {renderInlineMarkdown(match[3])}
          </li>
        );
        index += 1;
      }
      blocks.push(<ul key={`list-${index}`}>{items}</ul>);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim()) {
      const next = lines[index].trim();
      if (/^(#{1,3})\s+/.test(next) || /^\*\*.+\*\*:?$/.test(next)
        || /^[-*_]{3,}$/.test(next) || /^\s*([-*]|\d+[.)])\s+/.test(lines[index])) break;
      paragraph.push(next);
      index += 1;
    }
    blocks.push(<p key={`paragraph-${index}`}>{renderInlineMarkdown(paragraph.join(" "))}</p>);
  }

  return <div className="rag-chat-answer">{blocks}</div>;
};

function MessageDetails({ item, latest, isSending, send }) {
  return <>
    {latest && item.brief?.options?.length > 0 && (
      <div className="sc-chat-prompts rag-chat-options" role="group" aria-label={`Choose ${item.brief.option_field.replace(/_/g, " ")}`}>
        {item.brief.options.map(option => (
          <button key={option} type="button" disabled={isSending} onClick={() => send(option)}>
            {option}
          </button>
        ))}
      </div>
    )}
    {item.brief && <div className="rag-chat-builder">{builderLink(item.brief)}</div>}
    {latest && item.brief &&
      <StyleSwitcher brief={item.brief} onSelect={send} />}
    {latest && item.brief?.option_field === "charm" &&
      <CharmChoices color={selectedValue(item.brief, "charm_color")} onChoose={send} />}
    {item.images?.length > 0 && (
      <div className="rag-chat-image-results" aria-label="Retrieved images">
        {item.images.map((source) => (
            <figure key={`image-${source.image_id}`}>
              <img
                src={`${RAG_API_URL}${source.image_url}`}
                alt={source.excerpt || `Retrieved image from ${source.file_name}`}
                loading="lazy"
              />
              <figcaption>
                {source.label} · {source.file_name}
                {source.page_number != null ? ` · page ${Array.isArray(source.page_number) ? source.page_number.join(", ") : source.page_number}` : ""}
              </figcaption>
            </figure>
          ))}
      </div>
    )}
    {item.sources?.length > 0 && (
      <details className="rag-chat-sources">
        <summary>Sources ({item.sources.length})</summary>
        <ul>
          {item.sources.map((source) => (
            <li key={`${source.label}-${source.file_name}-${source.page_number}`}>
              <strong>{source.label} · {source.file_name}</strong>
              <span>
                {source.kind === "image" ? "Image description" : "Page"}
                {source.page_number != null ? ` ${source.page_number}` : ""}
              </span>
              {source.excerpt && <small>{source.excerpt}</small>}
            </li>
          ))}
        </ul>
      </details>
    )}
    {item.brief?.preferences?.length > 0 && (
      <details className="rag-chat-sources">
        <summary>Design brief · {{ ready: "Ready for design", collecting: "In progress", needs_review: "Extras need design review" }[item.brief.status]}</summary>
        <ul>{item.brief.preferences.map((p, i) => (
          <li key={i}><strong>{p.field.replace(/_/g, " ")}</strong><span>{p.value} · {p.intent}</span>
            {["required", "preferred"].includes(p.intent) && item.brief.review_reasons?.[p.field] &&
              <small>Needs review · {item.brief.review_reasons[p.field]}</small>}
          </li>
        ))}</ul>
      </details>
    )}
    {item.steps?.length > 0 && (
      <details className="rag-chat-sources">
        <summary>Activity ({item.steps.length})</summary>
        <ol>{item.steps.map((step, i) => <li key={i}>{step}</li>)}</ol>
      </details>
    )}
  </>;
}

export default function ChatbotWidget() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [progress, setProgress] = useState("");
  const [failure, setFailure] = useState(null);
  const [conversationId] = useState(() => crypto.randomUUID());
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

  async function send(question, { retry = false, fromComposer = false } = {}) {
    const text = question.trim();
    if (!text || text.length > 2000 || requestRef.current) return;

    const request = new AbortController();
    requestRef.current = request;
    setProgress("Sending your request…");
    setFailure(null);
    if (!retry) {
      if (fromComposer) setDraft("");
      setMessages((previous) => [...previous, { role: "user", text, time: new Date() }]);
    }

    const steps = [];
    try {
      const data = await streamAssistant(`${RAG_API_URL}/api/assistant/chat`, conversationId, text, step => {
        steps.push(step);
        setProgress(step);
      }, request.signal);
      const cited = new Set((data.answer.match(/\[[^\]]+\]/g) || []).flatMap(group => group.match(/\bI\d+\b/g) || []));
      const sources = data.sources || [];
      const images = sources.filter(source => source.kind === "image" && source.image_url && cited.has(source.label));
      setMessages(previous => [...previous, {
        role: "assistant", text: data.answer, time: new Date(), sources, images, brief: data.brief, steps,
      }]);
    } catch (error) {
      if (!request.signal.aborted) setFailure({ question: text, message: error.message, steps });
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
        aria-label="Open jewelry assistant"
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
            <p>Design preferences and jewelry guides</p>
          </div>
          <button type="button" className="sc-chat-close" aria-label="Close chat" onClick={closeChat} ref={closeRef}>
            <X size={21} aria-hidden="true" />
          </button>
        </header>

        <div className="sc-chat-scroll" ref={logRef} tabIndex={0} aria-label="Chat conversation">
          <div className="sc-chat-message sc-chat-message--assistant">
            <div className="sc-chat-byline">SheCraft</div>
            <p>What would you like to design? You can also ask about gemstones, sizing, or care.</p>
          </div>
          {messages.length === 0 && (
            <>
              <div className="sc-chat-prompts rag-chat-options" role="group" aria-label="Choose jewelry type">
                {Object.keys(jewelryOptions).map(kind => (
                  <button key={kind} type="button" onClick={() => send(kind)}>{kind}</button>
                ))}
                <button type="button" onClick={() => send("How do I measure my ring size?")}>
                  How do I measure my ring size? <ArrowUpRight size={15} aria-hidden="true" />
                </button>
              </div>
              <div className="rag-chat-builder">
                <a href="/orderpage" target="_blank" rel="noreferrer">Browse jewelry options ↗</a>
              </div>
            </>
          )}
          <div role="log" aria-label="Messages" className="sc-chat-messages">
            {messages.map((item, index) => (
              <article key={index} className={`sc-chat-message sc-chat-message--${item.role}`}>
                <div className="sc-chat-byline">
                  <span>{item.role === "assistant" ? "SheCraft" : "You"}</span>
                  <time dateTime={item.time.toISOString()}>{timeFormat.format(item.time)}</time>
                </div>
                {item.role === "assistant" ? <FormattedAnswer content={item.text} /> : <p>{item.text}</p>}
                <MessageDetails item={item} latest={index === messages.length - 1} isSending={isPending} send={send} />
              </article>
            ))}
          </div>
          <div role="status">
            {isPending && (
              <p className="sc-chat-status"><LoaderCircle size={16} aria-hidden="true" /> {progress}</p>
            )}
          </div>
          {failure && (
            <div className="sc-chat-error">
              <p role="alert">{failure.message || "The jewelry assistant is temporarily unavailable."}</p>
              <MessageDetails item={failure} />
              <button type="button" onClick={() => { logRef.current.focus(); send(failure.question, { retry: true }); }}>
                Try again
              </button>
            </div>
          )}
        </div>

        <footer className="sc-chat-footer">
          <form className="sc-chat-form" onSubmit={(event) => { event.preventDefault(); send(draft, { fromComposer: true }); }}>
            <textarea
              aria-label="Your question"
              placeholder="Ask a jewelry question…"
              maxLength={2000}
              rows={1}
              value={draft}
              onChange={event => setDraft(event.target.value)}
              onKeyDown={event => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  send(draft, { fromComposer: true });
                }
              }}
            />
            <button type="submit" className="sc-button" disabled={isPending || !draft.trim()} aria-label="Send question">
              <Send size={18} aria-hidden="true" />
            </button>
          </form>
          <div className="rag-chat-footnote">AI can make mistakes. Check the cited guide.</div>
        </footer>
      </dialog>
    </>
  );
}
