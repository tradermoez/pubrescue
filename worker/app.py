"""PubRescue API — upload .pub files, preview free, pay to download.

Flow:
  POST /api/jobs            multipart .pub files -> job_id, converts, returns preview URLs
  GET  /api/jobs/{id}       job status + previews
  POST /api/jobs/{id}/checkout  -> Stripe Checkout URL
  GET  /api/jobs/{id}/download  -> ZIP of all outputs (requires paid session)

State is in-memory + local disk (Render free tier; jobs are short-lived).
"""
import asyncio
import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont

import convert

DATA = Path(os.environ.get("DATA_DIR", "/tmp/pubrescue"))
DATA.mkdir(parents=True, exist_ok=True)

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_API = ("https://api-m.paypal.com"
              if os.environ.get("PAYPAL_ENV", "live") == "live"
              else "https://api-m.sandbox.paypal.com")
PRICE_CENTS = int(os.environ.get("PRICE_CENTS", "1900"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DEV_SKIP_PAYMENT = os.environ.get("DEV_SKIP_PAYMENT") == "1"
MAX_FILES = int(os.environ.get("MAX_FILES", "50"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
JOB_TTL_HOURS = float(os.environ.get("JOB_TTL_HOURS", "24"))

app = FastAPI(title="PubRescue")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict[str, dict] = {}  # job_id -> {created, files: [...], paid, stripe_session}


def _job_or_404(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found or expired")
    return job


def _watermark(png_path: Path) -> bytes:
    """Diagonal PREVIEW watermark over a preview image."""
    img = Image.open(png_path).convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            max(24, img.width // 12),
        )
    except OSError:
        font = ImageFont.load_default()
    text = "PREVIEW"
    step = max(150, img.height // 4)
    for y in range(0, img.height + step, step):
        draw.text((img.width // 6, y), text, font=font, fill=(120, 120, 120, 90))
    out = Image.alpha_composite(img, layer.rotate(20, center=(img.width // 2, img.height // 2)))
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(request: Request):
    form = await request.form()
    uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    if not uploads:
        raise HTTPException(400, "no files uploaded")
    if len(uploads) > MAX_FILES:
        raise HTTPException(400, f"max {MAX_FILES} files per job")

    formats = (form.get("formats") or "pdf").split(",")
    formats = [f.strip() for f in formats if f.strip() in convert.FORMATS] or ["pdf"]

    job_dir = convert.new_job_dir(DATA)
    job_id = job_dir.name
    job = {"created": time.time(), "paid": False, "stripe_session": None,
           "dir": str(job_dir), "files": [], "formats": formats}
    JOBS[job_id] = job

    for up in uploads:
        name = Path(up.filename or "file.pub").name
        if not name.lower().endswith(".pub"):
            job["files"].append({"name": name, "status": "error",
                                 "error": "not a .pub file"})
            continue
        raw = await up.read()
        if len(raw) > MAX_FILE_MB * 1024 * 1024:
            job["files"].append({"name": name, "status": "error",
                                 "error": f"file over {MAX_FILE_MB}MB"})
            continue
        fdir = job_dir / f"f{len(job['files'])}"
        fdir.mkdir()
        src = fdir / name
        src.write_bytes(raw)
        job["files"].append({"name": name, "status": "queued", "src": str(src),
                             "dir": str(fdir)})

    asyncio.get_running_loop().create_task(_process_job(job_id))
    return {"job_id": job_id, "files": [_public_file(f, i, job_id)
                                        for i, f in enumerate(job["files"])]}


async def _process_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return
    for f in job["files"]:
        if f["status"] != "queued":
            continue
        f["status"] = "converting"
        try:
            res = await convert.convert_file(
                Path(f["src"]), Path(f["dir"]), job["formats"])
            f["outputs"] = {fmt: str(p) for fmt, p in res["outputs"].items()}
            f["previews"] = [str(p) for p in res["previews"]]
            f["status"] = "done"
        except convert.ConversionError as e:
            f["status"] = "error"
            f["error"] = str(e)


def _public_file(f: dict, idx: int, job_id: str) -> dict:
    out = {"index": idx, "name": f["name"], "status": f["status"]}
    if f.get("error"):
        out["error"] = f["error"]
    if f.get("previews"):
        out["previews"] = [
            f"/api/jobs/{job_id}/preview/{idx}/{i}"
            for i in range(len(f["previews"]))
        ]
        out["formats"] = list(f.get("outputs", {}).keys())
    return out


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _job_or_404(job_id)
    return {"job_id": job_id, "paid": job["paid"],
            "files": [_public_file(f, i, job_id) for i, f in enumerate(job["files"])]}


@app.get("/api/jobs/{job_id}/preview/{file_idx}/{page}")
async def preview(job_id: str, file_idx: int, page: int):
    job = _job_or_404(job_id)
    try:
        f = job["files"][file_idx]
        path = Path(f["previews"][page])
    except (IndexError, KeyError):
        raise HTTPException(404, "preview not found")
    if job["paid"]:
        return FileResponse(path, media_type="image/png")
    return StreamingResponse(io.BytesIO(_watermark(path)), media_type="image/png")


async def _paypal_token(client) -> str:
    r = await client.post(
        f"{PAYPAL_API}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


@app.post("/api/jobs/{job_id}/checkout")
async def checkout(job_id: str, request: Request):
    job = _job_or_404(job_id)
    if job["paid"]:
        return {"paid": True}
    if DEV_SKIP_PAYMENT:
        job["paid"] = True
        return {"paid": True}
    if not (PAYPAL_CLIENT_ID and PAYPAL_SECRET):
        raise HTTPException(503, "payments not configured yet")
    import httpx
    body = {}
    if await request.body():
        body = await request.json()
    return_url = body.get("success_url") or f"{BASE_URL}/?job={job_id}&paid=1"
    cancel_url = body.get("cancel_url") or f"{BASE_URL}/?job={job_id}"
    order_req = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": job_id,
            "description": f"PubRescue conversion — {len(job['files'])} file(s)",
            "amount": {"currency_code": "USD",
                       "value": f"{PRICE_CENTS / 100:.2f}"},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "PubRescue",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }}},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _paypal_token(client)
        r = await client.post(
            f"{PAYPAL_API}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}"},
            json=order_req,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"PayPal order creation failed: {r.text[:300]}")
        order = r.json()
    job["paypal_order"] = order["id"]
    approve = next((l["href"] for l in order.get("links", [])
                    if l["rel"] in ("payer-action", "approve")), None)
    if not approve:
        raise HTTPException(502, "PayPal returned no approval link")
    return {"checkout_url": approve}


@app.post("/api/jobs/{job_id}/verify")
async def verify_payment(job_id: str):
    """Check/capture the PayPal order — no webhook config needed."""
    job = _job_or_404(job_id)
    if job["paid"]:
        return {"paid": True}
    order_id = job.get("paypal_order")
    if not order_id or not (PAYPAL_CLIENT_ID and PAYPAL_SECRET):
        return {"paid": False}
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _paypal_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.get(f"{PAYPAL_API}/v2/checkout/orders/{order_id}",
                             headers=headers)
        r.raise_for_status()
        status = r.json()["status"]
        if status == "APPROVED":
            # Buyer approved at PayPal; capture the funds now.
            r = await client.post(
                f"{PAYPAL_API}/v2/checkout/orders/{order_id}/capture",
                headers={**headers, "Content-Type": "application/json"},
            )
            if r.status_code in (200, 201):
                status = r.json()["status"]
    if status == "COMPLETED":
        job["paid"] = True
    return {"paid": job["paid"]}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str):
    job = _job_or_404(job_id)
    if not job["paid"]:
        raise HTTPException(402, "payment required")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in job["files"]:
            for fmt, p in f.get("outputs", {}).items():
                p = Path(p)
                if p.exists():
                    stem = Path(f["name"]).stem
                    z.write(p, f"{stem}/{stem}.{fmt}")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=pubrescue.zip"})


async def _cleanup_loop():
    while True:
        cutoff = time.time() - JOB_TTL_HOURS * 3600
        for job_id in list(JOBS):
            if JOBS[job_id]["created"] < cutoff:
                shutil.rmtree(JOBS[job_id]["dir"], ignore_errors=True)
                JOBS.pop(job_id, None)
        await asyncio.sleep(600)


@app.on_event("startup")
async def startup():
    asyncio.get_running_loop().create_task(_cleanup_loop())


# Serve the static frontend (mounted last so /api wins).
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
