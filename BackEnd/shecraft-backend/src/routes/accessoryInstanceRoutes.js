const router = require("express").Router();
const {
  createAccessory,
  previewAccessory,
  cancelAccessory,
} = require("../controllers/accessoryInstanceController");

// Read-only accessory price preview
router.post("/preview", previewAccessory);

// create accessory instance
router.post("/", createAccessory);

// cancel accessory reservation (release qty back)
router.post("/:accessoryID/cancel", cancelAccessory);

module.exports = router;
