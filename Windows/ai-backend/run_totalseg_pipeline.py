#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
from nibabel.processing import resample_from_to

from backend.core.runtime import find_console_script, subprocess_runtime_env


PROJECT_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("PPGL_DATA_ROOT", str(Path.home() / "ppgl-assist-data"))).expanduser().resolve()
DEFAULT_OUTPUT_ROOT = DATA_ROOT / "runs"
JETSON_FAST_ROIS = ("kidney_left", "kidney_right", "aorta")
ABDOMEN_ROIS = (
    "liver", "spleen", "pancreas", "stomach", "duodenum", "small_bowel", "colon",
    "kidney_left", "kidney_right", "adrenal_gland_left", "adrenal_gland_right", "aorta",
    "inferior_vena_cava", "portal_vein_and_splenic_vein", "iliac_artery_left",
    "iliac_artery_right", "iliac_vena_left", "iliac_vena_right", "iliopsoas_left",
    "iliopsoas_right", "vertebrae_T12", "vertebrae_L1", "vertebrae_L2", "vertebrae_L3",
    "vertebrae_L4", "vertebrae_L5",
)


def now_token() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_case_id(value: str | Path) -> str:
    name = Path(str(value)).name
    if name.endswith(".nii.gz"):
        return name[:-7]
    if name.endswith(".nii"):
        return name[:-4]
    return name


def ensure_dir(path: str | Path) -> Path:
    output = Path(path)
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_log(log_path: Path, message: str) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    print(message, flush=True)


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{int(minutes)}m{seconds:04.1f}s"


@contextmanager
def timed_stage(log_path: Path, timings: dict[str, float], key: str, label: str):
    started = time.perf_counter()
    append_log(log_path, f"[START] {label}")
    try:
        yield
    except Exception:
        elapsed = time.perf_counter() - started
        timings[key] = elapsed
        append_log(log_path, f"[FAILED] {label} after {format_elapsed(elapsed)}")
        raise
    else:
        elapsed = time.perf_counter() - started
        timings[key] = elapsed
        append_log(log_path, f"[DONE] {label} in {format_elapsed(elapsed)}")


def runtime_env(totalseg_root: Path | None) -> dict[str, str]:
    python_paths = [totalseg_root] if totalseg_root is not None else []
    env = subprocess_runtime_env(python_paths=python_paths)
    env["PYTHONNOUSERSITE"] = "1"
    configure_totalsegmentator_privacy(env)
    return env


def configure_totalsegmentator_privacy(env: dict[str, str]) -> None:
    """Keep TotalSegmentator state outside the user profile and disable telemetry."""
    home_dir = Path(
        env.get("TOTALSEG_HOME_DIR", str(DATA_ROOT / "totalsegmentator"))
    ).expanduser().resolve()
    home_dir.mkdir(parents=True, exist_ok=True)
    config_path = home_dir / "config.json"
    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    config.setdefault("totalseg_id", f"medvision_local_{uuid.uuid4().hex[:12]}")
    config.setdefault("prediction_counter", 0)
    config["send_usage_stats"] = False
    config["statistics_disclaimer_shown"] = True
    write_json(config_path, config)
    env["TOTALSEG_HOME_DIR"] = str(home_dir)


def rois_for_mode(mode: str) -> list[str] | None:
    if mode == "jetson_fast":
        return list(JETSON_FAST_ROIS)
    if mode == "abdomen":
        return list(ABDOMEN_ROIS)
    if mode == "full_total":
        return None
    raise ValueError(f"Unknown mode: {mode}")


def totalsegmentator_executable() -> str:
    return find_console_script("TotalSegmentator")


def load_mask_on_reference(mask_path: Path, reference_img: nib.Nifti1Image) -> np.ndarray:
    mask_img = nib.load(str(mask_path))
    if tuple(mask_img.shape[:3]) != tuple(reference_img.shape[:3]) or not np.allclose(mask_img.affine, reference_img.affine):
        mask_img = resample_from_to(mask_img, (reference_img.shape[:3], reference_img.affine), order=0)
    return np.asarray(mask_img.dataobj) > 0


def run_totalseg(args: argparse.Namespace, case_dir: Path, log_path: Path) -> Path:
    raw_dir = ensure_dir(case_dir / "total" / "raw_masks")
    summary_path = case_dir / "total" / "totalseg_summary.json"
    source_dir = Path(args.totalseg_existing_dir) if args.totalseg_existing_dir else None

    if args.force and raw_dir.exists():
        shutil.rmtree(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)

    if source_dir is not None:
        if not source_dir.is_dir():
            raise FileNotFoundError(f"--totalseg-existing-dir not found: {source_dir}")
        roi_subset = rois_for_mode(args.mode)
        source_masks = sorted(source_dir.glob("*.nii.gz"))
        selected = set(roi_subset or [path.name[:-7] for path in source_masks])
        copied: list[str] = []
        for source in source_masks:
            organ_name = source.name[:-7]
            if organ_name in selected:
                shutil.copy2(source, raw_dir / source.name)
                copied.append(source.name)
        if not copied:
            raise FileNotFoundError(f"No matching TotalSegmentator masks found in: {source_dir}")
        write_json(summary_path, {
            "case_id": args.case_id, "mode": args.mode, "source": str(source_dir),
            "copied_mask_count": len(copied), "raw_dir": str(raw_dir), "masks": copied,
        })
        append_log(log_path, f"Reused {len(copied)} TotalSegmentator masks from: {source_dir}")
        return raw_dir

    if list(raw_dir.glob("*.nii.gz")) and not args.force:
        append_log(log_path, f"TotalSegmentator masks already exist: {raw_dir}")
        return raw_dir

    command = [
        totalsegmentator_executable(), "-i", str(args.image), "-o", str(raw_dir),
        "-ta", "total", "-d", args.totalseg_device,
    ]
    if args.totalseg_quiet:
        command.append("-q")
    roi_subset = rois_for_mode(args.mode)
    if roi_subset:
        command.extend(["-rs", *roi_subset])
        weights_root = Path(os.environ.get("TOTALSEG_WEIGHTS_PATH", Path.home() / ".totalsegmentator/nnunet/results")).expanduser()
        crop_6mm = weights_root / "Dataset298_TotalSegmentator_total_6mm_1559subj"
        crop_3mm = weights_root / "Dataset297_TotalSegmentator_total_3mm_1559subj"
        if args.totalseg_robust_crop or (not crop_6mm.exists() and crop_3mm.exists()):
            command.append("-rc")
            append_log(log_path, "Using TotalSegmentator robust crop (-rc).")
    if args.totalseg_fast:
        command.append("-f")
    if args.totalseg_fastest:
        command.append("-ff")

    append_log(log_path, "Running TotalSegmentator: " + " ".join(command))
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.run(
            command,
            cwd=str(PROJECT_DIR),
            env=runtime_env(args.totalseg_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=True,
        )

    masks = sorted(path.name for path in raw_dir.glob("*.nii.gz"))
    write_json(summary_path, {
        "case_id": args.case_id, "mode": args.mode, "roi_subset": roi_subset or "all",
        "mask_count": len(masks), "raw_dir": str(raw_dir), "masks": masks,
    })
    append_log(log_path, f"TotalSegmentator masks saved: {raw_dir} ({len(masks)} files)")
    return raw_dir


def compose_output(args: argparse.Namespace, case_dir: Path, total_dir: Path, log_path: Path) -> None:
    fusion_dir = ensure_dir(case_dir / "fusion")
    analysis_dir = ensure_dir(case_dir / "analysis")
    final_path = fusion_dir / f"{args.case_id}_final_seg.nii.gz"
    label_map_path = fusion_dir / "label_map.json"
    metrics_path = analysis_dir / "clinical_metrics.json"
    report_path = analysis_dir / "report.md"

    reference_img = nib.load(str(args.image))
    final = np.zeros(reference_img.shape[:3], dtype=np.uint16)
    voxel_volume_mm3 = float(np.prod(reference_img.header.get_zooms()[:3]))
    label_map: dict[int, str] = {0: "background"}
    organs: dict[str, dict[str, float | int]] = {}

    next_label = 1
    for mask_path in sorted(total_dir.glob("*.nii.gz")):
        organ_name = mask_path.name[:-7]
        mask = load_mask_on_reference(mask_path, reference_img)
        voxel_count = int(mask.sum())
        if not voxel_count:
            continue
        final[mask] = next_label
        label_map[next_label] = f"totalseg:{organ_name}"
        organs[organ_name] = {
            "voxel_count": voxel_count,
            "volume_ml": round(voxel_count * voxel_volume_mm3 / 1000.0, 3),
        }
        next_label += 1
    if len(label_map) == 1:
        raise RuntimeError("TotalSegmentator did not produce any non-empty organ masks.")

    header = reference_img.header.copy()
    header.set_data_dtype(np.uint16)
    nib.save(nib.Nifti1Image(final, reference_img.affine, header), str(final_path))
    write_json(label_map_path, {
        "case_id": args.case_id, "pipeline": "totalsegmentator_only", "task": "total",
        "final_seg": str(final_path), "tumor_priority": False,
        "label_map": {str(key): value for key, value in label_map.items()},
    })
    metrics = {
        "case_id": args.case_id, "pipeline": "totalsegmentator_only", "task": "total",
        "mode": args.mode, "image": str(args.image), "voxel_volume_mm3": voxel_volume_mm3,
        "organ_count": len(organs), "organs": organs,
        "outputs": {"final_seg": str(final_path), "totalseg_dir": str(total_dir), "report": str(report_path)},
    }
    write_json(metrics_path, metrics)
    report_lines = [
        f"# Case {args.case_id}", "", "- Pipeline: `TotalSegmentator only`", "- Task: `total`",
        f"- Segmented structures: `{len(organs)}`", f"- Combined label: `{final_path}`", "", "## Organ volumes",
    ]
    report_lines.extend(f"- {name}: {values['volume_ml']:.3f} ml" for name, values in sorted(organs.items()))
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    append_log(log_path, f"TotalSegmentator combined label saved: {final_path}")


def prepare_case_dir(args: argparse.Namespace) -> tuple[Path, Path]:
    run_name = args.run_name or f"{now_token()}__{args.case_id}__{args.mode}"
    case_dir = ensure_dir(args.output_root / run_name)
    log_path = ensure_dir(case_dir / "logs") / "pipeline.log"
    input_dir = ensure_dir(case_dir / "input")
    image_link = input_dir / args.image.name
    if not image_link.exists():
        try:
            image_link.symlink_to(args.image)
        except OSError:
            shutil.copy2(args.image, image_link)
    write_json(case_dir / "run_config.json", {
        "timestamp": datetime.now().isoformat(timespec="seconds"), "argv": sys.argv,
        "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "case_dir": str(case_dir),
    })
    return case_dir, log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone TotalSegmentator full-organ pipeline.")
    parser.add_argument("--case-id", default="")
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), type=Path)
    parser.add_argument("--run-name", default="")
    parser.add_argument("--mode", choices=["jetson_fast", "abdomen", "full_total"], default="full_total")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--totalseg-root", default="", type=str)
    parser.add_argument("--totalseg-existing-dir", default="")
    parser.add_argument("--totalseg-device", default="gpu:0")
    parser.add_argument("--totalseg-fast", action="store_true")
    parser.add_argument("--totalseg-fastest", action="store_true")
    parser.add_argument("--totalseg-quiet", action="store_true")
    parser.add_argument("--totalseg-robust-crop", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.image = args.image.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    args.totalseg_root = Path(args.totalseg_root).expanduser().resolve() if args.totalseg_root else None
    args.totalseg_existing_dir = str(Path(args.totalseg_existing_dir).expanduser().resolve()) if args.totalseg_existing_dir else ""
    if not args.image.is_file():
        raise FileNotFoundError(f"image not found: {args.image}")
    if args.totalseg_root is not None and not args.totalseg_root.is_dir():
        raise FileNotFoundError(f"totalseg-root not found: {args.totalseg_root}")
    if not args.case_id:
        args.case_id = normalize_case_id(args.image)
    if not args.totalseg_existing_dir:
        totalsegmentator_executable()

    case_dir, log_path = prepare_case_dir(args)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    append_log(log_path, "Pipeline: TotalSegmentator only")
    with timed_stage(log_path, timings, "totalseg", "1/2 TotalSegmentator full-organ segmentation"):
        total_dir = run_totalseg(args, case_dir, log_path)
    with timed_stage(log_path, timings, "output", "2/2 TotalSegmentator output assembly"):
        compose_output(args, case_dir, total_dir, log_path)
    timings["total"] = time.perf_counter() - started
    timing_path = case_dir / "timing.json"
    write_json(timing_path, {
        "case_id": args.case_id,
        "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
        "timings_human": {key: format_elapsed(value) for key, value in timings.items()},
    })
    append_log(log_path, f"Timing saved: {timing_path}")
    print(f"TotalSegmentator pipeline finished: {case_dir}")


if __name__ == "__main__":
    main()
