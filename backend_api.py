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

@app.get("/health")
def health(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    docker_ok = run_cmd(["docker", "ps"], timeout=15)

    foam_check = docker_exec(
        "which blockMesh && which snappyHexMesh && which simpleFoam && which foamToVTK",
        timeout=20,
    )

    return {
        "backend": "ready",
        "docker": docker_ok["returncode"] == 0,
        "openfoam_container": OPENFOAM_CONTAINER,
        "openfoam_ready": foam_check["returncode"] == 0,
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

        zip_path = make_results_zip(job_dir, job_id)
        zip_b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")

        return JSONResponse(
            status_code=200,
            content={
                "job_id": job_id,
                "status": "completed",
                "progress": 100,
                "message": "CFD job completed successfully.",
                "vtk_available": True,
                "result_zip_filename": f"{job_id}_results.zip",
                "result_zip_base64": zip_b64,
                "download_url": f"/download/{job_id}",
            },
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


@app.get("/download/{job_id}")
def download_results(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    job_dir = JOBS_DIR / job_id
    zip_path = job_dir / f"{job_id}_results.zip"

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Result ZIP not found.")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
    )


@app.get("/status/{job_id}")
def status(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    status_path = JOBS_DIR / job_id / "status.json"

    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Job not found.")

    return json.loads(status_path.read_text(encoding="utf-8"))
