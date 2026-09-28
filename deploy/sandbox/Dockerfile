# pi-py 沙箱执行环境（企业场景 A+B）
# base 完全对齐 cube-lite-py:1.0（alpine:3.20 + apk python3，非官方 python 镜像：
# CubeSandbox guest 对 base 敏感，实测 python:3.12-alpine 会 "reset guest time
# BrokenPipe"）。在其上加 A+B 能力 + envd/entrypoint 沙箱骨架。
#
# CubeSandbox 沙箱镜像契约（缺一不可，否则 CubeMaster 创建容器失败）：
#   - base 用 alpine:3.20（与已在跑的 cube-lite-py 系一致）
#   - /usr/bin/envd + /usr/local/bin/cube-entrypoint.sh（ENTRYPOINT 拉起 envd 常驻）
#   - GNU 全集工具（busybox 阉割版撑不起模型生成的 grep -P / find -exec / sed -i）
#
# 构建 → 推本地 registry → CubeMaster 建模板（见 docs/production-deployment.md §5）：
#   docker build -t pi-sandbox:1.0 -f deploy/sandbox/Dockerfile .
#   docker tag  pi-sandbox:1.0 127.0.0.1:5000/pi-sandbox:1.0
#   docker push 127.0.0.1:5000/pi-sandbox:1.0
#   cubemastercli template create-from-image --image 127.0.0.1:5000/pi-sandbox:1.0 \
#       --alias pi-sandbox-ab --cpu 1000 --memory 256 --writable-layer-size 1Gi

FROM alpine:3.20

# 构建源 mirror：默认腾讯云内网（作者环境）。换云厂商时用 --build-arg 覆盖，
# 例如火山云/阿里云：--build-arg APK_MIRROR_HOST=dl-cdn.alpinelinux.org \
#                    --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#                    --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ARG APK_MIRROR_HOST=mirrors.tencentyun.com
ARG PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.tencentyun.com

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    ENVD_PORT=49983

# ---- 系统层：python + GNU 完整工具 + 企业高频命令 ----------------------
# python3/py3-pip: 系统包（/usr/bin，需 PEP 668 break-system-packages）
# coreutils/findutils/grep/sed/gawk: busybox 阉割版撑不起模型命令（cube-lite-py 实测踩过）
# poppler-utils: pdftotext/pdfinfo（PDF 提取）  7zip: 7z + rar 解压
# git/tar/curl/jq/file: 仓库/解压/网络/JSON/类型判断
# 腾讯云内网 mirror（本环境加速；换环境改回官方源）
RUN sed -i "s#dl-cdn.alpinelinux.org#${APK_MIRROR_HOST}#g" /etc/apk/repositories \
    && apk add --no-cache \
        ca-certificates bash \
        coreutils findutils grep sed gawk \
        python3 py3-pip \
        poppler-utils 7zip \
        git tar curl jq file \
    && ln -sf /bin/busybox /usr/bin/nice \
    && adduser -D -u 1000 user \
    && mkdir -p /var/log \
    && printf '[global]\nbreak-system-packages = true\n' > /etc/pip.conf \
    && python3 --version

# ---- A 档 Python 库（musllinux wheel，免编译链）-------------------------
# pandas + numpy : 企业数据分析核心
# openpyxl       : xlsx 读写；python-docx : docx；pypdf : PDF 提取/合并
RUN python3 -m pip install --no-cache-dir \
    -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
    pandas \
    openpyxl \
    python-docx \
    pypdf

# ---- B 档 Python 库（补边界）-------------------------------------------
# requests / pillow / beautifulsoup4+lxml / python-pptx
RUN python3 -m pip install --no-cache-dir \
    -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}" \
    requests \
    pillow \
    beautifulsoup4 \
    lxml \
    python-pptx

# ---- CubeSandbox 沙箱骨架（envd + 入口脚本，构建物料见 deploy/sandbox/）---
COPY deploy/sandbox/envd /usr/bin/envd
COPY deploy/sandbox/cube-entrypoint.sh /usr/local/bin/cube-entrypoint.sh
RUN chmod +x /usr/bin/envd /usr/local/bin/cube-entrypoint.sh \
    && /usr/bin/envd -version 2>&1 | head -2 || true

# ---- 能力自检：构建即验证，任何工具缺失直接 fail -----------------------
COPY deploy/sandbox/verify.sh /usr/local/bin/pi-verify
RUN chmod +x /usr/local/bin/pi-verify && /usr/local/bin/pi-verify

EXPOSE 49983
ENTRYPOINT ["/usr/local/bin/cube-entrypoint.sh"]
CMD []