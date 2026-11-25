const { execFile } = require("node:child_process");
const { mkdtemp, readFile, readdir, rm, rmdir } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const { createInterface } = require("node:readline");

const script = resolve(__dirname, "../../../tools/generate-sapphire-ring.py");
const cache = new Map();
const previewLifetime = 60 * 60 * 1000;
const maxPreviews = 32;
const maxConcurrentExports = 2;
let activeExports = 0;

function detectDesign(question) {
  const command = /^(?:please\s+)?(?:(?:can|could|would) you\s+|i (?:want|would like) you to\s+)?(?:please\s+)?(?:generate|create|design|make|build|model|render|show me)\b/i;
  if (!command.test(question) || /\b(?:how to|how (?:can|do|should)|instructions for)\b/i.test(question)) {
    return null;
  }
  const item = question.match(/\b(rings?|bracelets?|bangles?)\b/i)?.[0];
  if (!item) return null;
  const unsupported = question.match(/\b(?:platinum|titanium|copper|amethyst|opal|pearl|topaz|white[ -]gold)\b/i);
  const multipleItems = /\bring\b/i.test(question) && /\b(?:bracelet|bangle)\b/i.test(question);
  const metals = question.match(/\b(?:rose[ -]gold|silver|gold)\b/gi) || [];
  const gems = question.match(/\b(?:sapphire|ruby|emerald|diamond)\b/gi) || [];
  if (unsupported || multipleItems || new Set(metals.map(value => value.toLowerCase())).size > 1 ||
      new Set(gems.map(value => value.toLowerCase())).size > 1 ||
      /\b(?:size|diameter|engrave|engraved|engraving|halo|princess|oval|heart|carat)\b|\d/i.test(question)) {
    const error = new Error(unsupported
      ? `The preview generator does not support ${unsupported[0]} yet. Choose gold, silver, or rose gold, with sapphire, ruby, emerald, diamond, or no stone.`
      : "Please request one ring or bracelet with one metal and one gemstone. Custom sizes, cuts, and engraving are not available yet.");
    error.code = "UNSUPPORTED_DESIGN";
    throw error;
  }

  const kind = /^ring/i.test(item) ? "ring" : "bracelet";
  const metal = /\brose[ -]gold\b/i.test(question)
    ? "rose-gold"
    : /\bsilver\b/i.test(question) ? "silver" : "gold";
  const specifiedGem = question.match(/\b(sapphire|ruby|emerald|diamond)\b/i)?.[0].toLowerCase();
  const plain = /\b(?:plain|(?:no|without) (?:a )?(?:stones?|gems?(?:stones?)?|sapphire|ruby|emerald|diamond))\b/i.test(question);
  const gem = plain ? "none" : specifiedGem ?? (kind === "ring" ? "sapphire" : "none");
  return { kind, metal, gem };
}

async function blenderExecutable() {
  if (process.env.BLENDER_PATH) return process.env.BLENDER_PATH;
  if (process.platform !== "win32" || !process.env.ProgramFiles) return "blender";

  const directory = join(process.env.ProgramFiles, "Blender Foundation");
  const versions = await readdir(directory, { withFileTypes: true }).catch(() => []);
  const installations = versions.filter((entry) => entry.isDirectory() && /^Blender \d/.test(entry.name));
  installations.sort((a, b) => b.name.localeCompare(a.name, undefined, { numeric: true }));
  return installations.length ? join(directory, installations[0].name, "blender.exe") : "blender";
}

function exportModel(blender, args, signal, log) {
  return new Promise((resolve, reject) => {
    const child = execFile(blender, args, {
      signal,
      timeout: 40_000,
      maxBuffer: 256 * 1024,
      windowsHide: true,
    }, (error) => error ? reject(error) : resolve());
    for (const stream of [child.stdout, child.stderr]) {
      createInterface({ input: stream }).on("line", (line) => {
        if (line.trim()) log("blender.output", { message: line });
      });
    }
  });
}

function previewModel(id) {
  const entry = cache.get(id);
  if (!entry || entry.expires <= Date.now()) {
    cache.delete(id);
    return null;
  }
  return entry.model;
}

async function generatePreview(id, design, { signal, log, progress }) {
  if (activeExports >= maxConcurrentExports) {
    const error = new Error("All Blender workers are busy");
    error.code = "DESIGN_BUSY";
    throw error;
  }
  activeExports++;
  let folder;
  try {
    const { kind, metal, gem } = design;
    folder = await mkdtemp(join(tmpdir(), "shecraft-design-"));
    const filename = join(folder, "preview.glb");
    const blender = await blenderExecutable();
    signal.throwIfAborted();
    log("blender.started", design);
    progress(`Generating your ${kind}…`);
    await exportModel(blender, [
      "--background", "--factory-startup", "--python-exit-code", "1", "--python", script,
      "--", "--kind", kind, "--metal", metal, "--gem", gem, "--output", filename,
      "--style", design.style ?? "solitaire", "--color", design.color ?? "white",
      "--count", String(design.count ?? 20), "--size", String(design.size ?? 7),
      "--diameter", String(design.diameter ?? 60),
    ], signal, log);

    progress("Preparing your 3D preview…");
    const model = await readFile(filename);
    if (model.length < 20 || model.toString("ascii", 0, 4) !== "glTF" ||
        model.readUInt32LE(4) !== 2 || model.readUInt32LE(8) !== model.length) {
      throw new Error("Blender returned an invalid GLB");
    }
    signal.throwIfAborted();
    if (cache.size >= maxPreviews) cache.delete(cache.keys().next().value);
    cache.set(id, { model, expires: Date.now() + previewLifetime });
    log("blender.exported", { bytes: model.length, kind });

    const title = `${metal.replace("-", " ")} ${design.style === "tennis" ? "tennis " : ""}${gem === "none" ? "" : gem + " "}${kind}`;
    return {
      answer: `Your ${title} is ready to preview in 3D.`,
      sources: [],
      design: { ...design, id, title: title.charAt(0).toUpperCase() + title.slice(1) },
    };
  } finally {
    try {
      if (folder) {
        await rm(join(folder, "preview.glb"), { force: true });
        await rmdir(folder);
      }
    } catch (error) {
      log("blender.cleanup_failed", { code: error.code });
    } finally {
      activeExports--;
    }
  }
}

module.exports = { detectDesign, generatePreview, previewModel };
