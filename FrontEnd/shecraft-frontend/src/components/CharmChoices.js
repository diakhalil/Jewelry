import React, { useEffect, useState } from "react";

const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:5000";

export default function CharmChoices({ onChoose, color }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open || rows) return;
    const controller = new AbortController();
    async function load() {
      setError(false);
      try {
        const response = await fetch(`${API_BASE}/api/charms`, { signal: controller.signal });
        if (!response.ok) throw new Error("Catalog unavailable");
        const data = await response.json();
        setRows(data.filter(c => c.charmID && c.photoURL && c.qty > 0));
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }
    load();
    return () => controller.abort();
  }, [open, rows]);

  const matches = rows?.filter(c => !color || c.color?.toLowerCase().replace(/\s/g, "") === color.replace(/\s/g, ""));
  return <details className="rag-chat-sources" onToggle={e => setOpen(e.currentTarget.open)}>
    <summary>Choose a charm by photo</summary>
    {open && (matches?.length ? <div className="rag-charm-photos">
      {matches.map(charm => {
        const label = `${charm.color} ${charm.design === "shapes" ? "shape charm" : charm.design} · ${charm.charmID}`;
        return <button key={charm.charmID} type="button" onClick={() => onChoose(`I choose the catalog charm ${label}.`)}>
          <img src={`${API_BASE}${charm.photoURL}`} alt="" loading="lazy" />
          <span>{label}</span>
        </button>;
      })}
    </div> : <p>{error ? "The charm photos couldn’t load. You can describe your charm here."
      : rows ? "No in-stock charm photos are listed. You can describe what you’d like." : "Loading charm photos…"}</p>)}
  </details>;
}
