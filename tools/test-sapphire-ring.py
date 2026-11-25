"""Re-import the exported GLB and verify the demo geometry and materials."""

import json
import math
from pathlib import Path
import struct

import bpy
from mathutils import Vector


MODEL = (
    Path(__file__).resolve().parents[1]
    / "FrontEnd/shecraft-frontend/public/models/gold-sapphire-ring.glb"
)


def bounds(obj, axis):
    values = [(obj.matrix_world @ Vector(point))[axis] * 1000 for point in obj.bound_box]
    return min(values), max(values)


def verify():
    data = MODEL.read_bytes()
    magic, version, total_length, json_length, chunk_type = struct.unpack_from("<4sIII4s", data)
    assert (magic, version, total_length, chunk_type) == (b"glTF", 2, len(data), b"JSON")
    materials = {item["name"]: item for item in json.loads(data[20:20 + json_length])["materials"]}
    assert materials["Yellow gold"]["pbrMetallicRoughness"].get("metallicFactor", 1) == 1
    assert materials["Sapphire"]["pbrMetallicRoughness"]["metallicFactor"] == 0
    sapphire_material = materials["Sapphire"]["extensions"]
    assert math.isclose(sapphire_material["KHR_materials_ior"]["ior"], 1.76, abs_tol=0.001)
    assert math.isclose(sapphire_material["KHR_materials_transmission"]["transmissionFactor"], 0.25)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    assert "FINISHED" in bpy.ops.import_scene.gltf(filepath=str(MODEL))
    bpy.context.view_layer.update()
    objects = {obj.name: obj for obj in bpy.context.scene.objects if obj.type == "MESH"}
    expected_names = {"Gold band", "Gallery rail", "Round sapphire"}
    expected_names.update(f"Prong {index}" for index in range(1, 5))
    expected_names.update(f"Prong tip {index}" for index in range(1, 5))
    assert set(objects) == expected_names
    assert all(obj.data.polygons and obj.data.materials for obj in objects.values())

    band, rail, gem = (objects[name] for name in ("Gold band", "Gallery rail", "Round sapphire"))
    band_bottom, band_top = bounds(band, 2)
    gem_bottom, gem_top = bounds(gem, 2)
    assert math.isclose(band_top - band_bottom, 21, abs_tol=0.02)
    gem_left, gem_right = bounds(gem, 0)
    assert math.isclose(gem_right - gem_left, 5.3, abs_tol=0.02)
    assert abs(gem_left + gem_right) < 0.02
    assert band_top < gem_bottom < bounds(rail, 2)[0] < bounds(rail, 2)[1] < gem_top

    for index in range(1, 5):
        prong_bottom, prong_top = bounds(objects[f"Prong {index}"], 2)
        tip_bottom, tip_top = bounds(objects[f"Prong tip {index}"], 2)
        assert prong_bottom < band_top < bounds(rail, 2)[0] < prong_top
        # Each tip crosses the sloping crown near the sapphire's girdle.
        assert tip_bottom < 12.56 < tip_top < gem_top
    print(f"Verified GLB 2.0, {len(objects)} meshes, 21 mm band, 5.3 mm sapphire, setting, and both PBR materials.")


if __name__ == "__main__":
    verify()
