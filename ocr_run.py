"""Local, pinned OCR extraction run over staged public originals.

Writes only inside public-originals/<application>/ocr/<run_id>/. Produces one
extraction run with per-page records (status, confidences, hashes). It does not
decide that text is accurate: pages are 'ocr_ok' only by confidence thresholds,
and a document stays out of the usable corpus until a human spot-check is recorded.
"""
import argparse, csv, hashlib, io, json, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from pg_tool import paths

import os
def _find_tesseract():
    """PG_TESSERACT (path to the executable) wins; otherwise the first 'tesseract' on PATH. None when not installed."""
    v = os.environ.get("PG_TESSERACT") or shutil.which("tesseract")
    return Path(v) if v else None
TESSERACT = _find_tesseract()
DPI = 300
LANG = "eng"
PSM = "6"
OK_MEAN = 85.0        # mean word confidence for ocr_ok
LOW_WORD = 60.0       # a word below this counts as low-confidence
OK_LOW_FRACTION = 0.10

def sha(b): return hashlib.sha256(b).hexdigest()

def tesseract_version():
    if TESSERACT is None: raise RuntimeError("Tesseract OCR not found (set PG_TESSERACT or add it to PATH)")
    out = subprocess.run([str(TESSERACT), "--version"], capture_output=True, text=True).stdout
    return out.splitlines()[0].strip()

def ocr_page(png_bytes):
    proc = subprocess.run([str(TESSERACT), "stdin", "stdout", "-l", LANG, "--psm", PSM, "tsv"],
                          input=png_bytes, capture_output=True)
    if proc.returncode != 0:
        return None, proc.stderr.decode(errors="replace")[:500]
    rows = list(csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", errors="replace")), delimiter="\t", quoting=csv.QUOTE_NONE))
    return rows, ""

def page_text(rows):
    lines, key, cur = [], None, []
    for r in rows:
        if r.get("level") != "5" or not r.get("text", "").strip(): continue
        k = (r["block_num"], r["par_num"], r["line_num"])
        if k != key and cur: lines.append(" ".join(cur)); cur = []
        key = k; cur.append(r["text"])
    if cur: lines.append(" ".join(cur))
    return "\n".join(lines)

def run(app_dir, doc_ids, run_id):
    import pymupdf
    out = app_dir / "ocr" / run_id
    out.mkdir(parents=True, exist_ok=False)
    params = {"engine": tesseract_version(), "lang": LANG, "psm": PSM, "dpi": DPI, "renderer": "pymupdf " + pymupdf.__version__ + " / mupdf " + pymupdf.mupdf_version,
              "thresholds": {"ok_mean": OK_MEAN, "low_word": LOW_WORD, "ok_low_fraction": OK_LOW_FRACTION}}
    params_hash = sha(json.dumps(params, sort_keys=True).encode())
    receipt = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), "params": params, "params_hash": params_hash,
               "status": "OCR_RUN_UNREVIEWED", "documents": []}
    for doc_id in doc_ids:
        pdf = app_dir / (doc_id + ".pdf")
        original_sha = sha(pdf.read_bytes())
        doc = pymupdf.open(pdf)
        pages, texts = [], []
        for i, page in enumerate(doc, start=1):
            png = page.get_pixmap(dpi=DPI).tobytes("png")
            rows, err = ocr_page(png)
            rec = {"page": i, "run_id": run_id, "warnings": []}
            if rows is None:
                rec.update(status="failed", warnings=[err]); texts.append(""); pages.append(rec); continue
            words = [r for r in rows if r.get("level") == "5" and r.get("text", "").strip()]
            confs = [float(r["conf"]) for r in words if r.get("conf") not in (None, "", "-1")]
            text = page_text(rows); texts.append(text)
            low = sum(1 for c in confs if c < LOW_WORD)
            rec.update(text_sha256=sha(text.encode("utf-8")), char_count=len(text), word_count=len(words),
                       mean_confidence=round(sum(confs) / len(confs), 2) if confs else None,
                       min_confidence=min(confs) if confs else None, low_confidence_words=low)
            if not words: rec["status"] = "empty"
            elif rec["mean_confidence"] >= OK_MEAN and low / len(words) <= OK_LOW_FRACTION: rec["status"] = "ocr_ok"
            else: rec["status"] = "ocr_low_confidence"
            pages.append(rec)
        full = "\f".join(texts)
        (out / (doc_id + ".txt")).write_text(full, encoding="utf-8", newline="\n")
        text_sha = sha((out / (doc_id + ".txt")).read_bytes())
        (out / (doc_id + ".pages.json")).write_text(json.dumps({"document_id": doc_id, "original_sha256": original_sha, "run_id": run_id, "text_sha256": text_sha, "pages": pages}, indent=2) + "\n", encoding="utf-8")
        receipt["documents"].append({"document_id": doc_id, "original_sha256": original_sha, "text_sha256": text_sha, "pages": len(pages),
                                     "by_status": {s: sum(1 for p in pages if p["status"] == s) for s in sorted({p["status"] for p in pages})}})
        print(doc_id, receipt["documents"][-1]["by_status"], file=sys.stderr)
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    (out / "run-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("app_dir"); ap.add_argument("run_id"); ap.add_argument("doc_ids", nargs="+")
    a = ap.parse_args()
    if TESSERACT is None or not TESSERACT.exists(): sys.exit("Tesseract OCR not found: install it and put it on PATH, or set PG_TESSERACT to the tesseract executable. PyMuPDF is also required (pip install pymupdf).")
    r = run(paths.checked_or_exit(a.app_dir, 'the application folder'), a.doc_ids, a.run_id)
    print(json.dumps({d["document_id"]: d["by_status"] for d in r["documents"]}, indent=1))
