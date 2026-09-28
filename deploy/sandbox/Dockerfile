# pi-py 沙箱执行环境（企业场景 A+B 精简版）
# 面向：解压/压缩、多文件批量、csv/excel/pdf/docx/pptx、代码编写、网络抓取、图片处理
# 原则：标准库已覆盖 zip/tar/gz/bz2/xz/csv/json/hash —— 只装标准库补不了的高频格式。
#
# 构建 → 推本地 registry → CubeMaster 建模板（见 docs/production-deployment.md §5）：
#   docker build -t pi-sandbox:1.0 -f deploy/sandbox/Dockerfile .
#   docker tag  pi-sandbox:1.0 127.0.0.1:5000/pi-sandbox:1.0
#   docker push 127.0.0.1:5000/pi-sandbox:1.0
#   cubemastercli template build --image 127.0.0.1:5000/pi-sandbox:1.0 ...
#
# 磁盘预算：A+B 约 250MB，装后 Alpine 根 ~740MB 可用，workspace 留足。

FROM python:3.12-alpine3.20

# ---- A 档 · 系统工具（企业高频、标准库无）-------------------------------
# poppler-utils : pdftotext/pdfinfo —— PDF 转文本（OCR 外最快提取）
# 7zip          : 7z/7zz          —— .7z 解压，且 7-Zip 23.x 原生支持解压
#                  .rar（RAR/RAR5），一个包同时覆盖 7z+rar（Alpine 无 unrar 包）
# git           : 代码仓库/版本管理
RUN apk add --no-cache \
    poppler-utils \
    7zip \
    git

# ---- A 档 · Python 库（musllinux wheel，免编译链，省 ~200MB）------------
# pandas       : 企业数据分析核心（含 numpy）
# openpyxl     : xlsx 读写（标准库读不了 Excel）
# python-docx  : docx 读写
# pypdf        : PDF 文本/合并/裁剪（纯 Python，与 poppler 互补）
RUN pip install --no-cache-dir \
    pandas \
    openpyxl \
    python-docx \
    pypdf

# ---- B 档 · Python 库（补边界）-----------------------------------------
# requests       : HTTP 客户端（agent 最顺手的网络库）
# pillow         : 图片缩略/裁剪/格式转换
# beautifulsoup4 : 网页解析（情报/爬虫）
# lxml           : html/xml 快速解析（bs4 的高性能后端）
# python-pptx    : PPT 读写（企业汇报）
RUN pip install --no-cache-dir \
    requests \
    pillow \
    beautifulsoup4 \
    lxml \
    python-pptx

# ---- 能力自检：构建即验证，任何工具缺失直接 fail -----------------------
COPY deploy/sandbox/verify.sh /usr/local/bin/pi-verify
RUN chmod +x /usr/local/bin/pi-verify && /usr/local/bin/pi-verify

CMD ["python3"]