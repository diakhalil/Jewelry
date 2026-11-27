// src/controllers/accessoryInstanceController.js
const {
  createAccessoryInstance,
  previewAccessoryPrice,
  cancelAccessoryReservation,
} = require("../models/accessoryInstanceModel");
const { getMaterialsByMetal } = require("../models/materialModel");

async function resolveMaterialID(materialID, metal) {
  if (materialID) return { materialID };
  if (!metal) return { error: "Provide materialID or metal" };

  const rows = await getMaterialsByMetal(metal);
  if (!rows || rows.length === 0) {
    return { error: "Invalid metal (no material found)" };
  }

  // If multiple rows exist for same metal, pick cheapest (or pick first).
  rows.sort((a, b) => Number(a.price) - Number(b.price));
  return { materialID: rows[0].materialID };
}

exports.createAccessory = async (req, res) => {
  try {
    let { type, materialID, metal, product, charms, stones, nbOfCharms, nbOfStones } = req.body;

    if (!type) return res.status(400).json({ message: "Missing type" });

    const materialResolution = await resolveMaterialID(materialID, metal);
    if (materialResolution.error) {
      return res.status(400).json({ message: materialResolution.error });
    }
    materialID = materialResolution.materialID;

    const result = await createAccessoryInstance({
      type,
      materialID,
      nbOfCharms,
      nbOfStones,
      product,
      charms,
      stones,
    });

    return res.status(201).json(result);
  } catch (err) {
    console.error("createAccessory error:", err);
    return res.status(err.status || 500).json({
      message: err.status ? err.message : "Failed to create accessory instance",
      error: err.message,
      code: err.code,
      sqlMessage: err.sqlMessage,
    });
  }
};

exports.previewAccessory = async (req, res) => {
  try {
    const { type, materialID: requestedMaterialID, metal, product, charms, stones } = req.body;
    if (!type) return res.status(400).json({ message: "Missing type" });

    const materialResolution = await resolveMaterialID(requestedMaterialID, metal);
    if (materialResolution.error) {
      return res.status(400).json({ message: materialResolution.error });
    }
    const { materialID } = materialResolution;
    const { totalPrice, breakdown } = await previewAccessoryPrice({
      type,
      materialID,
      product,
      charms,
      stones,
    });

    return res.json({ computedPrice: totalPrice, breakdown });
  } catch (err) {
    console.error("previewAccessory error:", err);
    return res.status(err.status || 500).json({
      message: err.status ? err.message : "Failed to preview accessory price",
      error: err.message,
      code: err.code,
      sqlMessage: err.sqlMessage,
    });
  }
};

exports.cancelAccessory = async (req, res) => {
  try {
    const { accessoryID } = req.params;
    if (!accessoryID) return res.status(400).json({ message: "Missing accessoryID" });

    const result = await cancelAccessoryReservation(accessoryID);
    return res.json({ message: "Reservation cancelled and inventory released", ...result });
  } catch (err) {
    console.error("cancelAccessory error:", err);
    return res.status(err.status || 500).json({
      message: err.status ? err.message : "Failed to cancel accessory reservation",
      error: err.message,
      code: err.code,
      sqlMessage: err.sqlMessage,
    });
  }
};
