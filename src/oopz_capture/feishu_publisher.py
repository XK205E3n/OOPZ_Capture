"""M3 publication: approved public report -> public Feishu doc -> Base index."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class PublicationConfig:
    folder_token: str
    base_app_token: str
    base_table_id: str
    public_index_url: str
    # Usually left unset.  A tenant-specific URL derived from the stable Base
    # URL is required for a recipient outside the app tenant to open a report.
    document_url_prefix: str = ""

    def validate(self) -> None:
        if not all((self.folder_token, self.base_app_token, self.base_table_id, self.public_index_url)):
            raise ValueError("Feishu M3 requires public document folder, Base app/table tokens, and the stable public index URL")
        if not self.public_index_url.startswith("https://"):
            raise ValueError("OOPZ_FEISHU_PUBLIC_INDEX_URL must be an https URL")


class PublishingClient(Protocol):
    async def create_document(self, *, title: str, folder_token: str) -> str: ...
    async def append_text_blocks(self, *, document_id: str, blocks: list["DocumentBlock"]) -> None: ...
    async def replace_text_blocks(
        self, *, document_id: str, blocks: list["DocumentBlock"], known_old_count: int | None = None,
    ) -> None: ...
    async def set_anyone_with_link_readable(self, *, document_id: str) -> None: ...
    async def create_base_record(self, *, app_token: str, table_id: str, fields: dict[str, Any]) -> str: ...
    async def set_document_private(self, *, document_id: str) -> None: ...
    async def update_base_record(self, *, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None: ...
    async def delete_document(self, *, document_id: str) -> None: ...
    async def delete_base_record(self, *, app_token: str, table_id: str, record_id: str) -> None: ...


def recording_title(session_id: str) -> tuple[str, datetime]:
    """Return the human-facing title and recording time encoded by Session."""
    try:
        recorded_at = datetime.strptime(session_id[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError as error:
        raise ValueError("Session ID 缺少可识别的开始时间") from error
    return (
        f"{recorded_at.year}年{recorded_at.month}月{recorded_at.day}日"
        f"{recorded_at:%H:%M}录音总结",
        recorded_at,
    )


def approver_label(open_id: str, display_name: str | None = None) -> str:
    """Prefer a resolved Feishu display name, retaining an ID-only fallback."""
    name = str(display_name or "").strip()
    if name:
        return name
    value = str(open_id or "").strip()
    return f"飞书 ID：{value}" if value else "飞书 ID：未知"


def _public_report(session_dir: Path) -> Path:
    variants = session_dir / "analysis_variants"
    candidates = [path for path in variants.glob("*/summary.public.md") if path.is_file()]
    if not candidates:
        raise FileNotFoundError("approved session has no summary.public.md")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def public_report_fingerprint(*, output_root: Path, session_id: str) -> str:
    session_dir = (output_root.resolve() / session_id).resolve()
    if session_dir.parent != output_root.resolve():
        raise ValueError("unsafe Session directory")
    return sha256(_public_report(session_dir).read_bytes()).hexdigest()


@dataclass(frozen=True)
class DocumentBlock:
    kind: str
    text: str


def _blocks(markdown: str) -> list[DocumentBlock]:
    """Map the report's small Markdown subset to native Feishu document blocks."""
    output: list[DocumentBlock] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        level = len(line) - len(line.lstrip("#"))
        if level and line[level:].startswith(" "):
            output.append(DocumentBlock(f"heading{min(level, 9)}", line[level:].strip()))
        elif re.match(r"^[*-]\s+", line):
            output.append(DocumentBlock("bullet", re.sub(r"^[*-]\s+", "", line)))
        else:
            output.append(DocumentBlock("text", line))
    return output or [DocumentBlock("text", "本报告没有可公开的文本内容。")]


class FeishuPublisher:
    def __init__(self, config: PublicationConfig, client: PublishingClient, *, output_root: Path):
        config.validate()
        self.config, self.client, self.output_root = config, client, output_root.resolve()

    def document_url(self, document_id: str) -> str:
        """Build a URL on the same tenant host as the public Base index."""
        prefix = self.config.document_url_prefix.rstrip("/")
        if not prefix:
            parsed = urlparse(self.config.public_index_url)
            prefix = f"{parsed.scheme}://{parsed.netloc}/docx"
        return f"{prefix}/{document_id}"

    async def publish(
        self, *, session_id: str, approved_by_open_id: str,
        approved_by_name: str | None = None, expected_fingerprint: str | None = None,
    ) -> dict[str, str]:
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root or not session_dir.is_dir():
            raise ValueError("unsafe or missing Session directory")
        report = _public_report(session_dir)
        fingerprint = sha256(report.read_bytes()).hexdigest()
        if expected_fingerprint and expected_fingerprint != fingerprint:
            raise ValueError("候选公开报告在审查后发生变化；请重新审查后再发布")
        title, recorded_at = recording_title(session_id)
        blocks = _blocks(report.read_text(encoding="utf-8"))
        document_id = await self.client.create_document(title=title, folder_token=self.config.folder_token)
        await self.client.append_text_blocks(document_id=document_id, blocks=blocks)
        await self.client.set_anyone_with_link_readable(document_id=document_id)
        url = self.document_url(document_id)
        record_id = await self.client.create_base_record(app_token=self.config.base_app_token, table_id=self.config.base_table_id, fields={
            "报告标题": title,
            "录音日期": int(recorded_at.timestamp() * 1000),
            "发布时间": int(datetime.now().timestamp() * 1000),
            "状态": "已发布",
            "对外摘要": blocks[0].text[:1000],
            "阅读链接": {"link": url, "text": "阅读报告"},
            "审批人": approver_label(approved_by_open_id, approved_by_name),
        })
        return {"document_id": document_id, "document_url": url, "base_record_id": record_id, "public_index_url": self.config.public_index_url, "public_report_sha256": fingerprint}

    async def refresh_document(
        self, *, session_id: str, document_id: str, known_old_count: int | None = None,
    ) -> str:
        """Replace one published document's body while preserving its URL and index record."""
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root or not session_dir.is_dir():
            raise ValueError("unsafe or missing Session directory")
        report = _public_report(session_dir)
        await self.client.replace_text_blocks(
            document_id=document_id,
            blocks=_blocks(report.read_text(encoding="utf-8")),
            known_old_count=known_old_count,
        )
        return sha256(report.read_bytes()).hexdigest()

    async def revoke(self, publication: dict[str, Any]) -> None:
        await self.client.set_document_private(document_id=str(publication["document_id"]))
        await self.client.update_base_record(app_token=self.config.base_app_token, table_id=self.config.base_table_id, record_id=str(publication["base_record_id"]), fields={"状态": "已撤回"})

    async def delete(self, publication: dict[str, Any]) -> None:
        """Permanently remove the app-created public document and Base entry."""
        await self.delete_document(publication)
        await self.delete_index_record(publication)

    async def delete_document(self, publication: dict[str, Any]) -> None:
        await self.client.delete_document(document_id=str(publication["document_id"]))

    async def delete_index_record(self, publication: dict[str, Any]) -> None:
        await self.client.delete_base_record(
            app_token=self.config.base_app_token,
            table_id=self.config.base_table_id,
            record_id=str(publication["base_record_id"]),
        )

    async def repair_index_record(self, publication: dict[str, Any], *, approved_by_name: str | None = None) -> None:
        """Repair a pre-tenant-link record without republishing its contents."""
        session_id = str(publication.get("session_id") or "")
        title, _ = recording_title(session_id)
        await self.client.update_base_record(
            app_token=self.config.base_app_token,
            table_id=self.config.base_table_id,
            record_id=str(publication["base_record_id"]),
            fields={
                "报告标题": title,
                "阅读链接": {"link": self.document_url(str(publication["document_id"])), "text": "打开公开报告"},
                "审批人": approver_label(
                    str(publication.get("approved_by_open_id") or ""),
                    approved_by_name or str(publication.get("approved_by_name") or ""),
                ),
            },
        )


class LarkPublishingClient:
    """Thin official-SDK wrapper; isolated so tests need no Feishu credentials."""
    def __init__(self, app_id: str, app_secret: str):
        import lark_oapi as lark
        self.lark = lark
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    @staticmethod
    def _ok(response: Any) -> Any:
        if not response.success():
            raise RuntimeError(f"Feishu API failed: code={response.code}, msg={response.msg}")
        return response.data

    async def create_document(self, *, title: str, folder_token: str) -> str:
        from lark_oapi.api.docx.v1 import CreateDocumentRequest, CreateDocumentRequestBody
        request = CreateDocumentRequest.builder().request_body(CreateDocumentRequestBody.builder().title(title).folder_token(folder_token).build()).build()
        data = self._ok(await self.client.docx.v1.document.acreate(request))
        return str(data.document.document_id)

    async def append_text_blocks(self, *, document_id: str, blocks: list[DocumentBlock]) -> None:
        from lark_oapi.api.docx.v1 import Block, CreateDocumentBlockChildrenRequest, CreateDocumentBlockChildrenRequestBody, Text, TextElement, TextRun
        block_types = {
            "text": 2,
            **{f"heading{level}": level + 2 for level in range(1, 10)},
            "bullet": 12,
        }
        children = []
        for spec in blocks:
            body = Text.builder().elements([
                TextElement.builder().text_run(TextRun.builder().content(spec.text).build()).build()
            ]).build()
            builder = Block.builder().block_type(block_types[spec.kind])
            children.append(getattr(builder, spec.kind)(body).build())
        for start in range(0, len(children), 50):
            request = CreateDocumentBlockChildrenRequest.builder().document_id(document_id).block_id(document_id).request_body(CreateDocumentBlockChildrenRequestBody.builder().children(children[start:start + 50]).build()).build()
            self._ok(await self.client.docx.v1.document_block_children.acreate(request))

    async def replace_text_blocks(
        self, *, document_id: str, blocks: list[DocumentBlock], known_old_count: int | None = None,
    ) -> None:
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBody,
            GetDocumentBlockChildrenRequest,
        )

        if known_old_count is None:
            get_request = (
                GetDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .page_size(500)
                .build()
            )
            data = self._ok(await self.client.docx.v1.document_block_children.aget(get_request))
            old_count = len(list(getattr(data, "items", None) or []))
        else:
            if known_old_count < 0:
                raise ValueError("known_old_count must not be negative")
            old_count = known_old_count

        # Append first so a transient delete failure leaves duplicate content,
        # rather than an empty public document.
        await self.append_text_blocks(document_id=document_id, blocks=blocks)
        if old_count:
            body = (
                BatchDeleteDocumentBlockChildrenRequestBody.builder()
                .start_index(0)
                .end_index(old_count)
                .build()
            )
            request = (
                BatchDeleteDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .request_body(body)
                .build()
            )
            self._ok(await self.client.docx.v1.document_block_children.abatch_delete(request))

    async def set_anyone_with_link_readable(self, *, document_id: str) -> None:
        from lark_oapi.api.drive.v1 import PatchPermissionPublicRequest, PermissionPublicRequest
        # ``share_entity`` controls collaborator management.  Do not send it
        # here: ``only_me`` is not a valid Drive v1 value and makes the whole
        # request fail validation.  External link access only needs these two
        # fields, while the creator remains the document's collaborator owner.
        body = PermissionPublicRequest.builder().external_access(True).link_share_entity("anyone_readable").build()
        request = PatchPermissionPublicRequest.builder().token(document_id).type("docx").request_body(body).build()
        self._ok(await self.client.drive.v1.permission_public.apatch(request))

    async def set_document_private(self, *, document_id: str) -> None:
        from lark_oapi.api.drive.v1 import PatchPermissionPublicRequest, PermissionPublicRequest
        body = PermissionPublicRequest.builder().external_access(False).link_share_entity("tenant_readable").build()
        request = PatchPermissionPublicRequest.builder().token(document_id).type("docx").request_body(body).build()
        self._ok(await self.client.drive.v1.permission_public.apatch(request))

    async def create_base_record(self, *, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
        from lark_oapi.api.bitable.v1 import AppTableRecord, CreateAppTableRecordRequest
        request = CreateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).request_body(AppTableRecord.builder().fields(fields).build()).build()
        data = self._ok(await self.client.bitable.v1.app_table_record.acreate(request))
        return str(data.record.record_id)

    async def get_chat_member_name(self, *, chat_id: str, open_id: str) -> str | None:
        """Resolve a group member's current display name without storing a directory."""
        from lark_oapi.api.im.v1 import GetChatMembersRequest
        request = GetChatMembersRequest.builder().chat_id(chat_id).member_id_type("open_id").page_size(100).build()
        # The IM member endpoint in lark-oapi currently exposes a synchronous
        # ``get`` method (unlike the async document and Base endpoints).
        data = self._ok(self.client.im.v1.chat_members.get(request))
        for member in data.items or []:
            if str(member.member_id or "") == open_id:
                name = str(member.name or "").strip()
                return name or None
        return None

    async def update_base_record(self, *, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        from lark_oapi.api.bitable.v1 import AppTableRecord, UpdateAppTableRecordRequest
        request = UpdateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).record_id(record_id).request_body(AppTableRecord.builder().fields(fields).build()).build()
        self._ok(await self.client.bitable.v1.app_table_record.aupdate(request))

    async def delete_document(self, *, document_id: str) -> None:
        from lark_oapi.api.drive.v1 import DeleteFileRequest
        request = DeleteFileRequest.builder().file_token(document_id).type("docx").build()
        self._ok(await self.client.drive.v1.file.adelete(request))

    async def delete_base_record(self, *, app_token: str, table_id: str, record_id: str) -> None:
        from lark_oapi.api.bitable.v1 import DeleteAppTableRecordRequest
        request = DeleteAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).record_id(record_id).build()
        self._ok(await self.client.bitable.v1.app_table_record.adelete(request))
