#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import struct
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import vtk
from PIL import Image
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

from backend.core.runtime import subprocess_runtime_env

AI_BACKEND_DIR = Path(__file__).resolve().parents[2]
TOTALSEG_PIPELINE = AI_BACKEND_DIR / "run_totalseg_pipeline.py"


def normalize_case_id(path: Path) -> str:
    name = path.name
    if name.endswith(".nii.gz"):
        return name[:-7]
    if name.endswith(".nii"):
        return name[:-4]
    return path.stem


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def resolve_device(device: str) -> tuple[str, str]:
    value = str(device).strip().lower()
    if value == "cpu":
        return "cpu", "cpu"
    if value in {"cuda", "gpu"}:
        return "cuda:0", "gpu:0"
    if value.startswith("cuda:"):
        return value, "gpu:" + value.split(":", 1)[1]
    return value, "gpu:0"


def tumor_label_from_label_map(label_map: dict[str, Any]) -> int | None:
    labels = label_map.get("label_map", label_map)
    if not isinstance(labels, dict):
        return None
    for key, value in labels.items():
        if str(value).startswith("tumor:"):
            return int(key)
    return None


def normalize_slice(slice_2d: np.ndarray) -> np.ndarray:
    arr = np.asarray(slice_2d, dtype=np.float32)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    lo, hi = np.percentile(finite, [1, 99])
    if hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)
    arr = np.clip((arr - lo) / (hi - lo), 0, 1)
    return np.round(arr * 255).astype(np.uint8)


def clip_bbox(mask_2d: np.ndarray, shape: tuple[int, int], margin: int = 28) -> tuple[slice, slice]:
    points = np.argwhere(mask_2d)
    if points.size == 0:
        return slice(0, shape[0]), slice(0, shape[1])
    lo = points.min(axis=0)
    hi = points.max(axis=0) + 1
    height, width = shape
    pad = max(margin, int(max(hi - lo) * 0.18))
    r0 = max(0, int(lo[0]) - pad)
    r1 = min(height, int(hi[0]) + pad)
    c0 = max(0, int(lo[1]) - pad)
    c1 = min(width, int(hi[1]) + pad)
    return slice(r0, r1), slice(c0, c1)


def resize_preview(rgb: np.ndarray, max_size: int = 760) -> np.ndarray:
    image = Image.fromarray(rgb)
    image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return np.asarray(image)


def plane_slice(volume: np.ndarray, plane: str, index: int) -> np.ndarray:
    if plane == "axial":
        return volume[:, :, index]
    if plane == "coronal":
        return volume[:, index, :]
    if plane == "sagittal":
        return volume[index, :, :]
    raise ValueError(f"Unsupported plane: {plane}")


def orient_preview(slice_2d: np.ndarray, plane: str) -> np.ndarray:
    if plane == "axial":
        return np.rot90(slice_2d)
    if plane == "coronal":
        return np.flipud(np.rot90(slice_2d))
    if plane == "sagittal":
        return np.flipud(np.rot90(slice_2d))
    return slice_2d


def label_color_uint8(name: str, index: int) -> np.ndarray:
    return np.asarray(color_for_label(name, index)[:3], dtype=np.float32) * 255.0


def overlay_slice_rgb(
    image_slice: np.ndarray,
    mask_slice: np.ndarray,
    labels: dict[int, str],
    tumor_label: int | None,
) -> np.ndarray:
    base = normalize_slice(image_slice)
    rgb = np.stack([base, base, base], axis=-1).astype(np.float32)

    for order, label_id in enumerate(sorted(labels)):
        if label_id == 0:
            continue
        current = mask_slice == label_id
        if not current.any():
            continue
        name = labels[label_id]
        is_tumor = tumor_label is not None and label_id == tumor_label
        color = label_color_uint8(name, order)
        alpha = 0.82 if is_tumor else 0.44
        rgb[current] = rgb[current] * (1.0 - alpha) + color * alpha

    return np.clip(rgb, 0, 255).astype(np.uint8)


def make_slice_preview(
    image: np.ndarray,
    mask: np.ndarray,
    labels: dict[int, str],
    tumor_label: int | None,
    plane: str,
    index: int,
) -> np.ndarray:
    image_slice = orient_preview(plane_slice(image, plane, index), plane)
    mask_slice = orient_preview(plane_slice(mask, plane, index), plane)
    tumor_slice = mask_slice == tumor_label if tumor_label is not None else np.zeros(mask_slice.shape, dtype=bool)
    content = (mask_slice > 0) | tumor_slice
    crop_rows, crop_cols = clip_bbox(content, mask_slice.shape)
    rgb = overlay_slice_rgb(image_slice, mask_slice, labels, tumor_label)
    rgb = rgb[crop_rows, crop_cols]
    return resize_preview(rgb)


def add_unique_slice(rows: list[dict[str, Any]], row: dict[str, Any]) -> None:
    key = (row["plane"], int(row["index"]))
    if key not in {(item["plane"], int(item["index"])) for item in rows}:
        rows.append(row)


def create_overlay_png(image_path: Path, mask_path: Path, label_map_path: Path, output_path: Path) -> None:
    image = np.asarray(nib.load(str(image_path)).dataobj)
    mask = np.rint(np.asarray(nib.load(str(mask_path)).dataobj)).astype(np.int32, copy=False)
    label_map = load_json(label_map_path)
    tumor_label = tumor_label_from_label_map(label_map)

    image = image[..., 0] if image.ndim == 4 else image
    if image.shape[:3] != mask.shape[:3]:
        raise ValueError(f"Image and mask shape mismatch: {image.shape[:3]} vs {mask.shape[:3]}")

    tumor = mask == tumor_label if tumor_label is not None else np.zeros(mask.shape, dtype=bool)
    if tumor.any():
        z = int(np.argmax(tumor.sum(axis=(0, 1))))
    else:
        z = int(mask.shape[2] // 2)

    base = normalize_slice(image[:, :, z])
    rgb = np.stack([base, base, base], axis=-1).astype(np.float32)
    organ = (mask[:, :, z] > 0) & ~tumor[:, :, z]
    tumor_slice = tumor[:, :, z]

    rgb[organ] = rgb[organ] * 0.55 + np.array([30, 144, 255], dtype=np.float32) * 0.45
    rgb[tumor_slice] = rgb[tumor_slice] * 0.35 + np.array([255, 64, 64], dtype=np.float32) * 0.65
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    # Rotate for a more conventional preview orientation.
    rgb = np.rot90(rgb)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(output_path)


def create_slice_gallery(image_path: Path, mask_path: Path, label_map_path: Path, output_dir: Path) -> dict[str, Any]:
    image = np.asarray(nib.load(str(image_path)).dataobj)
    mask = np.rint(np.asarray(nib.load(str(mask_path)).dataobj)).astype(np.int32, copy=False)
    label_payload = load_json(label_map_path)
    labels = {int(k): str(v) for k, v in label_payload.get("label_map", {}).items()}
    tumor_label = tumor_label_from_label_map(label_payload)

    image = image[..., 0] if image.ndim == 4 else image
    if image.shape[:3] != mask.shape[:3]:
        raise ValueError(f"Image and mask shape mismatch: {image.shape[:3]} vs {mask.shape[:3]}")

    tumor = mask == tumor_label if tumor_label is not None else np.zeros(mask.shape, dtype=bool)
    focus = tumor if tumor.any() else mask > 0
    if focus.any():
        coords = np.argwhere(focus)
        x_center, y_center, z_center = [int(round(v)) for v in coords.mean(axis=0)]
        axial_counts = focus.sum(axis=(0, 1))
        z_max = int(np.argmax(axial_counts))
        z_min = int(coords[:, 2].min())
        z_high = int(coords[:, 2].max())
    else:
        x_center, y_center, z_center = [int(v // 2) for v in mask.shape[:3]]
        z_max = z_center
        z_min = max(0, z_center - 12)
        z_high = min(mask.shape[2] - 1, z_center + 12)

    candidates: list[dict[str, Any]] = []
    add_unique_slice(candidates, {"plane": "axial", "index": z_max, "title": "轴位：分割结构最大截面"})
    add_unique_slice(candidates, {"plane": "axial", "index": z_center, "title": "轴位：分割结构中心层面"})
    add_unique_slice(candidates, {"plane": "axial", "index": z_min, "title": "轴位：分割范围上缘"})
    add_unique_slice(candidates, {"plane": "axial", "index": z_high, "title": "轴位：分割范围下缘"})
    add_unique_slice(candidates, {"plane": "coronal", "index": y_center, "title": "冠状位：全器官分割"})
    add_unique_slice(candidates, {"plane": "sagittal", "index": x_center, "title": "矢状位：全器官分割"})

    slice_dir = output_dir / "slices"
    slice_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for order, item in enumerate(candidates, start=1):
        plane = str(item["plane"])
        index = int(item["index"])
        filename = f"{order:02d}_{plane}_{index}.png"
        image_out = make_slice_preview(image, mask, labels, tumor_label, plane, index)
        Image.fromarray(image_out).save(slice_dir / filename)
        mask_slice = plane_slice(mask, plane, index)
        rows.append(
            {
                "filename": filename,
                "title": item["title"],
                "plane": plane,
                "index": index,
                "mask_pixels": int((mask_slice > 0).sum()),
            }
        )

    manifest_path = slice_dir / "slice_gallery.json"
    manifest = {
        "schema_version": "ppgl_slice_gallery_v1",
        "slice_count": len(rows),
        "slices": rows,
    }
    write_json(manifest_path, manifest)
    return {"slice_dir": str(slice_dir), "manifest_path": str(manifest_path), "slice_count": len(rows)}


def align4(data: bytearray) -> None:
    pad = (-len(data)) % 4
    if pad:
        data.extend(b"\x00" * pad)


def add_buffer_blob(blob: bytearray, payload: bytes) -> tuple[int, int]:
    align4(blob)
    offset = len(blob)
    blob.extend(payload)
    align4(blob)
    return offset, len(payload)


def color_for_label(name: str, index: int) -> list[float]:
    if name.startswith("tumor:"):
        return [1.0, 0.02, 0.06, 1.0]
    palette = {
        "aorta": [1.0, 0.16, 0.16, 0.82],
        "inferior_vena_cava": [0.15, 0.39, 0.92, 0.74],
        "portal_vein_and_splenic_vein": [0.22, 0.74, 0.97, 0.68],
        "kidney_left": [1.0, 0.36, 0.66, 0.76],
        "kidney_right": [0.0, 0.76, 0.84, 0.76],
        "adrenal_gland_left": [1.0, 0.83, 0.18, 0.92],
        "adrenal_gland_right": [1.0, 0.71, 0.02, 0.92],
        "liver": [0.20, 0.78, 0.35, 0.66],
        "spleen": [0.49, 0.23, 0.93, 0.66],
        "pancreas": [1.0, 0.54, 0.12, 0.68],
        "stomach": [1.0, 0.44, 0.57, 0.56],
        "duodenum": [0.12, 0.79, 0.64, 0.56],
        "small_bowel": [0.55, 0.82, 0.0, 0.50],
        "colon": [0.0, 0.61, 0.53, 0.50],
        "iliopsoas_left": [0.75, 0.46, 0.40, 0.48],
        "iliopsoas_right": [0.63, 0.40, 0.35, 0.48],
    }
    organ = name.split(":", 1)[-1]
    if organ.startswith("vertebrae_"):
        return [0.92, 0.93, 0.94, 0.46]
    if organ.startswith("iliac_artery"):
        return [0.91, 0.09, 0.11, 0.78]
    if organ.startswith("iliac_vena"):
        return [0.20, 0.36, 0.95, 0.70]
    return palette.get(organ, [0.35 + (index % 5) * 0.09, 0.62, 0.95 - (index % 4) * 0.08, 0.54])


def create_vtk_snap_surface(
    label_mask: np.ndarray,
    spacing: np.ndarray,
    center: np.ndarray,
    max_faces: int = 80_000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    points = np.argwhere(label_mask)
    margin = 2
    lower = np.maximum(points.min(axis=0) - margin, 0).astype(np.int32)
    upper = np.minimum(points.max(axis=0) + margin + 1, np.asarray(label_mask.shape)).astype(np.int32)
    slices = tuple(slice(int(lower[axis]), int(upper[axis])) for axis in range(3))
    cropped = np.ascontiguousarray(label_mask[slices], dtype=np.uint8)

    image = vtk.vtkImageData()
    image.SetDimensions(*(int(value) for value in cropped.shape))
    image.SetSpacing(*(float(value) for value in spacing))
    image.SetOrigin(*(float(value) for value in lower * spacing))
    vtk_scalars = numpy_to_vtk(
        cropped.ravel(order="F"),
        deep=True,
        array_type=vtk.VTK_UNSIGNED_CHAR,
    )
    image.GetPointData().SetScalars(vtk_scalars)

    surface = vtk.vtkDiscreteFlyingEdges3D()
    surface.SetInputData(image)
    surface.SetValue(0, 1)
    surface.ComputeNormalsOff()
    surface.ComputeGradientsOff()

    smooth = vtk.vtkWindowedSincPolyDataFilter()
    smooth.SetInputConnection(surface.GetOutputPort())
    smooth.SetNumberOfIterations(30)
    smooth.SetPassBand(0.08)
    smooth.FeatureEdgeSmoothingOff()
    smooth.BoundarySmoothingOn()
    smooth.NonManifoldSmoothingOn()
    smooth.NormalizeCoordinatesOn()
    smooth.Update()
    source_face_count = int(smooth.GetOutput().GetNumberOfPolys())
    if source_face_count < 1:
        raise RuntimeError("VTK produced an empty surface")

    geometry_port = smooth.GetOutputPort()
    if source_face_count > max_faces:
        decimate = vtk.vtkQuadricDecimation()
        decimate.SetInputConnection(geometry_port)
        decimate.SetTargetReduction(1.0 - (max_faces / source_face_count))
        decimate.VolumePreservationOn()
        decimate.Update()
        geometry_port = decimate.GetOutputPort()

    normals_filter = vtk.vtkPolyDataNormals()
    normals_filter.SetInputConnection(geometry_port)
    normals_filter.ComputePointNormalsOn()
    normals_filter.ComputeCellNormalsOff()
    normals_filter.SplittingOff()
    normals_filter.ConsistencyOn()
    normals_filter.AutoOrientNormalsOn()
    normals_filter.Update()
    polydata = normals_filter.GetOutput()

    vertices = vtk_to_numpy(polydata.GetPoints().GetData()).astype(np.float32, copy=False)
    cell_data = vtk_to_numpy(polydata.GetPolys().GetData())
    cells = cell_data.reshape(-1, 4)
    if not np.all(cells[:, 0] == 3):
        raise RuntimeError("VTK surface contains non-triangle cells")
    faces = cells[:, 1:].astype(np.uint32, copy=False)
    normals = vtk_to_numpy(polydata.GetPointData().GetNormals()).astype(np.float32, copy=False)
    vertices = vertices - center.reshape(1, 3)
    return vertices, faces, normals, source_face_count


def make_binary_glb(meshes: list[dict[str, Any]], output_path: Path) -> None:
    blob = bytearray()
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    gltf_meshes: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []

    for mesh_index, mesh in enumerate(meshes):
        material_index = len(materials)
        color = mesh["color"]
        alpha = float(color[3])
        materials.append(
            {
                "name": mesh["name"],
                "pbrMetallicRoughness": {
                    "baseColorFactor": color,
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.46,
                },
                "alphaMode": "BLEND" if alpha < 0.999 else "OPAQUE",
                "doubleSided": True,
            }
        )

        pos = np.asarray(mesh["vertices"], dtype=np.float32)
        normals = np.asarray(mesh["normals"], dtype=np.float32)
        faces = np.asarray(mesh["faces"], dtype=np.uint32).reshape(-1)

        pos_offset, pos_length = add_buffer_blob(blob, pos.tobytes(order="C"))
        pos_view = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": pos_offset, "byteLength": pos_length, "target": 34962})
        pos_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": pos_view,
                "byteOffset": 0,
                "componentType": 5126,
                "count": int(pos.shape[0]),
                "type": "VEC3",
                "min": [float(x) for x in pos.min(axis=0)],
                "max": [float(x) for x in pos.max(axis=0)],
            }
        )

        normal_offset, normal_length = add_buffer_blob(blob, normals.tobytes(order="C"))
        normal_view = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": normal_offset, "byteLength": normal_length, "target": 34962})
        normal_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": normal_view,
                "byteOffset": 0,
                "componentType": 5126,
                "count": int(normals.shape[0]),
                "type": "VEC3",
            }
        )

        index_offset, index_length = add_buffer_blob(blob, faces.tobytes(order="C"))
        index_view = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": index_offset, "byteLength": index_length, "target": 34963})
        index_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": index_view,
                "byteOffset": 0,
                "componentType": 5125,
                "count": int(faces.shape[0]),
                "type": "SCALAR",
            }
        )

        gltf_meshes.append(
            {
                "name": mesh["name"],
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_accessor, "NORMAL": normal_accessor},
                        "indices": index_accessor,
                        "material": material_index,
                    }
                ],
            }
        )
        nodes.append({"name": mesh["name"], "mesh": mesh_index})

    gltf = {
        "asset": {"version": "2.0", "generator": "PPGL TotalSegmentator mesh exporter"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": gltf_meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_bytes = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    bin_bytes = bytes(blob)
    bin_bytes += b"\x00" * ((4 - len(bin_bytes) % 4) % 4)

    total_length = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total_length))
        f.write(struct.pack("<I4s", len(json_bytes), b"JSON"))
        f.write(json_bytes)
        f.write(struct.pack("<I4s", len(bin_bytes), b"BIN\x00"))
        f.write(bin_bytes)


def gltfpack_executable() -> str:
    configured = os.environ.get("PPGL_GLTFPACK_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        command = shutil.which(configured)
        if command:
            return command
        raise RuntimeError(f"PPGL_GLTFPACK_BIN does not point to an executable: {configured}")

    command = shutil.which("gltfpack")
    if command:
        return command

    executable_names = ["gltfpack.cmd", "gltfpack.exe", "gltfpack"] if os.name == "nt" else ["gltfpack"]
    project_root = AI_BACKEND_DIR.parent
    for package_dir in [AI_BACKEND_DIR, project_root / "frontend-vue-prototype"]:
        for executable_name in executable_names:
            candidate = package_dir / "node_modules" / ".bin" / executable_name
            if candidate.is_file():
                return str(candidate)

    raise RuntimeError("gltfpack not found. Install it or configure PPGL_GLTFPACK_BIN.")


def meshopt_compress_glb(source_path: Path, output_path: Path, simplify_ratio: float = 1.0) -> None:
    gltfpack = gltfpack_executable()
    command_prefix = [gltfpack]
    if os.name == "nt" and Path(gltfpack).suffix.lower() in {".cmd", ".bat"}:
        configured_node = os.environ.get("PPGL_NODE_BIN", "").strip()
        node = ""
        if configured_node:
            configured_path = Path(configured_node).expanduser()
            if configured_path.is_file():
                node = str(configured_path.resolve())
            else:
                node = shutil.which(configured_node) or ""
            if not node:
                raise RuntimeError(
                    f"PPGL_NODE_BIN does not point to node.exe: {configured_node}"
                )
        else:
            node = shutil.which("node") or ""
        cli_path = Path(gltfpack).resolve().parent.parent / "gltfpack" / "cli.js"
        if not node:
            raise RuntimeError(
                "gltfpack requires Node.js on Windows. Configure PPGL_NODE_BIN to node.exe."
            )
        if not cli_path.is_file():
            raise RuntimeError(f"gltfpack cli.js not found: {cli_path}")
        command_prefix = [node, str(cli_path)]

    command = [
        *command_prefix,
        "-i", str(source_path),
        "-o", str(output_path),
        "-c",
        "-kn",
        "-km",
        "-vp", "16",
        "-vn", "12",
    ]
    if simplify_ratio < 0.999:
        command.extend(["-si", str(simplify_ratio), "-se", "0.01"])
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"gltfpack failed for {source_path.name}: {completed.stderr.strip()}")


def visible_by_default(label_name: str) -> bool:
    if label_name.startswith("tumor:"):
        return True
    organ = label_name.split(":", 1)[-1]
    return organ in {
        "adrenal_gland_left",
        "adrenal_gland_right",
        "aorta",
        "inferior_vena_cava",
        "portal_vein_and_splenic_vein",
        "kidney_left",
        "kidney_right",
        "liver",
        "spleen",
        "pancreas",
    }


def create_mesh_outputs(mask_path: Path, label_map_path: Path, output_dir: Path) -> dict[str, Any]:
    mask_img = nib.load(str(mask_path))
    mask = np.rint(np.asarray(mask_img.dataobj)).astype(np.uint16, copy=False)
    label_payload = load_json(label_map_path)
    labels = {int(k): str(v) for k, v in label_payload.get("label_map", {}).items()}
    spacing = np.asarray(mask_img.header.get_zooms()[:3], dtype=np.float32)
    center = (np.asarray(mask.shape[:3], dtype=np.float32) - 1.0) * spacing / 2.0
    mesh_dir = output_dir / "meshes"
    glb_path = mesh_dir / "scene.glb"
    manifest_path = mesh_dir / "mesh_manifest.json"
    organ_mesh_dir = mesh_dir / "organs"
    organ_mesh_dir.mkdir(parents=True, exist_ok=True)

    meshes: list[dict[str, Any]] = []
    manifest_meshes: list[dict[str, Any]] = []
    for label_id, name in sorted(labels.items()):
        if label_id == 0:
            continue
        label_mask = mask == label_id
        voxel_count = int(label_mask.sum())
        if voxel_count < 8:
            continue
        try:
            vertices, faces, normals, source_face_count = create_vtk_snap_surface(
                label_mask,
                spacing,
                center,
            )
        except (RuntimeError, ValueError):
            continue
        if len(vertices) == 0 or len(faces) == 0:
            continue
        color = color_for_label(name, len(meshes))
        mesh_payload = {
            "label_id": label_id,
            "name": name,
            "vertices": vertices,
            "faces": faces,
            "normals": normals,
            "color": color,
        }
        meshes.append(mesh_payload)
        high_path = organ_mesh_dir / f"{label_id}.glb"
        high_raw_path = organ_mesh_dir / f"{label_id}.raw.glb"
        make_binary_glb([mesh_payload], high_raw_path)
        meshopt_compress_glb(high_raw_path, high_path)
        high_raw_path.unlink(missing_ok=True)
        manifest_meshes.append(
            {
                "label_id": label_id,
                "name": name,
                "vertex_count": int(vertices.shape[0]),
                "face_count": int(faces.shape[0]),
                "voxel_count": voxel_count,
                "source_face_count": source_face_count,
                "surface_algorithm": "vtkDiscreteFlyingEdges3D",
                "smoothing_filter": "vtkWindowedSincPolyDataFilter",
                "smoothing_iterations": 30,
                "smoothing_pass_band": 0.08,
                "color": color,
                "visible_by_default": visible_by_default(name),
                "high_available": True,
                "high_file_size": high_path.stat().st_size,
                "high_vertex_count": int(vertices.shape[0]),
                "high_face_count": int(faces.shape[0]),
            }
        )

    if not meshes:
        raise RuntimeError("No meshes could be generated from final mask.")
    raw_glb_path = mesh_dir / "scene.raw.glb"
    make_binary_glb(meshes, raw_glb_path)
    meshopt_compress_glb(raw_glb_path, glb_path, simplify_ratio=0.35)
    raw_glb_path.unlink(missing_ok=True)
    manifest = {
        "mesh_schema_version": "ppgl_mesh_v3_vtk_snap",
        "overview_file_size": glb_path.stat().st_size,
        "overview_simplify_ratio": 0.35,
        "surface_pipeline": [
            "vtkDiscreteFlyingEdges3D",
            "vtkWindowedSincPolyDataFilter",
            "vtkQuadricDecimation",
            "vtkPolyDataNormals",
        ],
        "mesh_count": len(meshes),
        "meshes": manifest_meshes,
    }
    write_json(manifest_path, manifest)
    return {"glb_path": str(glb_path), "manifest_path": str(manifest_path), "mesh_count": len(meshes)}


def run_totalseg(args: argparse.Namespace, case_id: str, output_dir: Path) -> Path:
    if not TOTALSEG_PIPELINE.exists():
        raise FileNotFoundError(f"TotalSegmentator pipeline not found: {TOTALSEG_PIPELINE}")

    _device, totalseg_device = resolve_device(args.device)
    run_root = output_dir
    run_name = "totalseg"
    cmd = [
        sys.executable,
        str(TOTALSEG_PIPELINE),
        "--case-id",
        case_id,
        "--image",
        str(args.input),
        "--output-root",
        str(run_root),
        "--run-name",
        run_name,
        "--mode",
        str(args.mode),
        "--totalseg-device",
        totalseg_device,
    ]

    if args.force:
        cmd.append("--force")
    if args.totalseg_fast:
        cmd.append("--totalseg-fast")
    if args.totalseg_fastest:
        cmd.append("--totalseg-fastest")
    if str(args.totalseg_existing_dir).strip():
        cmd.extend(["--totalseg-existing-dir", str(args.totalseg_existing_dir)])

    completed = subprocess.run(
        cmd,
        cwd=str(AI_BACKEND_DIR),
        env=subprocess_runtime_env(),
        text=True,
        capture_output=True,
    )
    log_path = output_dir / "run_totalseg_subprocess.log"
    log_path.write_text(
        "COMMAND:\n"
        + " ".join(cmd)
        + "\n\nSTDOUT:\n"
        + completed.stdout
        + "\n\nSTDERR:\n"
        + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"TotalSegmentator pipeline failed. See log: {log_path}")
    return run_root / run_name


def collect_outputs(case_id: str, input_path: Path, output_dir: Path, run_dir: Path) -> dict[str, Any]:
    final_seg = run_dir / "fusion" / f"{case_id}_final_seg.nii.gz"
    label_map_path = run_dir / "fusion" / "label_map.json"
    metrics_path = run_dir / "analysis" / "clinical_metrics.json"
    llm_context_path = run_dir / "analysis" / "llm_context.json"
    report_path = run_dir / "analysis" / "report.md"
    timing_path = run_dir / "timing.json"

    for path in [final_seg, label_map_path, metrics_path]:
        if not path.exists():
            raise FileNotFoundError(f"Expected pipeline output missing: {path}")

    mask_path = output_dir / "mask.nii.gz"
    result_path = output_dir / "result.json"
    overlay_path = output_dir / "overlay.png"
    shutil.copy2(final_seg, mask_path)
    create_overlay_png(input_path, mask_path, label_map_path, overlay_path)
    slice_gallery = create_slice_gallery(input_path, mask_path, label_map_path, output_dir)
    mesh_outputs = create_mesh_outputs(mask_path, label_map_path, output_dir)

    label_payload = load_json(label_map_path)
    metrics = load_json(metrics_path)
    timing = load_json(timing_path) if timing_path.exists() else {}

    labels = label_payload.get("label_map", {})
    result = {
        "case_id": case_id,
        "status": "completed",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_image": str(input_path),
        "outputs": {
            "mask_path": str(mask_path),
            "overlay_path": str(overlay_path),
            "result_path": str(result_path),
            "totalseg_run_dir": str(run_dir),
            "label_map_path": str(label_map_path),
            "clinical_metrics_path": str(metrics_path),
            "llm_context_path": str(llm_context_path) if llm_context_path.exists() else "",
            "report_path": str(report_path) if report_path.exists() else "",
            "slice_gallery_path": slice_gallery["manifest_path"],
            "slice_dir": slice_gallery["slice_dir"],
            "mesh_glb_path": mesh_outputs["glb_path"],
            "mesh_manifest_path": mesh_outputs["manifest_path"],
        },
        "segmentation": {
            "label_count": len(labels),
            "label_map": labels,
            "tumor_priority": False,
            "mesh_count": mesh_outputs["mesh_count"],
        },
        "clinical_metrics": metrics,
        "summary": {
            "pipeline": metrics.get("pipeline"),
            "task": metrics.get("task"),
            "organ_count": metrics.get("organ_count"),
            "voxel_volume_mm3": metrics.get("voxel_volume_mm3"),
            "organs": metrics.get("organs", {}),
        },
        "timing": timing,
    }
    write_json(result_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CT full-organ segmentation inference entrypoint.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case-id", default="")
    parser.add_argument("--mode", choices=["jetson_fast", "abdomen", "full_total"], default="full_total")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--totalseg-existing-dir", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--totalseg-fast", action="store_true")
    parser.add_argument("--totalseg-fastest", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if str(args.totalseg_existing_dir).strip():
        args.totalseg_existing_dir = Path(str(args.totalseg_existing_dir)).expanduser().resolve()
    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    args.output.mkdir(parents=True, exist_ok=True)
    case_id = args.case_id.strip() or normalize_case_id(args.input)
    run_dir = run_totalseg(args, case_id, args.output)
    result = collect_outputs(case_id, args.input, args.output, run_dir)
    print(json.dumps({"status": "completed", "result_path": result["outputs"]["result_path"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
