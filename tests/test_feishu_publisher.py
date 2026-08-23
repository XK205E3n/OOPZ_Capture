import asyncio
from pathlib import Path

from oopz_capture.feishu_publisher import DocumentBlock, FeishuPublisher, PublicationConfig, _blocks


class FakePublishingClient:
    def __init__(self):
        self.calls = []

    async def create_document(self, **kwargs):
        self.calls.append(("create_document", kwargs)); return "docx_public"

    async def append_text_blocks(self, **kwargs):
        self.calls.append(("append_text_blocks", kwargs))

    async def replace_text_blocks(self, **kwargs):
        self.calls.append(("replace_text_blocks", kwargs))

    async def set_anyone_with_link_readable(self, **kwargs):
        self.calls.append(("set_anyone_with_link_readable", kwargs))

    async def create_base_record(self, **kwargs):
        self.calls.append(("create_base_record", kwargs)); return "rec_public"

    async def set_document_private(self, **kwargs):
        self.calls.append(("set_document_private", kwargs))

    async def update_base_record(self, **kwargs):
        self.calls.append(("update_base_record", kwargs))

    async def delete_document(self, **kwargs):
        self.calls.append(("delete_document", kwargs))

    async def delete_base_record(self, **kwargs):
        self.calls.append(("delete_base_record", kwargs))


def test_publish_creates_an_individual_document_and_updates_stable_index(tmp_path: Path) -> None:
    async def run():
        session = tmp_path / "2026-08-22_10-51-32_BJT" / "analysis_variants" / "configured-api"
        session.mkdir(parents=True)
        (session / "summary.public.md").write_text("# 标题\n\n公开摘要", encoding="utf-8")
        client = FakePublishingClient()
        publisher = FeishuPublisher(PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"), client, output_root=tmp_path)
        result = await publisher.publish(session_id="2026-08-22_10-51-32_BJT", approved_by_open_id="ou_admin", approved_by_name="E3n")
        assert result["public_index_url"] == "https://feishu.cn/base/index"
        assert result["document_url"] == "https://feishu.cn/docx/docx_public"
        assert [kind for kind, _ in client.calls] == ["create_document", "append_text_blocks", "set_anyone_with_link_readable", "create_base_record"]
        fields = client.calls[-1][1]["fields"]
        assert client.calls[0][1]["title"] == "2026年8月22日10:51录音总结"
        assert fields["报告标题"] == "2026年8月22日10:51录音总结"
        assert fields["状态"] == "已发布"
        assert fields["阅读链接"]["link"] == result["document_url"]
        assert fields["审批人"] == "E3n"
    asyncio.run(run())


def test_markdown_is_uploaded_as_native_heading_text_and_list_blocks() -> None:
    assert _blocks("# 报告\n\n## 总结\n\n正文\n\n### 话题\n- 第一项\n* 第二项") == [
        DocumentBlock("heading1", "报告"),
        DocumentBlock("heading2", "总结"),
        DocumentBlock("text", "正文"),
        DocumentBlock("heading3", "话题"),
        DocumentBlock("bullet", "第一项"),
        DocumentBlock("bullet", "第二项"),
    ]


def test_publish_passes_structured_blocks_to_feishu_client(tmp_path: Path) -> None:
    async def run():
        session = tmp_path / "2026-08-22_10-51-32_BJT" / "analysis_variants" / "configured-api"
        session.mkdir(parents=True)
        (session / "summary.public.md").write_text("## 总结\n\n正文\n\n- 条目", encoding="utf-8")
        client = FakePublishingClient()
        publisher = FeishuPublisher(
            PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"),
            client,
            output_root=tmp_path,
        )

        await publisher.publish(session_id="2026-08-22_10-51-32_BJT", approved_by_open_id="ou_admin")

        append_call = next(kwargs for kind, kwargs in client.calls if kind == "append_text_blocks")
        assert append_call["blocks"] == [
            DocumentBlock("heading2", "总结"),
            DocumentBlock("text", "正文"),
            DocumentBlock("bullet", "条目"),
        ]
    asyncio.run(run())


def test_refresh_document_replaces_body_without_changing_document_identity(tmp_path: Path) -> None:
    async def run():
        session = tmp_path / "2026-08-22_10-51-32_BJT" / "analysis_variants" / "configured-api"
        session.mkdir(parents=True)
        report = session / "summary.public.md"
        report.write_text("## 总结\n\n新版正文", encoding="utf-8")
        client = FakePublishingClient()
        publisher = FeishuPublisher(
            PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"),
            client,
            output_root=tmp_path,
        )

        fingerprint = await publisher.refresh_document(
            session_id="2026-08-22_10-51-32_BJT",
            document_id="docx_existing",
        )

        assert fingerprint == __import__("hashlib").sha256(report.read_bytes()).hexdigest()
        assert client.calls == [("replace_text_blocks", {
            "document_id": "docx_existing",
            "blocks": [DocumentBlock("heading2", "总结"), DocumentBlock("text", "新版正文")],
            "known_old_count": None,
        })]
    asyncio.run(run())


def test_publish_keeps_open_id_label_when_a_display_name_is_unavailable(tmp_path: Path) -> None:
    async def run():
        session = tmp_path / "2026-08-22_10-51-32_BJT" / "analysis_variants" / "configured-api"
        session.mkdir(parents=True)
        (session / "summary.public.md").write_text("公开摘要", encoding="utf-8")
        client = FakePublishingClient()
        publisher = FeishuPublisher(PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"), client, output_root=tmp_path)
        await publisher.publish(session_id="2026-08-22_10-51-32_BJT", approved_by_open_id="ou_admin")
        assert client.calls[-1][1]["fields"]["审批人"] == "飞书 ID：ou_admin"
    asyncio.run(run())


def test_publish_uses_the_tenant_host_of_the_stable_index(tmp_path: Path) -> None:
    async def run():
        session = tmp_path / "2026-08-22_10-51-32_BJT" / "analysis_variants" / "configured-api"
        session.mkdir(parents=True)
        (session / "summary.public.md").write_text("公开摘要", encoding="utf-8")
        client = FakePublishingClient()
        publisher = FeishuPublisher(
            PublicationConfig("folder", "base", "table", "https://scnz911uzgrz.feishu.cn/base/index"),
            client,
            output_root=tmp_path,
        )
        result = await publisher.publish(session_id="2026-08-22_10-51-32_BJT", approved_by_open_id="ou_admin")
        assert result["document_url"] == "https://scnz911uzgrz.feishu.cn/docx/docx_public"
    asyncio.run(run())


def test_revoke_removes_public_access_and_hides_calendar_record(tmp_path: Path) -> None:
    async def run():
        client = FakePublishingClient()
        publisher = FeishuPublisher(PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"), client, output_root=tmp_path)
        await publisher.revoke({"document_id": "docx_public", "base_record_id": "rec_public"})
        assert [kind for kind, _ in client.calls] == ["set_document_private", "update_base_record"]
        assert client.calls[-1][1]["fields"] == {"状态": "已撤回"}
    asyncio.run(run())


def test_delete_removes_the_public_document_and_base_record(tmp_path: Path) -> None:
    async def run():
        client = FakePublishingClient()
        publisher = FeishuPublisher(PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index"), client, output_root=tmp_path)
        await publisher.delete({"document_id": "docx_public", "base_record_id": "rec_public"})
        assert [kind for kind, _ in client.calls] == ["delete_document", "delete_base_record"]
    asyncio.run(run())
