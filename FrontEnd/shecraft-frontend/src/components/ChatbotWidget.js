import React, { useEffect, useRef, useState } from "react";
import { LoaderCircle, MessageCircle, Send, Sparkles, X } from "lucide-react";
import "../styles/chatbot-widget.css";

const RAG_API_URL = process.env.REACT_APP_RAG_API_URL || "http://localhost:8000";

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

const ChatbotWidget = () => {
  const [isOpen, setIsOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      content: "Hi! Ask me about gemstones, jewelry materials, sizing, or care.",
      sources: [],
    },
  ]);
  const bottomRef = useRef(null);

  useEffect(() => {
    if (isOpen) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isOpen, isSending]);

  const sendMessage = async (event) => {
    event?.preventDefault();
    const message = question.trim();
    if (!message || isSending) return;

    setQuestion("");
    setMessages((current) => [...current, { role: "user", content: message }]);
    setIsSending(true);

    try {
      const response = await fetch(`${RAG_API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, text_top_k: 5, image_top_k: 3 }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || data.error || "The assistant could not answer.");
      }
      setMessages((current) => [
        ...current,
        { role: "assistant", content: data.answer, sources: data.sources || [] },
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: `${error.message} Make sure the RAG service is running.`,
          sources: [],
          isError: true,
        },
      ]);
    } finally {
      setIsSending(false);
    }
  };

  return (
    <div className="rag-chat-root">
      {isOpen && (
        <section className="rag-chat-panel" aria-label="Jewelry knowledge assistant">
          <header className="rag-chat-header">
            <div className="rag-chat-heading">
              <span className="rag-chat-avatar"><Sparkles size={17} /></span>
              <div>
                <strong>Jewelry assistant</strong>
                <span>Answers grounded in your guides</span>
              </div>
            </div>
            <button
              type="button"
              className="rag-chat-close"
              onClick={() => setIsOpen(false)}
              aria-label="Close chat"
            >
              <X size={20} />
            </button>
          </header>

          <div className="rag-chat-messages" aria-live="polite">
            {messages.map((item, index) => (
              <article
                className={`rag-chat-message rag-chat-${item.role}${item.isError ? " rag-chat-error" : ""}`}
                key={`${item.role}-${index}`}
              >
                {item.role === "assistant" && !item.isError
                  ? <FormattedAnswer content={item.content} />
                  : <p>{item.content}</p>}
                {item.role === "assistant" && item.sources?.some((source) => source.kind === "image" && source.image_url) && (
                  <div className="rag-chat-image-results" aria-label="Retrieved images">
                    {item.sources
                      .filter((source) => source.kind === "image" && source.image_url)
                      .map((source) => (
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
              </article>
            ))}
            {isSending && (
              <div className="rag-chat-thinking" role="status">
                <LoaderCircle size={16} className="rag-chat-spinner" />
                Searching the jewelry guides…
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <form className="rag-chat-form" onSubmit={sendMessage}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) sendMessage(event);
              }}
              placeholder="Ask a jewelry question…"
              maxLength={2000}
              rows={1}
              aria-label="Your question"
              disabled={isSending}
            />
            <button
              type="submit"
              aria-label="Send question"
              disabled={!question.trim() || isSending}
            >
              <Send size={17} />
            </button>
          </form>
          <div className="rag-chat-footnote">AI can make mistakes. Check the cited guide.</div>
        </section>
      )}

      <button
        type="button"
        className="rag-chat-launcher"
        onClick={() => setIsOpen((open) => !open)}
        aria-label={isOpen ? "Close jewelry assistant" : "Open jewelry assistant"}
        aria-expanded={isOpen}
      >
        {isOpen ? <X size={22} /> : <MessageCircle size={23} />}
        {!isOpen && <span>Ask SheCraft</span>}
      </button>
    </div>
  );
};

export default ChatbotWidget;
