import subprocess
from pathlib import Path

from test_analysis_pipeline import RecordingClient, make_session
from test_deepseek_client import config, success
from oopz_capture.analysis_pipeline import run_analysis, FINAL_REQUIRED, SHORT_REQUIRED
from oopz_capture.deepseek_client import DeepSeekClient
from oopz_capture.reports import overall_summary_text


def test_legacy_text_and_public_report_only_deliver_overall():
    text = "## 分析缺失时段\n\n90-95 minutes\n\n### 整体性总结\n\nKeep this.\n\n## 按时间顺序的进展\n\nDo not send.\n\n## 每60分钟长期摘要\n\nDo not send hours."
    value = overall_summary_text(text)
    assert 'Keep this.' in value and '90-95 minutes' in value
    assert 'Do not send' not in value


def test_pdf_error_progress_for_fresh_and_cached_analysis(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('browser missing')
    monkeypatch.setattr('oopz_capture.analysis_pipeline.render_session_reports', fail)
    handoff = make_session(tmp_path)
    client = RecordingClient()
    for _ in range(2):
        events = []
        run_analysis(handoff, client, render_pdf=True, progress_reporter=events.append)
        errors = [e for e in events if e['stage'] == 'pdf_failed']
        assert len(errors) == 1 and 'browser missing' in errors[0]['message']


def test_enabled_thinking_all_stages_uses_provider_default_effort(tmp_path):
    observed = []
    def transport(endpoint, headers, payload, timeout):
        observed.append(payload)
        fields = {**SHORT_REQUIRED, **FINAL_REQUIRED}
        return success({k: 'test summary' if v is str else [] for k, v in fields.items()})
    client = DeepSeekClient(config(provider='openai-compatible',model='qwen3.8-flash',
        base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
        thinking_mode='enabled', thinking_max_tokens=4096),transport=transport)
    output = run_analysis(make_session(tmp_path, short_window_count=1),client)
    assert len(observed) == 3
    assert all(p['enable_thinking'] is True and 'reasoning_effort' not in p for p in observed)
    assert all(p['max_tokens'] == 4096 for p in observed)
    assert all(p['thinking'] == 'enabled' for p in output['result']['analysis_policy'].values())


def test_browser_selection_uses_existing_edge_and_validates_override():
    root = Path(__file__).resolve().parents[1]
    program = r'''import {findChrome} from './tools/md_to_pdf.mjs';
import assert from 'node:assert/strict';
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
assert.equal(findChrome({}, p => p === edge), edge);
assert.throws(() => findChrome({}, () => false), /PDF browser missing/);
assert.throws(() => findChrome({MD_TO_PDF_CHROME_PATH:'missing'}, () => false), /MD_TO_PDF_CHROME_PATH/);
assert.equal(findChrome({MD_TO_PDF_CHROME_PATH:'custom'}, p => p === 'custom'), 'custom');
'''
    result = subprocess.run([str(root/'tools/node/node.exe'),'--input-type=module','-e',program],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr


def test_pdf_subprocess_error_keeps_diagnostic_reason(tmp_path, monkeypatch):
    import pytest
    from oopz_capture import pdf_reports
    source = tmp_path / 'report.md'
    source.write_text('test', encoding='utf-8')
    executable = tmp_path / 'node.exe'
    executable.write_text('fixture', encoding='utf-8')
    renderer = tmp_path / 'render.mjs'
    renderer.write_text('fixture', encoding='utf-8')
    monkeypatch.setattr(pdf_reports, 'NODE_PATH', executable)
    monkeypatch.setattr(pdf_reports, 'RENDERER', renderer)
    monkeypatch.setattr(pdf_reports, 'NODE_MODULES', tmp_path)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['node'], stderr='Browser executable missing')
    monkeypatch.setattr(pdf_reports.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='Browser executable missing'):
        pdf_reports.render_markdown_pdf(source, tmp_path / 'report.pdf')


def test_invalid_browser_cli_exits_without_http_server_hang(tmp_path):
    import os
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'input.md'
    source.write_text('# test',encoding='utf-8')
    env = os.environ.copy()
    env['MD_TO_PDF_CHROME_PATH'] = str(tmp_path / 'missing.exe')
    result = subprocess.run([str(root/'tools/node/node.exe'),str(root/'tools/md_to_pdf.mjs'),
        str(source),str(tmp_path/'out.pdf')],env=env,capture_output=True,text=True,timeout=8)
    assert result.returncode == 1
    assert 'MD_TO_PDF_CHROME_PATH' in result.stderr
    assert not (tmp_path/'out.pdf').exists()
