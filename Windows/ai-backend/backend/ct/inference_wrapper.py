from pathlib import Path
import subprocess
import sys


BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_single_case(
    input_path: str,
    output_dir: str,
    device: str = "cuda",
    case_id: str = "",
    mode: str = "full_total",
    totalseg_fast: bool = False,
    totalseg_fastest: bool = False,
):
    """
    单病例推理包装函数。

    输入：
        input_path: 上传的 .nii.gz 路径
        output_dir: 当前病例输出目录

    输出：
        mask.nii.gz
        result.json
        overlay.png
        meshes/scene.glb
    """

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    cmd = [
        sys.executable,
        "-m",
        "backend.ct.run_totalseg",
        "--input",
        str(input_path),
        "--output",
        str(output_dir),
        "--device",
        device,
        "--mode",
        mode,
    ]
    if case_id:
        cmd += ["--case-id", case_id]
    if totalseg_fast:
        cmd.append("--totalseg-fast")
    if totalseg_fastest:
        cmd.append("--totalseg-fastest")
    result = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR.parent),
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Segmentation failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    return {
        "mask_path": str(output_dir / "mask.nii.gz"),
        "result_path": str(output_dir / "result.json"),
        "overlay_path": str(output_dir / "overlay.png"),
        "mesh_glb_path": str(output_dir / "meshes" / "scene.glb"),
        "mesh_manifest_path": str(output_dir / "meshes" / "mesh_manifest.json"),
    }
