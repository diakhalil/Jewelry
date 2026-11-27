// src/models/accessoryInstanceModel.js
const pool = require("../config/db");

// --- helpers: ID generators (A001, R001, N001, B001, E001) ---
async function nextId(conn, table, idCol, prefix, pad = 3) {
  const [rows] = await conn.query(
    `SELECT ${idCol} AS id FROM ${table} WHERE ${idCol} LIKE ? ORDER BY ${idCol} DESC LIMIT 1`,
    [`${prefix}%`]
  );

  const last = rows.length ? rows[0].id : null;
  const lastNum = last ? parseInt(String(last).slice(prefix.length), 10) : 0;
  const nextNum = lastNum + 1;

  return `${prefix}${String(nextNum).padStart(pad, "0")}`;
}

function badRequest(msg) {
  const e = new Error(msg);
  e.status = 400;
  return e;
}

function norm(s) {
  return String(s || "").trim().toLowerCase();
}

/**
 * ✅ Fixed-price rules (ignores metal/material price for these cases)
 * You said:
 * - necklace + style "name" => 40
 * - ring style tennis => 70, aurora => 90, celeste => 80
 * - bracelet style "name bracelet" => 35
 * - earring style hoop square => 20, hoop round => 20, dangling => 45
 */
function getBasePrice(type, product = {}) {
  const t = norm(type);
  const style = norm(product?.style);

  // necklace
  if (t === "necklace" && style === "name") return 40;

  // ring
  if (t === "ring") {
    if (style === "tennis") return 70;
    if (style === "aurora") return 90;
    if (style === "celeste") return 80;
  }

  // bracelet
  if (t === "bracelet" && style === "name bracelet") return 35;

  // earring
  if (t === "earring") {
    if (style === "hoop square") return 20;
    if (style === "hoop round") return 20;
    if (style === "dangling") return 45;
  }

  return 0; // not a fixed-price product
}

function normalizeDesignComponents({ charms = [], stones = [] } = {}) {
  function normalize(items, idKey) {
    const quantities = new Map();
    for (const item of Array.isArray(items) ? items : []) {
      const id = item?.[idKey];
      if (!id) continue;

      const quantity = item.quantity == null ? 1 : Number(item.quantity);
      if (!Number.isFinite(quantity) || quantity <= 0) continue;
      quantities.set(id, (quantities.get(id) || 0) + quantity);
    }

    return [...quantities.entries()].map(([id, requestedQuantity]) => ({
      [idKey]: id,
      requestedQuantity,
      storedQuantity: Math.max(1, Math.trunc(requestedQuantity)),
    }));
  }

  return {
    charms: normalize(charms, "charmID"),
    stones: normalize(stones, "stoneID"),
  };
}

async function reserveAndDecrementCharms(conn, charms) {
  if (!Array.isArray(charms) || charms.length === 0) return;

  const ids = charms.map((item) => item.charmID);
  if (ids.length === 0) return;

  const placeholders = ids.map(() => "?").join(",");

  const [rows] = await conn.query(
    `SELECT charmID, qty FROM charm WHERE charmID IN (${placeholders}) FOR UPDATE`,
    ids
  );

  if (rows.length !== ids.length) throw badRequest("One or more charmIDs do not exist");

  const stock = new Map(rows.map((r) => [r.charmID, Number(r.qty)]));
  for (const { charmID: id, requestedQuantity: needed } of charms) {
    const available = stock.get(id) ?? 0;
    if (available < needed) {
      throw badRequest(`Charm ${id} out of stock (need ${needed}, have ${available})`);
    }
  }

  for (const { charmID: id, requestedQuantity: needed } of charms) {
    await conn.query(`UPDATE charm SET qty = qty - ? WHERE charmID = ?`, [needed, id]);
  }
}

async function reserveAndDecrementStones(conn, stones) {
  if (!Array.isArray(stones) || stones.length === 0) return;

  const ids = stones.map((item) => item.stoneID);
  if (ids.length === 0) return;

  const placeholders = ids.map(() => "?").join(",");

  const [rows] = await conn.query(
    `SELECT stoneID, qty FROM stone WHERE stoneID IN (${placeholders}) FOR UPDATE`,
    ids
  );

  if (rows.length !== ids.length) throw badRequest("One or more stoneIDs do not exist");

  const stock = new Map(rows.map((r) => [r.stoneID, Number(r.qty)]));
  for (const { stoneID: id, requestedQuantity: needed } of stones) {
    const available = stock.get(id) ?? 0;
    if (available < needed) {
      throw badRequest(`Stone ${id} out of stock (need ${needed}, have ${available})`);
    }
  }

  for (const { stoneID: id, requestedQuantity: needed } of stones) {
    await conn.query(`UPDATE stone SET qty = qty - ? WHERE stoneID = ?`, [needed, id]);
  }
}

async function calculateDesignPrice(conn, design = {}, normalizedComponents = null) {
  const { type, materialID, product = {}, charms = [], stones = [] } = design;
  const components = normalizedComponents || normalizeDesignComponents({ charms, stones });
  const basePrice = getBasePrice(type, product);
  const materialIncluded = basePrice <= 0;

  const [[material]] = await conn.query(
    `SELECT materialID, price FROM material WHERE materialID = ?`,
    [materialID]
  );
  if (!material) throw badRequest(`Material not found for pricing: ${materialID}`);

  async function getComponentPrices(table, idColumn, componentsForType) {
    if (componentsForType.length === 0) return new Map();
    const ids = componentsForType.map((item) => item[idColumn]);
    const placeholders = ids.map(() => "?").join(", ");
    const [rows] = await conn.query(
      `SELECT ${idColumn}, price FROM ${table} WHERE ${idColumn} IN (${placeholders})`,
      ids
    );
    const prices = new Map(rows.map((row) => [row[idColumn], Number(row.price) || 0]));
    if (prices.size !== ids.length) {
      throw badRequest(`One or more ${table} IDs do not exist`);
    }
    return prices;
  }

  const charmPrices = await getComponentPrices("charm", "charmID", components.charms);
  const stonePrices = await getComponentPrices("stone", "stoneID", components.stones);
  const toCents = (value) => Math.round((Number(value) || 0) * 100);
  const charmLines = components.charms.map(({ charmID, storedQuantity }) => {
    const unitPrice = charmPrices.get(charmID);
    return {
      charmID,
      quantity: storedQuantity,
      unitPrice,
      lineTotal: toCents(unitPrice * storedQuantity) / 100,
    };
  });
  const stoneLines = components.stones.map(({ stoneID, storedQuantity }) => {
    const unitPrice = stonePrices.get(stoneID);
    return {
      stoneID,
      quantity: storedQuantity,
      unitPrice,
      lineTotal: toCents(unitPrice * storedQuantity) / 100,
    };
  });

  const charmsTotalCents = charmLines.reduce((sum, line) => sum + toCents(line.lineTotal), 0);
  const stonesTotalCents = stoneLines.reduce((sum, line) => sum + toCents(line.lineTotal), 0);
  const materialPrice = Number(material.price) || 0;
  const materialCost = materialIncluded ? materialPrice : 0;
  const totalCents = toCents(basePrice) + toCents(materialCost) + charmsTotalCents + stonesTotalCents;

  return {
    totalPrice: totalCents / 100,
    breakdown: {
      basePrice,
      material: {
        materialID,
        unitPrice: materialPrice,
        included: materialIncluded,
        lineTotal: materialCost,
      },
      charms: charmLines,
      charmsTotal: charmsTotalCents / 100,
      stones: stoneLines,
      stonesTotal: stonesTotalCents / 100,
    },
  };
}

async function previewAccessoryPrice(design) {
  const conn = await pool.getConnection();
  try {
    return await calculateDesignPrice(conn, design);
  } finally {
    conn.release();
  }
}

async function upsertOrnaments(conn, accessoryID, charms) {
  if (!Array.isArray(charms) || charms.length === 0) return;
  const rows = charms.map(({ charmID, storedQuantity }) => [
    accessoryID,
    charmID,
    storedQuantity,
  ]);

  const placeholders = rows.map(() => "(?,?,?)").join(", ");
  const flat = rows.flat();

  await conn.query(
    `
    INSERT INTO ornaments (accessoryID, charmID, quantity)
    VALUES ${placeholders}
    ON DUPLICATE KEY UPDATE quantity = VALUES(quantity)
    `,
    flat
  );
}

async function upsertGems(conn, accessoryID, stones) {
  if (!Array.isArray(stones) || stones.length === 0) return;
  const rows = stones.map(({ stoneID, storedQuantity }) => [
    accessoryID,
    stoneID,
    storedQuantity,
  ]);

  const placeholders = rows.map(() => "(?,?,?)").join(", ");
  const flat = rows.flat();

  await conn.query(
    `
    INSERT INTO gems (accessoryID, stoneID, quantity)
    VALUES ${placeholders}
    ON DUPLICATE KEY UPDATE quantity = VALUES(quantity)
    `,
    flat
  );
}

// --- main transactional creator ---
async function createAccessoryInstance({
  type, // "ring" | "necklace" | "bracelet" | "earring"
  materialID, // ✅ MUST be provided by controller
  nbOfCharms = 0,
  nbOfStones = 0,
  product,
  charms = [],
  stones = [],
}) {
  const components = normalizeDesignComponents({ charms, stones });
  const conn = await pool.getConnection();
  try {
    await conn.beginTransaction();

    if (!materialID) throw badRequest("materialID is required");

    // 1) create accessoryID
    const accessoryID = await nextId(conn, "accessory", "accessoryID", "X", 3);

    // 2) insert accessory
    await conn.query(
      `INSERT INTO accessory (accessoryID, price, materialID, status)
       VALUES (?, 0, ?, 'reserved')`,
      [accessoryID, materialID]
    );

    // 3) insert child row based on type
    if (type === "ring") {
      const ringID = await nextId(conn, "ring", "ringID", "R", 3);
      const { bandType, diameter, style } = product || {};
      await conn.query(
        `INSERT INTO ring (ringID, bandType, diameter, accessoryID) VALUES (?, ?, ?, ?)`,
        [ringID, bandType, diameter, accessoryID]
      );
    } else if (type === "necklace") {
      const necklaceID = await nextId(conn, "necklace", "necklaceID", "N", 3);
      const { chain, style, length } = product || {};
      await conn.query(
        `INSERT INTO necklace (necklaceID, chain, style, length, accessoryID) VALUES (?, ?, ?, ?, ?)`,
        [necklaceID, chain, style, length, accessoryID]
      );
    } else if (type === "bracelet") {
      const braceletID = await nextId(conn, "bracelet", "braceletID", "B", 3);
      const { chain, style, length } = product || {};
      await conn.query(
        `INSERT INTO bracelet (braceletID, chain, style, length, accessoryID) VALUES (?, ?, ?, ?, ?)`,
        [braceletID, chain, style, length, accessoryID]
      );
    } else if (type === "earring") {
      const earringID = await nextId(conn, "earring", "earringID", "E", 3);
      const { backing, style, size } = product || {};
      await conn.query(
        `INSERT INTO earring (earringID, backing, style, size, accessoryID) VALUES (?, ?, ?, ?, ?)`,
        [earringID, backing, style, size, accessoryID]
      );
    } else {
      throw badRequest(`Unsupported type: ${type}`);
    }

    // 4) reserve + decrement inventory, then insert ornaments/gems
    await reserveAndDecrementCharms(conn, components.charms);
    await reserveAndDecrementStones(conn, components.stones);

    await upsertOrnaments(conn, accessoryID, components.charms);
    await upsertGems(conn, accessoryID, components.stones);

    // 5) compute price and update accessory
    const { totalPrice: computedPrice } = await calculateDesignPrice(conn, {
      type,
      materialID,
      product,
      charms,
      stones,
    }, components);

    await conn.query(`UPDATE accessory SET price = ? WHERE accessoryID = ?`, [
      computedPrice,
      accessoryID,
    ]);

    await conn.commit();
    return { accessoryID, computedPrice };
  } catch (err) {
    await conn.rollback();
    throw err;
  } finally {
    conn.release();
  }
}

async function cancelAccessoryReservation(accessoryID) {
  const conn = await pool.getConnection();
  try {
    await conn.beginTransaction();

    const [[acc]] = await conn.query(
      `SELECT accessoryID, status FROM accessory WHERE accessoryID = ? FOR UPDATE`,
      [accessoryID]
    );

    if (!acc) {
      const e = new Error("Accessory not found");
      e.status = 404;
      throw e;
    }

    if (acc.status === "cancelled") {
      const e = new Error("Accessory already cancelled");
      e.status = 409;
      throw e;
    }

    if (acc.status === "ordered") {
      const e = new Error("Cannot cancel: accessory already ordered");
      e.status = 409;
      throw e;
    }

    const [chRows] = await conn.query(
      `SELECT charmID, quantity FROM ornaments WHERE accessoryID = ?`,
      [accessoryID]
    );

    for (const r of chRows) {
      await conn.query(`UPDATE charm SET qty = qty + ? WHERE charmID = ?`, [
        Number(r.quantity),
        r.charmID,
      ]);
    }

    const [stRows] = await conn.query(
      `SELECT stoneID, quantity FROM gems WHERE accessoryID = ?`,
      [accessoryID]
    );

    for (const r of stRows) {
      await conn.query(`UPDATE stone SET qty = qty + ? WHERE stoneID = ?`, [
        Number(r.quantity),
        r.stoneID,
      ]);
    }

    await conn.query(`UPDATE accessory SET status = 'cancelled' WHERE accessoryID = ?`, [
      accessoryID,
    ]);

    await conn.commit();
    return {
      accessoryID,
      releasedCharms: chRows.length,
      releasedStones: stRows.length,
    };
  } catch (err) {
    await conn.rollback();
    throw err;
  } finally {
    conn.release();
  }
}

module.exports = {
  normalizeDesignComponents,
  calculateDesignPrice,
  previewAccessoryPrice,
  createAccessoryInstance,
  cancelAccessoryReservation,
};
