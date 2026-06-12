"""LibreOffice headless conversion wrapper for .pub files."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
PDFTOPPM = shutil.which("pdftoppm") or "/usr/bin/pdftoppm"

# 512MB Render instance: one LibreOffice at a time.
_sema = asyncio.Semaphore(int(os.environ.get("CONVERT_CONCURRENCY", "1")))

FORMATS = {"pdf", "png", "svg", "odg"}
CONVERT_TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT", "120"))


class ConversionError(Exception):
    pass


def _run_soffice(src: Path, outdir: Path, fmt: str) -> Path:
    """Convert src to fmt inside outdir. Blocking; run in thread."""
    # Isolated profile per invocation so parallel/crashed runs don't poison each other.
    profile = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    try:
        cmd = [
            SOFFICE,
            f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--nolockcheck",
            "--convert-to", fmt,
            "--outdir", str(outdir),
            str(src),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=CONVERT_TIMEOUT, text=True
        )
        out = outdir / (src.stem + "." + fmt)
        # soffice exits 0 even on silent failure; trust only the output file.
        if not out.exists() or out.stat().st_size == 0:
            raise ConversionError(
                f"conversion to {fmt} produced no output "
                f"(rc={proc.returncode}, stderr={proc.stderr[-400:]})"
            )
        return out
    except subprocess.TimeoutExpired:
        raise ConversionError(f"conversion to {fmt} timed out after {CONVERT_TIMEOUT}s")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _render_previews(pdf: Path, outdir: Path, max_pages: int = 10) -> list[Path]:
    """Render low-res preview PNGs from a PDF. Blocking; run in thread."""
    prefix = outdir / "preview"
    subprocess.run(
        [PDFTOPPM, "-png", "-r", "72", "-f", "1", "-l", str(max_pages),
         str(pdf), str(prefix)],
        capture_output=True, timeout=60, check=True,
    )
    return sorted(outdir.glob("preview-*.png"))


async def convert_file(src: Path, outdir: Path, formats: list[str]) -> dict:
    """Convert one .pub file to the requested formats + preview PNGs.

    Returns {"outputs": {fmt: path}, "previews": [paths]}.
    """
    bad = set(formats) - FORMATS
    if bad:
        raise ConversionError(f"unsupported formats: {bad}")
    outputs: dict[str, Path] = {}
    async with _sema:
        loop = asyncio.get_running_loop()
        # PDF always — it drives the preview.
        pdf = await loop.run_in_executor(None, _run_soffice, src, outdir, "pdf")
        outputs["pdf"] = pdf
        for fmt in formats:
            if fmt == "pdf":
                continue
            try:
                outputs[fmt] = await loop.run_in_executor(
                    None, _run_soffice, src, outdir, fmt
                )
            except ConversionError:
                # Secondary format failures shouldn't sink the file.
                pass
        prevdir = outdir / "previews"
        prevdir.mkdir(exist_ok=True)
        previews = await loop.run_in_executor(None, _render_previews, pdf, prevdir)
    return {"outputs": outputs, "previews": previews}


def new_job_dir(base: Path) -> Path:
    d = base / uuid.uuid4().hex
    d.mkdir(parents=True)
    return d
