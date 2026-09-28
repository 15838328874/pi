#!/bin/sh
# pi-py 沙箱能力自检：构建时跑一遍，任何工具缺失即 exit 1（镜像构建失败）。
# 也保留在镜像 /usr/local/bin/pi-verify，随时可在沙箱内复核。
set -e
echo "== pi-py 沙箱能力自检 =="

# ---- 1. 系统命令 ----
for cmd in python3 pip3 git pdftotext pdfinfo 7z; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "  [OK] cmd $cmd"
  else
    echo "  [MISS] cmd $cmd"; exit 1
  fi
done

# ---- 2. Python 库（A+B 全量）----
python3 - <<'PY'
import importlib, sys
mods = [
    # A 档
    "pandas", "numpy", "openpyxl", "docx", "pypdf",
    # B 档
    "requests", "PIL", "bs4", "lxml", "pptx",
]
ok = True
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  [OK] py {m}")
    except Exception as e:  # noqa: BLE001
        print(f"  [MISS] py {m}: {e}")
        ok = False
if not ok:
    sys.exit(1)
PY

# ---- 3. 标准库压缩/解压实战（zip + tar.gz 写读往返）----
python3 - <<'PY'
import io, tarfile, zipfile
# zip 写→读
with zipfile.ZipFile("/tmp/pi-t.zip", "w") as z:
    z.writestr("a.txt", b"hello-pi")
with zipfile.ZipFile("/tmp/pi-t.zip") as z:
    assert z.read("a.txt") == b"hello-pi", "zip 往返失败"
# tar.gz 写→读
data = b"world-pi"
with tarfile.open("/tmp/pi-t.tar.gz", "w:gz") as t:
    ti = tarfile.TarInfo("b.txt"); ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))
with tarfile.open("/tmp/pi-t.tar.gz") as t:
    assert t.extractfile("b.txt").read() == data, "tar.gz 往返失败"
print("  [OK] stdlib zip/tar.gz 写读往返")
PY

# ---- 4. PDF 文本提取实战（poppler 命令行 + pypdf 库双通道）----
python3 - <<'PY'
import pypdf, io
w = pypdf.PdfWriter()
p = w.add_blank_page(width=200, height=200)
buf = io.BytesIO(); w.write(buf); buf.seek(0)
r = pypdf.PdfReader(buf)
assert len(r.pages) == 1, "pypdf 往返失败"
print("  [OK] pypdf 空白页写读往返（pdftotext 命令已在上方校验在 PATH）")
PY

echo "== 自检全部通过 =="