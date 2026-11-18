#!/usr/bin/env node

/** Create a print-safe article HTML file from a browser-saved webpage. */

const fs = require("node:fs");
const path = require("node:path");

function usage() {
  console.error("Usage: node clean_html_for_pdf.js <input.html> <output.html>");
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function getAttribute(tag, name) {
  const pattern = new RegExp(
    `\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`,
    "i",
  );
  const match = tag.match(pattern);
  return match ? match[1] ?? match[2] ?? match[3] ?? "" : "";
}

function cleanArticle(article) {
  let cleaned = article
    .replace(/<(script|style|svg|button|form|iframe|noscript)\b[\s\S]*?<\/\1\s*>/gi, "")
    .replace(/<aside\b[\s\S]*?<\/aside\s*>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");

  cleaned = cleaned.replace(/<img\b[^>]*>/gi, (tag) => {
    const src = getAttribute(tag, "src");
    const alt = getAttribute(tag, "alt");
    if (!src || src === "data:,") return "";
    return `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt)}">`;
  });

  const allowed = new Set([
    "article",
    "div",
    "p",
    "strong",
    "em",
    "figure",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "img",
    "br",
  ]);

  cleaned = cleaned.replace(/<\/?([a-z][a-z0-9-]*)\b[^>]*>/gi, (tag, name) => {
    const lowerName = name.toLowerCase();
    if (!allowed.has(lowerName)) return "";
    if (lowerName === "img") return tag;
    if (tag.startsWith("</")) return `</${lowerName}>`;
    if (lowerName === "br") return "<br>";
    return `<${lowerName}>`;
  });

  cleaned = cleaned
    .replace(/<div>\s*<\/div>/gi, "")
    .replace(/<p>\s*<\/p>/gi, "")
    .replace(/(?:\r?\n\s*){3,}/g, "\n\n")
    .trim();

  return cleaned;
}

function main() {
  const [, , inputArgument, outputArgument] = process.argv;
  if (!inputArgument || !outputArgument) {
    usage();
    process.exitCode = 2;
    return;
  }

  const inputPath = path.resolve(inputArgument);
  const outputPath = path.resolve(outputArgument);
  const source = fs.readFileSync(inputPath, "utf8");
  const articleMatch = source.match(/<article\b[\s\S]*?<\/article\s*>/i);
  if (!articleMatch) {
    throw new Error(`No <article> element found in ${inputPath}`);
  }

  const title =
    source.match(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/i)?.[1]?.trim() ||
    path.basename(inputPath, path.extname(inputPath));
  const canonicalUrl =
    source.match(/<link\b[^>]*rel=["']?canonical["']?[^>]*href=["']?([^"'\s>]+)/i)?.[1] ||
    source.match(/<link\b[^>]*href=["']?([^"'\s>]+)[^>]*rel=["']?canonical["']?/i)?.[1] ||
    "";
  const article = cleanArticle(articleMatch[0]);
  const sourceLine = canonicalUrl
    ? `<p class="source">Source: <a href="${escapeHtml(canonicalUrl)}">${escapeHtml(canonicalUrl)}</a></p>`
    : "";

  const output = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(title)}</title>
  <style>
    @page { size: A4; margin: 18mm; }
    * { box-sizing: border-box; }
    body { margin: 0 auto; max-width: 780px; color: #202020; background: #fff; font: 16px/1.55 Arial, sans-serif; }
    h1 { margin: 0 0 0.35em; color: #3f3024; font: 700 32px/1.15 Georgia, serif; }
    h2, h3 { break-after: avoid; color: #3f3024; }
    p { margin: 0.75em 0; }
    strong { color: #3f3024; }
    figure { margin: 1.25em 0; break-inside: avoid; }
    img { display: block; max-width: 100%; max-height: 220mm; margin: 0 auto; object-fit: contain; }
    figcaption { margin-top: 0.4em; color: #555; font-size: 0.88em; }
    .source { margin: 0 0 2em; color: #666; font-size: 0.82em; overflow-wrap: anywhere; }
    a { color: inherit; }
  </style>
</head>
<body>
${sourceLine}
${article}
</body>
</html>
`;

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, output, "utf8");
  console.log(`Cleaned: ${inputPath}`);
  console.log(`Created: ${outputPath}`);
}

main();
