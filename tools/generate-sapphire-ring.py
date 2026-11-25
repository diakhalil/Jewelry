"""Generate the SheCraft demo ring or a configured jewelry preview."""

import argparse
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector


OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "FrontEnd/shecraft-frontend/public/models/gold-sapphire-ring.glb"
)
MM_TO_M = 0.001
METALS = {
    "gold": ("Yellow gold", (0.83, 0.53, 0.19)),
    "silver": ("Silver", (0.77, 0.80, 0.85)),
    "rose-gold": ("Rose gold", (0.83, 0.42, 0.34)),
}
GEMS = {
    "sapphire": ((0.018, 0.085, 0.48), 1.76),
    "ruby": ((0.48, 0.014, 0.055), 1.76),
    "emerald": ((0.024, 0.38, 0.15), 1.58),
    "diamond": ((0.88, 0.94, 1.0), 2.42),
}
DIAMOND_COLORS = {
    "white": (0.88, 0.94, 1.0),
    "pink": (0.85, 0.22, 0.48),
    "blue": (0.06, 0.28, 0.85),
    "yellow": (0.95, 0.70, 0.10),
    "green": (0.08, 0.65, 0.30),
}


def repeat_tennis_setting(band, radius, count):
    """Fit evenly spaced miniature settings around the outside of the band."""
    prototype = [obj for obj in bpy.context.scene.objects if obj != band]
    scale = min(0.48, math.pi * (radius + 1) / count / 2.9)
    for index in range(count):
        angle = math.tau * index / count
        transform = (
            Matrix.Rotation(angle, 4, "Y")
            @ Matrix.Translation((0, 0, (radius + 0.55) * MM_TO_M))
            @ Matrix.Scale(scale, 4)
            @ Matrix.Translation((0, 0, -9.5 * MM_TO_M))
        )
        for obj in prototype:
            copy = obj.copy()
            copy.data = obj.data
            copy.name = f"Tennis {index + 1} {obj.name}"
            bpy.context.collection.objects.link(copy)
            copy.matrix_world = transform @ obj.matrix_world
    for obj in prototype:
        bpy.data.objects.remove(obj, do_unlink=True)


def pbr_material(name, color, metallic, roughness, ior, transmission=0.0):
    material = bpy.data.materials.new(name)
    material.diffuse_color = (*color, 1)
    if material.node_tree is None:
        material.use_nodes = True
    shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Metallic"].default_value = metallic
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["IOR"].default_value = ior
    shader.inputs["Transmission Weight"].default_value = transmission
    return material


def create_mesh(name, vertices_mm, faces, material, smooth=False):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(value * MM_TO_M for value in point) for point in vertices_mm], [], faces)
    mesh.update()

    topology = bmesh.new()
    topology.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(topology, faces=list(topology.faces))
    topology.to_mesh(mesh)
    topology.free()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material)
    for face in mesh.polygons:
        face.use_smooth = smooth
    return obj


def create_torus(name, radius, radial_width, axial_width, material, *, height=0, upright=False):
    sections, profile = 96, 16
    vertices, faces = [], []
    for section in range(sections):
        angle = math.tau * section / sections
        for step in range(profile):
            cross_angle = math.tau * step / profile
            offset = radius + radial_width * math.cos(cross_angle)
            x, y = offset * math.cos(angle), offset * math.sin(angle)
            depth = axial_width * math.sin(cross_angle)
            vertices.append((x, depth, y + height) if upright else (x, y, depth + height))
            next_section = (section + 1) % sections
            next_step = (step + 1) % profile
            faces.append((
                section * profile + step,
                next_section * profile + step,
                next_section * profile + next_step,
                section * profile + next_step,
            ))
    return create_mesh(name, vertices, faces, material, smooth=True)


def create_prong(index, start_mm, end_mm, material):
    start, end = Vector(start_mm) * MM_TO_M, Vector(end_mm) * MM_TO_M
    axis = end - start
    bpy.ops.mesh.primitive_cone_add(
        vertices=24,
        radius1=0.44 * MM_TO_M,
        radius2=0.34 * MM_TO_M,
        depth=axis.length,
        location=(start + end) / 2,
    )
    prong = bpy.context.object
    prong.name = f"Prong {index}"
    prong.rotation_euler = axis.to_track_quat("Z", "Y").to_euler()
    prong.data.materials.append(material)
    for face in prong.data.polygons:
        face.use_smooth = len(face.vertices) == 4

    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.36 * MM_TO_M, location=end)
    tip = bpy.context.object
    tip.name = f"Prong tip {index}"
    tip.data.materials.append(material)
    for face in tip.data.polygons:
        face.use_smooth = True


def create_gem(material):
    sides = 16
    vertices, faces = [], []
    for radius, height, angle_offset in (
        (1.68, 13.45, 0.5),
        (2.65, 12.56, 0),
        (2.65, 12.36, 0),
    ):
        for index in range(sides):
            angle = math.tau * (index + angle_offset) / sides
            vertices.append((radius * math.cos(angle), radius * math.sin(angle), height))

    culet = len(vertices)
    vertices.append((0, 0, 10.85))
    faces.append(tuple(range(sides)))
    for index in range(sides):
        following = (index + 1) % sides
        faces.extend((
            (index, sides + index, sides + following),
            (index, sides + following, following),
            (sides + index, 2 * sides + index, 2 * sides + following, sides + following),
            (2 * sides + index, culet, 2 * sides + following),
        ))
    return create_mesh(f"Round {material.name.lower()}", vertices, faces, material)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("ring", "bracelet"), default="ring")
    parser.add_argument("--metal", choices=METALS, default="gold")
    parser.add_argument("--gem", choices=(*GEMS, "none"))
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--style", choices=("solitaire", "tennis", "plain"), default="solitaire")
    parser.add_argument("--color", choices=DIAMOND_COLORS, default="white")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--size", type=float)
    parser.add_argument("--diameter", type=float, default=60)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    if args.style == "tennis" and not 8 <= args.count <= 32:
        parser.error("Tennis stone count must be 8–32.")
    if args.kind == "bracelet" and not 50 <= args.diameter <= 80:
        parser.error("Bracelet inner diameter must be 50–80 mm.")
    if args.size is not None and (not 3 <= args.size <= 13 or args.size * 2 % 1):
        parser.error("Use a US ring size from 3 to 13, in half-size increments.")
    gem = args.gem or ("sapphire" if args.kind == "ring" else "none")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.length_unit = "MILLIMETERS"

    metal_name, color = METALS[args.metal]
    metal = pbr_material(metal_name, color, metallic=1.0, roughness=0.22, ior=1.5)
    print(f"Building {args.kind} mesh and assigning {metal_name}", flush=True)
    if args.kind == "ring":
        radius = 9.5 if args.size is None else (11.63 + 0.8128 * args.size) / 2 + 1
        band = create_torus("Gold band" if args.metal == "gold" else "Ring band", radius, 1, 1.25, metal, upright=True)
        setting_offset = radius - 9.5
    else:
        radius = args.diameter / 2 + 1.7
        band = create_torus("Bangle", radius, 1.7, 2.2, metal, upright=True)
        setting_offset = radius - 9.5

    if gem != "none" and args.style != "plain":
        color, ior = GEMS[gem]
        if gem == "diamond":
            color = DIAMOND_COLORS[args.color]
        stone = pbr_material(
            gem.capitalize(), color, metallic=0, roughness=0.14, ior=ior, transmission=0.25
        )
        print(f"Mounting the {gem} in a four-prong setting", flush=True)
        create_torus("Gallery rail", 2.35, 0.35, 0.30, metal, height=11.8)
        for index, (x, y) in enumerate(((1, 1), (-1, 1), (-1, -1), (1, -1)), start=1):
            create_prong(index, (1.5 * x, 0.55 * y, 9.9), (1.75 * x, 1.75 * y, 12.78), metal)
        create_gem(stone)
        bpy.context.view_layer.update()
        if args.style == "tennis":
            repeat_tennis_setting(band, radius, args.count)
        else:
            for obj in bpy.context.scene.objects:
                if obj != band:
                    obj.location.z += setting_offset * MM_TO_M

    args.output.parent.mkdir(parents=True, exist_ok=True)
    status = bpy.ops.export_scene.gltf(filepath=str(args.output), export_format="GLB")
    if "FINISHED" not in status or not args.output.is_file():
        raise RuntimeError(f"GLB export failed: {status}")
    print(f"Exported {args.output.stat().st_size:,} bytes as GLB", flush=True)


if __name__ == "__main__":
    main()
