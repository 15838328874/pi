"""Heavy-parser backends: OCR for scans / images via an EXTERNAL HTTP service.

This is the v1.5 gap, landed for the image/scan path. The kernel depends only on
the ``HeavyParser.parse(path) -> str`` protocol (file → Markdown), so the vendor
is swappable: today PaddleOCR's online API, tomorrow a self-hosted MinerU service
— change ``PI_RAG_HEAVY_PARSER`` and the URL/token/model, no kernel change.

Blocking by design: OCR jobs are second-to-minute scale and polled, so ``parse``
blocks. Callers (ingest) must run it via ``asyncio.to_thread`` to keep the event
loop free — same house rule as the PDF parser.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from pi.rag.protocols import HeavyParser

log = logging.getLogger("pi.rag.heavy")

PADDLEOCR_DEFAULT_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
PADDLEOCR_DEFAULT_MODEL = "PaddleOCR-VL-1.6"

_POLL_INTERVAL_S = 5.0
_MAX_POLL_ATTEMPTS = 240  # 20 分钟上限；图片/扫描件极少超这个


class HeavyParserError(Exception):
    """重解析服务调用失败（提交/轮询/下载任一环节）。"""


class PaddleOcrHeavyParser:
    """PaddleOCR 线上 API（aistudio-app）。

    协议三步：multipart 提交 → 轮询 GET /jobs/{id} 到 done → 下载
    ``resultUrl.jsonUrl`` 的 JSONL，把每页 ``layoutParsingResults`` 的
    ``markdown.text`` 拼接成一个 Markdown 文档。
    """

    def __init__(self, url: str, token: str, model: str, *, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.model = model
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def parse(self, path: Path) -> str:
        job_id = self._submit(path)
        json_url = self._await(job_id)
        return self._download(json_url)

    # -- steps --------------------------------------------------------------

    def _submit(self, path: Path) -> str:
        data = {
            "model": self.model,
            "optionalPayload": json.dumps(
                {
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "useChartRecognition": False,
                }
            ),
        }
        try:
            with path.open("rb") as fh:
                resp = self._client.post(
                    self.url,
                    headers={"Authorization": f"bearer {self.token}"},
                    data=data,
                    files={"file": (path.name, fh, "application/octet-stream")},
                )
        except httpx.HTTPError as exc:
            raise HeavyParserError(f"OCR submit failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise HeavyParserError(f"OCR submit HTTP {resp.status_code}: {resp.text[:200]}")
        job_id = (resp.json().get("data") or {}).get("jobId")
        if not job_id:
            raise HeavyParserError(f"OCR submit returned no jobId: {resp.text[:200]}")
        return job_id

    def _await(self, job_id: str) -> str:
        for attempt in range(_MAX_POLL_ATTEMPTS):
            try:
                resp = self._client.get(
                    f"{self.url}/{job_id}",
                    headers={"Authorization": f"bearer {self.token}"},
                )
            except httpx.HTTPError as exc:
                raise HeavyParserError(f"OCR poll failed: {type(exc).__name__}: {exc}") from exc
            if resp.status_code != 200:
                raise HeavyParserError(f"OCR poll HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json().get("data") or {}
            state = data.get("state")
            if state == "done":
                url = (data.get("resultUrl") or {}).get("jsonUrl")
                if not url:
                    raise HeavyParserError("OCR job done but no jsonUrl in result")
                return url
            if state == "failed":
                raise HeavyParserError(f"OCR job failed: {data.get('errorMsg', 'unknown')}")
            log.debug("OCR job %s: %s (attempt %d)", job_id, state, attempt + 1)
            time.sleep(_POLL_INTERVAL_S)
        raise HeavyParserError(f"OCR job {job_id} timed out after {_MAX_POLL_ATTEMPTS} polls")

    def _download(self, json_url: str) -> str:
        # 结果 URL 是预签名的对象存储地址，不能带 Authorization 头，也不走本
        # 客户端（可能跨域/跨签）。独立 GET。
        try:
            resp = httpx.get(json_url, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HeavyParserError(f"OCR result download failed: {type(exc).__name__}: {exc}") from exc

        parts: list[str] = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results = json.loads(line).get("result", {}).get("layoutParsingResults", [])
            except (ValueError, AttributeError):
                continue
            for res in results:
                md = (res.get("markdown") or {}).get("text", "")
                if md.strip():
                    parts.append(md.strip())
        if not parts:
            raise HeavyParserError("OCR succeeded but produced no markdown text")
        return "\n\n".join(parts)


def build_heavy_parser(config: "RagConfig") -> HeavyParser | None:  # noqa: F821 - circular-ish, resolved at runtime
    """按配置构建重解析服务；未配置/配置不完整时返回 None（维持 v1 行为：标记跳过）。"""
    kind = config.heavy_parser.strip().lower()
    if not kind:
        return None
    if kind == "paddleocr":
        token = config.heavy_parser_token.strip()
        if not token:
            log.warning("PI_RAG_HEAVY_PARSER=paddleocr but PI_RAG_HEAVY_PARSER_TOKEN unset; "
                        "heavy parsing disabled (images/scans stay needs_heavy_parser)")
            return None
        url = config.heavy_parser_url.strip() or PADDLEOCR_DEFAULT_URL
        model = config.heavy_parser_model.strip() or PADDLEOCR_DEFAULT_MODEL
        log.info("rag heavy parser: paddleocr (model=%s)", model)
        return PaddleOcrHeavyParser(url, token, model)
    log.warning("unknown PI_RAG_HEAVY_PARSER=%r; heavy parsing disabled", kind)
    return None
