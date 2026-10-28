"""安全的 CAD 项目文件清单与 ZIP 暂存提取。"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, zipfile, stat
from pathlib import Path, PureWindowsPath

CAD_EXTENSIONS = {".sldprt", ".sldasm", ".slddrw", ".step", ".stp", ".iges", ".igs", ".dxf", ".dwg", ".stl"}
EXCLUDED_NAMES = {"agent", "output", "outputs", ".git", "__pycache__"}

def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()

def _safe_zip_members(z: zipfile.ZipFile, *, max_member_size=2*1024**3, max_total_size=8*1024**3):
    seen, total, rows = set(), 0, []
    for info in z.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"): continue
        p = Path(name)
        win = PureWindowsPath(name)
        if p.is_absolute() or win.is_absolute() or win.drive or ".." in p.parts or ":" in name or any(part == "" for part in p.parts) or any(part.rstrip(" .") != part for part in p.parts):
            raise ValueError(f"ZIP 路径不安全: {info.filename}")
        devices = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
        if any(part.split('.', 1)[0].casefold() in devices for part in p.parts):
            raise ValueError(f"ZIP 设备名不安全: {info.filename}")
        mode = (info.external_attr >> 16) & 0xF000
        if mode == 0xA000: raise ValueError(f"ZIP 符号链接被拒绝: {info.filename}")
        key = name.casefold()
        if key in seen: raise ValueError(f"ZIP 大小写路径冲突: {info.filename}")
        if info.file_size > max_member_size or total + info.file_size > max_total_size:
            raise ValueError(f"ZIP 大小超限: {info.filename}")
        seen.add(key); total += info.file_size
        rows.append({"name": name, "size_bytes": info.file_size, "crc": info.CRC})
    return rows

def inventory_project(root, output=None, extract_root=None, *, max_zip_member_size=2*1024**3, max_zip_total_size=8*1024**3):
    root = Path(root).expanduser().resolve()
    if not root.is_dir(): raise NotADirectoryError(root)
    files, zips = [], []
    excluded_roots = []
    if extract_root: excluded_roots.append(Path(extract_root).expanduser().resolve())
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d.casefold() not in EXCLUDED_NAMES and not _is_link(Path(base)/d)]
        for name in names:
            if name.startswith('~$'):
                continue
            path = Path(base)/name
            if _is_link(path) or any(str(path.resolve()).casefold().startswith(str(x).casefold()+os.sep) for x in excluded_roots): continue
            if path.suffix.casefold() in CAD_EXTENSIONS: files.append(path)
            elif path.suffix.casefold() == ".zip": zips.append(path)
    records=[]; by_hash={}
    for path in sorted(files):
        digest=_sha256(path); rec={"path":str(path),"relative_path":str(path.relative_to(root)),"basename":path.name,"size_bytes":path.stat().st_size,"sha256":digest,"provenance":[str(path)]}
        by_hash.setdefault(digest,[]).append(rec); records.append(rec)
    for recs in by_hash.values():
        if len(recs)>1:
            prov=[r["path"] for r in recs]
            for r in recs: r["dedup_hash_group"]=len(recs); r["provenance"]=prov
    zip_records=[]; validated=[]
    for zp in sorted(zips):
        with zipfile.ZipFile(zp) as z:
            members=_safe_zip_members(z,max_member_size=max_zip_member_size,max_total_size=max_zip_total_size)
            validated.append((zp,members))
    archive_records=[]
    for zp, members in validated:
        with zipfile.ZipFile(zp) as z:
            for item in members:
                if Path(item["name"]).suffix.casefold() in CAD_EXTENSIONS and not Path(item['name']).name.startswith('~$'):
                    with z.open(item["name"]) as stream:
                        h=hashlib.sha256()
                        for chunk in iter(lambda: stream.read(1024*1024), b""): h.update(chunk)
                    rec={"path":f"{zp}!{item['name']}","relative_path":item["name"],"basename":Path(item["name"]).name,"size_bytes":item["size_bytes"],"sha256":h.hexdigest(),"provenance":[str(zp)+"!"+item["name"]],"archive":str(zp)}
                    archive_records.append(rec); records.append(rec); by_hash.setdefault(rec["sha256"],[]).append(rec)
            zr={"path":str(zp),"relative_path":str(zp.relative_to(root)),"members":members,"extracted":False}
            if extract_root:
                stage=Path(extract_root).expanduser().resolve()/(zp.stem+'_'+_sha256(zp)[:10])
                if stage.exists(): raise FileExistsError(stage)
                stage.mkdir(parents=True)
                for item in members:
                    target=stage/Path(item["name"])
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(item["name"]) as src, target.open("xb") as dst: shutil.copyfileobj(src,dst)
                zr["extract_root"]=str(stage); zr["extracted"]=True
                for rec in archive_records:
                    if rec.get('archive') == str(zp):
                        rec['extracted_path'] = str(stage / rec['relative_path'])
            zip_records.append(zr)
    basename_groups={}
    for recs in by_hash.values():
        if len(recs)>1:
            prov=[r["path"] for r in recs]
            for r in recs: r["dedup_hash_group"]=len(recs); r["provenance"]=prov
    for rec in records: basename_groups.setdefault(rec["basename"].casefold(),set()).add(rec["sha256"])
    for rec in records:
        rec['configuration_inventory'] = {'status': 'not_inspected', 'reason': 'filesystem_inventory_does_not_infer_native_configurations'}
        if len(basename_groups[rec["basename"].casefold()])>1: rec["basename_variant"]=True
    result={"root":str(root),"files":records,"file_count":len(records),"distinct_files":len(by_hash),"zip_archives":zip_records,"zip_count":len(zip_records),"dedup_groups":sum(1 for x in by_hash.values() if len(x)>1),"basename_variants":sum(1 for x in basename_groups.values() if len(x)>1),"review_required":True}
    if output: Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result

def main():
    p=argparse.ArgumentParser(); p.add_argument("--root",required=True); p.add_argument("--output",required=True); p.add_argument("--extract-root"); args=p.parse_args(); inventory_project(args.root,args.output,args.extract_root)
if __name__ == "__main__": main()
