import os, json, shutil, subprocess, time, uuid, zipfile, base64
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

API_KEY = os.environ.get("JEWLZ_CFD_API_KEY", "CHANGE_ME_BEFORE_PUBLIC_USE")
OPENFOAM_CONTAINER = os.environ.get("OPENFOAM_CONTAINER", "jewlz-openfoam")
JOBS_DIR = Path(os.environ.get("JEWLZ_CFD_JOBS_DIR", "cfd_backend_jobs")).resolve()
MAX_UPLOAD_MB = float(os.environ.get("JEWLZ_MAX_UPLOAD_MB", "80"))
MAX_SOLVER_SECONDS = int(os.environ.get("JEWLZ_MAX_SOLVER_SECONDS", "1200"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Jewlz CFD Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

def require_api_key(x_api_key: Optional[str]):
    if not API_KEY or API_KEY == "CHANGE_ME_BEFORE_PUBLIC_USE":
        raise HTTPException(status_code=500, detail="Set JEWLZ_CFD_API_KEY before exposing the tunnel.")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized CFD backend request.")


def run_cmd(cmd, cwd=None, timeout=60):
    r = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "cmd": " ".join(cmd),
        "returncode": r.returncode,
        "stdout": r.stdout[-12000:],
        "stderr": r.stderr[-12000:],
    }


def docker_exec(command, timeout=60):
    return run_cmd(
        ["docker", "exec", OPENFOAM_CONTAINER, "bash", "-lc", command],
        timeout=timeout,
    )


def docker_cp_to_container(local_path: Path, container_path: str, timeout=240):
    return run_cmd(
        ["docker", "cp", str(local_path), f"{OPENFOAM_CONTAINER}:{container_path}"],
        timeout=timeout,
    )


def docker_cp_from_container(container_path: str, local_path: Path, timeout=240):
    return run_cmd(
        ["docker", "cp", f"{OPENFOAM_CONTAINER}:{container_path}", str(local_path)],
        timeout=timeout,
    )


def write_status(job_dir: Path, status: dict):
    status["updated_at"] = time.time()
    (job_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

def safe_extract_zip(zip_path: Path, extract_dir: Path):
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        for m in z.infolist():
            name = m.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise HTTPException(status_code=400, detail=f"Unsafe ZIP member path: {m.filename}")
            z.extract(m, extract_dir)


def find_openfoam_case(root: Path) -> Path:
    candidates = []

    for block_dict in root.rglob("blockMeshDict"):
        if block_dict.parent.name == "system":
            case_dir = block_dict.parent.parent
            score = 0

            if (case_dir / "constant").exists():
                score += 2
            if (case_dir / "0").exists():
                score += 2
            if (case_dir / "0.orig").exists():
                score += 1
            if (case_dir / "system" / "snappyHexMeshDict").exists():
                score += 1
            if (case_dir / "system" / "controlDict").exists():
                score += 1

            candidates.append((score, case_dir))

    if not candidates:
        raise RuntimeError("Could not locate OpenFOAM case in uploaded ZIP. Expected system/blockMeshDict.")

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def make_results_zip(job_dir: Path, job_id: str):
    zip_out = job_dir / f"{job_id}_results.zip"

    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in job_dir.rglob("*"):
            if p.is_file() and p != zip_out:
                z.write(p, p.relative_to(job_dir))

    return zip_out



def find_latest_internal_vtu(results_dir: Path) -> Optional[Path]:
    """Find the latest OpenFOAM foamToVTK internal.vtu file."""
    candidates = list(Path(results_dir).rglob("internal.vtu"))
    if not candidates:
        return None

    def timestep_score(path: Path):
        # Prefer the largest numeric timestep folder name, then newest mtime.
        score = -1.0
        for part in path.parts:
            try:
                score = max(score, float(part.split("_")[-1]))
            except Exception:
                pass
        return (score, path.stat().st_mtime)

    return sorted(candidates, key=timestep_score, reverse=True)[0]


def _sample_indices(n: int, max_points: int = 60000):
    if n <= max_points:
        return list(range(n))
    step = max(1, n // max_points)
    return list(range(0, n, step))[:max_points]


def generate_cfd_visual_assets(results_dir: Path, logs: dict, max_points: int = 60000):
    """
    Backend-side VTU parsing using PyVista/VTK.

    Creates:
      - cfd_visual_mesh.json for Streamlit interactive 3D Plotly
      - pressure_visual.png for PDF report
      - velocity_visual.png for PDF report

    PyVista is used instead of meshio because OpenFOAM foamToVTK can produce
    polyhedra/mixed-cell VTU files that meshio cannot load reliably.
    """
    assets = {"created": False, "message": ""}
    vtu_path = find_latest_internal_vtu(results_dir)
    if vtu_path is None:
        assets["message"] = "No internal.vtu found under copied VTK results."
        return assets

    try:
        import numpy as np
        import pyvista as pv
    except Exception as e:
        assets["message"] = f"pyvista/numpy unavailable on backend: {e}"
        return assets

    try:
        mesh = pv.read(str(vtu_path))
        assets["vtu_path"] = str(vtu_path)
        assets["mesh_type"] = type(mesh).__name__
        assets["n_points"] = int(getattr(mesh, "n_points", 0))
        assets["n_cells"] = int(getattr(mesh, "n_cells", 0))
        assets["point_arrays"] = list(mesh.point_data.keys())
        assets["cell_arrays"] = list(mesh.cell_data.keys())

        if mesh.n_points <= 0 and mesh.n_cells <= 0:
            assets["message"] = "VTU contains no usable points or cells."
            return assets

        def _as_vector_magnitude(arr):
            arr = np.asarray(arr, dtype=float)
            if arr.ndim == 2 and arr.shape[1] >= 3:
                return np.linalg.norm(arr[:, :3], axis=1)
            return np.abs(arr).reshape(-1)

        # Prefer point data when available. Otherwise use cell centers for OpenFOAM cell data.
        if "p" in mesh.point_data or "U" in mesh.point_data:
            cloud_points = np.asarray(mesh.points, dtype=float)[:, :3]
            if "p" in mesh.point_data:
                pressure = np.asarray(mesh.point_data["p"], dtype=float).reshape(-1)
            else:
                pressure = np.zeros(len(cloud_points), dtype=float)

            if "U" in mesh.point_data:
                velocity_mag = _as_vector_magnitude(mesh.point_data["U"])
            else:
                velocity_mag = np.zeros(len(cloud_points), dtype=float)

            source_kind = "point_data"

        elif "p" in mesh.cell_data or "U" in mesh.cell_data:
            centers = mesh.cell_centers()
            cloud_points = np.asarray(centers.points, dtype=float)[:, :3]
            if "p" in mesh.cell_data:
                pressure = np.asarray(mesh.cell_data["p"], dtype=float).reshape(-1)
            else:
                pressure = np.zeros(len(cloud_points), dtype=float)

            if "U" in mesh.cell_data:
                velocity_mag = _as_vector_magnitude(mesh.cell_data["U"])
            else:
                velocity_mag = np.zeros(len(cloud_points), dtype=float)

            source_kind = "cell_data_centers"
        else:
            # Last-resort: display mesh geometry with zero field values.
            if mesh.n_cells > 0:
                centers = mesh.cell_centers()
                cloud_points = np.asarray(centers.points, dtype=float)[:, :3]
            else:
                cloud_points = np.asarray(mesh.points, dtype=float)[:, :3]
            pressure = np.zeros(len(cloud_points), dtype=float)
            velocity_mag = np.zeros(len(cloud_points), dtype=float)
            source_kind = "geometry_only_no_p_or_U"
            assets["field_warning"] = "No p or U arrays found in point_data/cell_data; generated geometry-only visualization."

        n = min(len(cloud_points), len(pressure), len(velocity_mag))
        if n <= 0:
            assets["message"] = "No usable cloud points after field extraction."
            return assets

        cloud_points = cloud_points[:n]
        pressure = pressure[:n]
        velocity_mag = velocity_mag[:n]

        # Remove non-finite values so Plotly/matplotlib do not fail.
        finite = np.isfinite(cloud_points).all(axis=1) & np.isfinite(pressure) & np.isfinite(velocity_mag)
        cloud_points = cloud_points[finite]
        pressure = pressure[finite]
        velocity_mag = velocity_mag[finite]

        if len(cloud_points) == 0:
            assets["message"] = "All extracted CFD field points were non-finite."
            return assets

        idx = _sample_indices(len(cloud_points), max_points=max_points)
        cloud_points = np.asarray(cloud_points[idx], dtype=float)
        pressure = np.asarray(pressure[idx], dtype=float)
        velocity_mag = np.asarray(velocity_mag[idx], dtype=float)

        json_path = Path(results_dir) / "cfd_visual_mesh.json"
        payload = {
            "points": cloud_points.tolist(),
            "pressure": pressure.tolist(),
            "velocity_magnitude": velocity_mag.tolist(),
            "metadata": {
                "source_vtu": str(vtu_path),
                "source_kind": source_kind,
                "mesh_type": type(mesh).__name__,
                "point_arrays": list(mesh.point_data.keys()),
                "cell_arrays": list(mesh.cell_data.keys()),
                "n_points": int(len(cloud_points)),
                "pressure_min": float(np.nanmin(pressure)) if len(pressure) else 0.0,
                "pressure_max": float(np.nanmax(pressure)) if len(pressure) else 0.0,
                "velocity_min": float(np.nanmin(velocity_mag)) if len(velocity_mag) else 0.0,
                "velocity_max": float(np.nanmax(velocity_mag)) if len(velocity_mag) else 0.0,
            },
        }
        json_path.write_text(json.dumps(payload), encoding="utf-8")

        # PDF-only PNGs. Streamlit still uses cfd_visual_mesh.json for interactive 3D.
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            def save_png(vals, title, label, out_name):
                fig = plt.figure(figsize=(9, 6), dpi=150)
                ax = fig.add_subplot(111, projection="3d")
                sc = ax.scatter(
                    cloud_points[:, 0], cloud_points[:, 1], cloud_points[:, 2],
                    c=vals, s=2, alpha=0.75
                )
                ax.set_title(title)
                ax.set_xlabel("X [m]")
                ax.set_ylabel("Y [m]")
                ax.set_zlabel("Z [m]")
                cb = fig.colorbar(sc, ax=ax, shrink=0.65, pad=0.08)
                cb.set_label(label)
                fig.tight_layout()
                out_path = Path(results_dir) / out_name
                fig.savefig(out_path, bbox_inches="tight")
                plt.close(fig)
                return str(out_path)

            assets["pressure_png"] = save_png(pressure, "Remote OpenFOAM Pressure Field", "Pressure", "pressure_visual.png")
            assets["velocity_png"] = save_png(velocity_mag, "Remote OpenFOAM Velocity Magnitude", "|U| [m/s]", "velocity_visual.png")
        except Exception as e:
            assets["png_warning"] = str(e)

        assets.update({"created": True, "json": str(json_path), "message": "Backend CFD visual assets created with PyVista."})
        return assets
    except Exception as e:
        assets["message"] = f"Backend CFD visual asset generation failed with PyVista: {e}"
        return assets

@app.get("/health")
def health(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    docker_ok = run_cmd(["docker", "ps"], timeout=15)

    foam_check = docker_exec(
        "which blockMesh && which snappyHexMesh && which simpleFoam && which foamToVTK",
        timeout=20,
    )

    try:
        import meshio  # noqa: F401
        meshio_ok = True
    except Exception as e:
        meshio_ok = str(e)

    try:
        import matplotlib  # noqa: F401
        matplotlib_ok = True
    except Exception as e:
        matplotlib_ok = str(e)

    try:
        import pyvista  # noqa: F401
        pyvista_ok = True
    except Exception as e:
        pyvista_ok = str(e)

    try:
        import vtk  # noqa: F401
        vtk_ok = True
    except Exception as e:
        vtk_ok = str(e)

    return {
        "backend": "ready",
        "docker": docker_ok["returncode"] == 0,
        "openfoam_container": OPENFOAM_CONTAINER,
        "openfoam_ready": foam_check["returncode"] == 0,
        "meshio": meshio_ok,
        "matplotlib": matplotlib_ok,
        "pyvista": pyvista_ok,
        "vtk": vtk_ok,
        "openfoam_check_stdout": foam_check["stdout"],
        "openfoam_check_stderr": foam_check["stderr"],
    }


@app.post("/submit")
async def submit_smoke_test(
    geometry: UploadFile = File(...),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    return {
        "status": "received",
        "message": "Backend received geometry.",
        "filename": geometry.filename,
    }

@app.post("/run_case_zip")
async def run_case_zip(
    case_zip: UploadFile = File(None),
    zip_file: UploadFile = File(None),
    file: UploadFile = File(None),
    job_id: str = Form(""),
    notes: str = Form(""),
    run_snappy: bool = Form(True),
    run_solver: bool = Form(True),
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    upload = case_zip or zip_file or file

    if upload is None:
        raise HTTPException(
            status_code=400,
            detail="No case ZIP uploaded. Expected form field case_zip, zip_file, or file.",
        )

    data = await upload.read()

    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large. Max size is {MAX_UPLOAD_MB} MB.",
        )

    job_id = job_id or f"CFD-{uuid.uuid4().hex[:10].upper()}"
    job_dir = JOBS_DIR / job_id

    if job_dir.exists():
        shutil.rmtree(job_dir)

    job_dir.mkdir(parents=True, exist_ok=True)

    status = {
        "job_id": job_id,
        "status": "running",
        "progress": 1,
        "message": "Received OpenFOAM case ZIP.",
        "notes": notes,
        "created_at": time.time(),
    }

    write_status(job_dir, status)

    uploaded_zip = job_dir / "uploaded_case.zip"
    uploaded_zip.write_bytes(data)

    logs = {}

    try:
        check = docker_exec(
            "which blockMesh && which simpleFoam && which foamToVTK",
            timeout=20,
        )

        logs["openfoam_check"] = check

        if check["returncode"] != 0:
            raise RuntimeError(
                "OpenFOAM Docker container is not ready: " + check["stderr"]
            )

        extract_dir = job_dir / "extracted"
        safe_extract_zip(uploaded_zip, extract_dir)

        case_dir = find_openfoam_case(extract_dir)

        (job_dir / "case_detected.txt").write_text(
            str(case_dir),
            encoding="utf-8",
        )

        container_case = f"/tmp/{job_id}"

        docker_exec(f"rm -rf {container_case}", timeout=60)

        status.update(
            progress=20,
            message="Copying OpenFOAM case into Docker.",
        )
        write_status(job_dir, status)

        cp_in = docker_cp_to_container(
            case_dir,
            container_case,
            timeout=300,
        )

        logs["copy_case_to_docker"] = cp_in

        if cp_in["returncode"] != 0:
            raise RuntimeError(
                "Could not copy OpenFOAM case into Docker: " + cp_in["stderr"]
            )

        verify = docker_exec(
            f"test -f {container_case}/system/blockMeshDict && echo CASE_OK",
            timeout=30,
        )

        logs["verify_case"] = verify

        if verify["returncode"] != 0 or "CASE_OK" not in verify["stdout"]:
            listing = docker_exec(
                f"find {container_case} -maxdepth 4 -type f | head -100",
                timeout=30,
            )

            logs["case_listing_after_copy"] = listing

            raise RuntimeError(
                f"Could not locate copied OpenFOAM case in Docker at "
                f"{container_case}/system/blockMeshDict. "
                f"Listing: {listing['stdout']} {listing['stderr']}"
            )


        status.update(progress=35, message="Running blockMesh.")
        write_status(job_dir, status)

        logs["blockMesh"] = docker_exec(
            f"cd {container_case} && blockMesh 2>&1 | tee log.blockMesh",
            timeout=240,
        )

        if logs["blockMesh"]["returncode"] != 0:
            raise RuntimeError("blockMesh failed: " + logs["blockMesh"]["stderr"])

        if run_snappy:
            snappy_exists = docker_exec(
                f"test -f {container_case}/system/snappyHexMeshDict && echo YES || echo NO",
                timeout=20,
            )

            logs["snappy_exists"] = snappy_exists

            if "YES" in snappy_exists["stdout"]:
                status.update(progress=52, message="Running snappyHexMesh.")
                write_status(job_dir, status)

                logs["snappyHexMesh"] = docker_exec(
                    f"cd {container_case} && snappyHexMesh -overwrite 2>&1 | tee log.snappyHexMesh",
                    timeout=600,
                )

                if logs["snappyHexMesh"]["returncode"] != 0:
                    raise RuntimeError(
                        "snappyHexMesh failed: " + logs["snappyHexMesh"]["stderr"]
                    )
            else:
                logs["snappyHexMesh"] = {
                    "returncode": 0,
                    "stdout": "No snappyHexMeshDict found. Skipped.",
                    "stderr": "",
                }

        if run_solver:
            status.update(progress=70, message="Running simpleFoam solver.")
            write_status(job_dir, status)

            logs["simpleFoam"] = docker_exec(
                f"cd {container_case} && simpleFoam 2>&1 | tee log.simpleFoam",
                timeout=MAX_SOLVER_SECONDS,
            )

            if logs["simpleFoam"]["returncode"] != 0:
                raise RuntimeError(
                    "simpleFoam failed: " + logs["simpleFoam"]["stderr"]
                )

        status.update(progress=88, message="Exporting OpenFOAM results to VTK.")
        write_status(job_dir, status)

        logs["foamToVTK"] = docker_exec(
            f"cd {container_case} && foamToVTK 2>&1 | tee log.foamToVTK",
            timeout=360,
        )

        if logs["foamToVTK"]["returncode"] != 0:
            raise RuntimeError("foamToVTK failed: " + logs["foamToVTK"]["stderr"])

        results_dir = job_dir / "docker_results"
        results_dir.mkdir(exist_ok=True)

        cp_vtk = docker_cp_from_container(
            f"{container_case}/VTK",
            results_dir / "VTK",
            timeout=360,
        )

        logs["copy_vtk_from_docker"] = cp_vtk

        if cp_vtk["returncode"] != 0:
            raise RuntimeError(
                "Could not copy VTK results from Docker: " + cp_vtk["stderr"]
            )

        for log_name in [
            "log.blockMesh",
            "log.snappyHexMesh",
            "log.simpleFoam",
            "log.foamToVTK",
        ]:
            docker_cp_from_container(
                f"{container_case}/{log_name}",
                results_dir / log_name,
                timeout=120,
            )

        status.update(progress=94, message="Generating backend 3D visual assets.")
        write_status(job_dir, status)
        logs["visual_assets"] = generate_cfd_visual_assets(results_dir, logs)

        logs["visual_asset_files"] = []
        for root, dirs, files in os.walk(results_dir):
            for f in files:
                if f.endswith(".json") or f.endswith(".png"):
                    logs["visual_asset_files"].append(
                        os.path.join(root, f)
                    )

        (job_dir / "openfoam_logs.json").write_text(
            json.dumps(logs, indent=2),
            encoding="utf-8",
        )

        status.update(
            status="completed",
            progress=100,
            message="CFD job completed successfully.",
            vtk_available=True,
        )

        write_status(job_dir, status)

        return FileResponse(
            make_results_zip(job_dir, job_id),
            media_type="application/zip",
            filename=f"{job_id}_results.zip",
        )

    except Exception as exc:
        (job_dir / "openfoam_logs.json").write_text(
            json.dumps(logs, indent=2),
            encoding="utf-8",
        )

        status.update(
            status="failed",
            progress=100,
            message=str(exc),
            error=str(exc),
        )

        write_status(job_dir, status)

        return JSONResponse(status_code=500, content=status)


@app.get("/status/{job_id}")
def status(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    status_path = JOBS_DIR / job_id / "status.json"

    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Job not found.")

    return json.loads(status_path.read_text(encoding="utf-8"))
