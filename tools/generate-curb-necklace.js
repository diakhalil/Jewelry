const fs = require('fs');
const path = require('path');

// Generates a compact, dependency-free GLB approximation of a curb-chain
// necklace. glTF units are metres; the necklace is roughly 36 cm wide and
// 30 cm from its top ends to the lowest point.
const output = path.resolve(__dirname, '../BackEnd/public/images/chains/curb-chain-necklace.glb');

function torus(major = 0.00425, tube = 0.00105, majorSegments = 20, tubeSegments = 8) {
  const positions = [];
  const normals = [];
  const indices = [];
  for (let i = 0; i < majorSegments; i++) {
    const u = (i / majorSegments) * Math.PI * 2;
    const cu = Math.cos(u), su = Math.sin(u);
    for (let j = 0; j < tubeSegments; j++) {
      const v = (j / tubeSegments) * Math.PI * 2;
      const cv = Math.cos(v), sv = Math.sin(v);
      positions.push((major + tube * cv) * cu, (major + tube * cv) * su, tube * sv);
      normals.push(cv * cu, cv * su, sv);
    }
  }
  for (let i = 0; i < majorSegments; i++) {
    const ni = (i + 1) % majorSegments;
    for (let j = 0; j < tubeSegments; j++) {
      const nj = (j + 1) % tubeSegments;
      const a = i * tubeSegments + j;
      const b = ni * tubeSegments + j;
      const c = ni * tubeSegments + nj;
      const d = i * tubeSegments + nj;
      indices.push(a, b, d, b, c, d);
    }
  }
  return { positions, normals, indices };
}

function quatMul(a, b) {
  return [
    a[3]*b[0] + a[0]*b[3] + a[1]*b[2] - a[2]*b[1],
    a[3]*b[1] - a[0]*b[2] + a[1]*b[3] + a[2]*b[0],
    a[3]*b[2] + a[0]*b[1] - a[1]*b[0] + a[2]*b[3],
    a[3]*b[3] - a[0]*b[0] - a[1]*b[1] - a[2]*b[2],
  ];
}

function necklacePoint(t) {
  // A soft U silhouette, matching the supplied front-view reference.
  return [0.18 * t, -0.15 + 0.30 * Math.pow(Math.abs(t), 1.62), 0];
}

// Build a dense polyline, then resample it at even arc-length intervals.
const dense = [];
let total = 0;
for (let i = 0; i <= 2000; i++) {
  const p = necklacePoint(-1 + 2 * i / 2000);
  if (dense.length) {
    const q = dense[dense.length - 1].p;
    total += Math.hypot(p[0] - q[0], p[1] - q[1]);
  }
  dense.push({ p, distance: total });
}

const linkCount = Math.floor(total / 0.0071) + 1;
const sampled = [];
let cursor = 0;
for (let i = 0; i < linkCount; i++) {
  const wanted = total * i / (linkCount - 1);
  while (cursor + 1 < dense.length && dense[cursor + 1].distance < wanted) cursor++;
  const a = dense[cursor], b = dense[Math.min(cursor + 1, dense.length - 1)];
  const span = Math.max(1e-9, b.distance - a.distance);
  const f = (wanted - a.distance) / span;
  sampled.push([
    a.p[0] + (b.p[0] - a.p[0]) * f,
    a.p[1] + (b.p[1] - a.p[1]) * f,
    0,
  ]);
}

const geometry = torus();
const pos = Buffer.from(new Float32Array(geometry.positions).buffer);
const norm = Buffer.from(new Float32Array(geometry.normals).buffer);
const idx = Buffer.from(new Uint16Array(geometry.indices).buffer);
const chunks = [];
const views = [];
let byteOffset = 0;
for (const data of [pos, norm, idx]) {
  const padding = (4 - (byteOffset % 4)) % 4;
  if (padding) { chunks.push(Buffer.alloc(padding)); byteOffset += padding; }
  views.push({ buffer: 0, byteOffset, byteLength: data.length });
  chunks.push(data);
  byteOffset += data.length;
}
const binary = Buffer.concat(chunks);

const nodes = sampled.map((p, i) => {
  const prev = sampled[Math.max(0, i - 1)];
  const next = sampled[Math.min(sampled.length - 1, i + 1)];
  const angle = Math.atan2(next[1] - prev[1], next[0] - prev[0]) - Math.PI / 2;
  const qAlign = [0, 0, Math.sin(angle / 2), Math.cos(angle / 2)];
  const twist = (i % 2 ? 1 : -1) * 0.88;
  const qTwist = [0, Math.sin(twist / 2), 0, Math.cos(twist / 2)];
  return {
    name: `Curb_Link_${String(i + 1).padStart(3, '0')}`,
    mesh: 0,
    translation: p,
    rotation: quatMul(qAlign, qTwist),
    scale: [0.72, 1.0, 0.78],
  };
});

const gltf = {
  asset: { version: '2.0', generator: 'SheCraft curb-chain generator' },
  scene: 0,
  scenes: [{ name: 'Curb Chain Necklace', nodes: nodes.map((_, i) => i) }],
  nodes,
  meshes: [{
    name: 'Curb_Link_Mesh',
    primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, material: 0 }],
  }],
  materials: [{
    name: 'Polished 18K Gold',
    pbrMetallicRoughness: {
      baseColorFactor: [1.0, 0.766, 0.336, 1.0],
      metallicFactor: 1.0,
      roughnessFactor: 0.17,
    },
    doubleSided: false,
  }],
  accessors: [
    { bufferView: 0, componentType: 5126, count: geometry.positions.length / 3, type: 'VEC3', min: [-0.0053, -0.0053, -0.00105], max: [0.0053, 0.0053, 0.00105] },
    { bufferView: 1, componentType: 5126, count: geometry.normals.length / 3, type: 'VEC3' },
    { bufferView: 2, componentType: 5123, count: geometry.indices.length, type: 'SCALAR', min: [0], max: [geometry.positions.length / 3 - 1] },
  ],
  bufferViews: [
    { ...views[0], target: 34962 },
    { ...views[1], target: 34962 },
    { ...views[2], target: 34963 },
  ],
  buffers: [{ byteLength: binary.length }],
};

function paddedJsonBuffer(value) {
  const raw = Buffer.from(JSON.stringify(value), 'utf8');
  const padding = (4 - (raw.length % 4)) % 4;
  return Buffer.concat([raw, Buffer.alloc(padding, 0x20)]);
}
const json = paddedJsonBuffer(gltf);
const binPadding = (4 - (binary.length % 4)) % 4;
const bin = Buffer.concat([binary, Buffer.alloc(binPadding)]);
const header = Buffer.alloc(12);
header.writeUInt32LE(0x46546c67, 0);
header.writeUInt32LE(2, 4);
header.writeUInt32LE(12 + 8 + json.length + 8 + bin.length, 8);
const jsonHeader = Buffer.alloc(8);
jsonHeader.writeUInt32LE(json.length, 0);
jsonHeader.writeUInt32LE(0x4e4f534a, 4);
const binHeader = Buffer.alloc(8);
binHeader.writeUInt32LE(bin.length, 0);
binHeader.writeUInt32LE(0x004e4942, 4);

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, Buffer.concat([header, jsonHeader, json, binHeader, bin]));
console.log(`Generated ${output}`);
console.log(`${nodes.length} links, ${(fs.statSync(output).size / 1024).toFixed(1)} KiB`);
