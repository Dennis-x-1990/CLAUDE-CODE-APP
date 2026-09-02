import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

UPLOAD_ROOT = Path.home() / ".claude" / "webui-uploads"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

UPLOAD_EXT_BY_KIND = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "gif": ".gif", "webp": ".webp", "pdf": ".pdf", "txt": ".txt", "py": ".py", "js": ".js", "ts": ".ts", "html": ".html", "zip": ".zip"}

router = APIRouter()


def _session_exists(session_id: str) -> bool:
    if not session_id or len(session_id) > 64:
        return False
    for proj in PROJECTS_DIR.iterdir():
        try:
            if proj.is_dir() and (proj / f"{session_id}.jsonl").is_file():
                return True
        except OSError:
            continue
    return False


@router.post("/api/upload")
async def upload_file(sessionId: str = Form(...), kind: str = Form("txt"),
                      file: UploadFile = File(...)):
    if not sessionId or not _session_exists(sessionId):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot create upload dir: {e}")

    original_name = file.filename or "file"
    ext = UPLOAD_EXT_BY_KIND.get(kind or "", "")
    dest = UPLOAD_ROOT / f"{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100MB)")
    dest.write_bytes(data)

    return {"ok": True, "path": str(dest), "name": original_name, "size": len(data)}
