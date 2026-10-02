from __future__ import annotations

import base64
from importlib.metadata import PackageNotFoundError
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from oopz_capture import pdf_reports, weasy_pdf


@pytest.mark.parametrize('value', [
    'https://example.invalid/image.png', 'http://127.0.0.1/private',
    'file:///tmp/private.png', 'ftp://example.invalid/x', '/tmp/private.png',
    '//example.invalid/x', 'javascript:alert(1)',
    'data:image/svg+xml,%3Csvg/%3E', 'data:text/css,body{}',
    'data:font/woff2;base64,AAAA', 'data:image/png;charset=utf-8,abc',
    'data:image/png;base64,!!!', 'data:image/png',
])
def test_pdf_fetcher_rejects_external_and_unsafe_resources(value):
    with pytest.raises(ValueError):
        weasy_pdf.data_only_fetcher(value)


def test_pdf_fetcher_only_decodes_bounded_raster_data(monkeypatch):
    payload = b'\x89PNG\r\n\x1a\nfixture-raster-bytes'
    url = 'data:image/png;base64,' + base64.b64encode(payload).decode()
    assert weasy_pdf.data_only_fetcher(url) == {'string': payload, 'mime_type': 'image/png'}
    assert weasy_pdf.data_only_fetcher('data:image/jpeg,%FF%D8%FF')['string'] == b'\xff\xd8\xff'
    with pytest.raises(ValueError, match='declared type'):
        weasy_pdf.data_only_fetcher('data:image/png;base64,' + base64.b64encode(b'%!PS-Adobe EPSF').decode())
    monkeypatch.setattr(weasy_pdf, 'MAX_RESOURCE_BYTES', 4)
    with pytest.raises(ValueError, match='[Oo]versized'):
        weasy_pdf.data_only_fetcher('data:image/png;base64,' + base64.b64encode(b'12345').decode())


@pytest.mark.parametrize('installed', ['69.0', '68.1', '0.0', '71.0', '70.0rc1'])
def test_pdf_backend_requires_security_fixed_weasyprint(monkeypatch, installed):
    monkeypatch.setattr(weasy_pdf, 'version', lambda _: installed)
    with pytest.raises(weasy_pdf.PDFConfigurationError, match='>=70'):
        weasy_pdf.require_weasyprint()


def test_pdf_backend_reports_missing_dependency(monkeypatch):
    def missing(_):
        raise PackageNotFoundError()
    monkeypatch.setattr(weasy_pdf, 'version', missing)
    with pytest.raises(weasy_pdf.PDFConfigurationError, match='missing'):
        weasy_pdf.require_weasyprint()


def test_html_sanitizer_preserves_text_but_removes_all_embedding_channels():
    pytest.importorskip('tinyhtml5')
    html = ('<html><head><link REL="ATTACHMENT" href="data:image/png,whatever"></head><body>'
            'Before<svg><text>SVG_CANARY</text></svg>After'
            '<a rel="alternate attachment" href="file:///private">Visible label</a>'
            '<a href="https://example.invalid">Ordinary link</a>'
            '<script>ACTIVE_CANARY</script><object>OBJECT_CANARY</object></body></html>')
    result = weasy_pdf.sanitize_report_html(html)
    assert 'BeforeAfter' in result and 'Visible label' in result
    assert 'https://example.invalid' in result
    assert all(value not in result for value in ('SVG_CANARY', 'ACTIVE_CANARY', 'OBJECT_CANARY',
                                                 'file:///private', 'attachment', 'data:image/png'))


def test_pdf_engine_selection_is_explicit(monkeypatch):
    monkeypatch.delenv('OOPZ_PDF_BACKEND', raising=False)
    assert pdf_reports.pdf_backend() == 'chromium'
    monkeypatch.setenv('OOPZ_PDF_BACKEND', 'weasyprint')
    monkeypatch.setattr(pdf_reports.sys, 'platform', 'linux')
    assert pdf_reports.pdf_backend() == 'weasyprint'
    monkeypatch.setattr(pdf_reports.sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='Linux'):
        pdf_reports.pdf_backend()
    monkeypatch.setenv('OOPZ_PDF_BACKEND', 'automatic')
    with pytest.raises(ValueError, match='chromium or weasyprint'):
        pdf_reports.pdf_backend()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux explicit PDF backend')
def test_weasy_engine_failure_never_falls_back_and_removes_partial(tmp_path, monkeypatch):
    monkeypatch.setenv('OOPZ_PDF_BACKEND', 'weasyprint')
    monkeypatch.setattr(pdf_reports, 'find_node', lambda: Path(sys.executable))
    monkeypatch.setattr(pdf_reports, 'validate_node', lambda *args: None)
    monkeypatch.setattr(pdf_reports, 'NODE_MODULES', tmp_path)
    source = tmp_path / 'source.md'
    source.write_text('# report')
    destination = tmp_path / 'out.pdf'
    calls = []
    def fail(command, _env):
        calls.append(command)
        destination.write_bytes(b'partial')
        raise subprocess.CalledProcessError(1, command, stderr='fixture failure')
    monkeypatch.setattr(pdf_reports, '_run_renderer', fail)
    with pytest.raises(RuntimeError, match='fixture failure'):
        pdf_reports.render_markdown_pdf(source, destination)
    assert len(calls) == 1 and calls[0][1:3] == ['-m', 'oopz_capture.weasy_pdf']
    assert not destination.exists()


@pytest.mark.parametrize('failure', [KeyboardInterrupt, subprocess.TimeoutExpired])
def test_pdf_interruption_cleans_partial_output(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('OOPZ_PDF_BACKEND', 'chromium')
    monkeypatch.setattr(pdf_reports, 'find_node', lambda: Path(sys.executable))
    monkeypatch.setattr(pdf_reports, 'validate_node', lambda *args: None)
    monkeypatch.setattr(pdf_reports, 'NODE_MODULES', tmp_path)
    source, output = tmp_path / 'source.md', tmp_path / 'out.pdf'
    source.write_text('# report')
    def interrupt(command, _env):
        output.write_bytes(b'partial')
        if failure is subprocess.TimeoutExpired:
            raise failure(command, 180)
        raise failure()
    monkeypatch.setattr(pdf_reports, '_run_renderer', interrupt)
    expected = RuntimeError if failure is subprocess.TimeoutExpired else KeyboardInterrupt
    with pytest.raises(expected):
        pdf_reports.render_markdown_pdf(source, output)
    assert not output.exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux real PDF worker')
def test_real_weasyprint_cjk_pages_and_blocked_private_resources(tmp_path, monkeypatch):
    pytest.importorskip('weasyprint')
    pypdf = pytest.importorskip('pypdf')
    node = shutil.which('node')
    if not node:
        pytest.skip('Node Markdown parser unavailable')
    if not (pdf_reports.NODE_MODULES / 'md-to-pdf').exists():
        pytest.skip('Locked Markdown parser not installed')
    monkeypatch.setenv('OOPZ_PDF_BACKEND', 'weasyprint')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    local = tmp_path / 'private.svg'
    local.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>LOCAL_DOCUMENT_CANARY</text></svg>')
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(image_bytes, format='PNG')
    image_url = 'data:image/png;base64,' + base64.b64encode(image_bytes.getvalue()).decode()
    source, output = tmp_path / '多页报告.md', tmp_path / '多页报告.pdf'
    source.write_text('# 中文报告测试\n\n' + ('## 会议记录\n\n正常中文内容，飞书总结与分页。\n\n' * 120)
                      + f'<img src="{local.as_uri()}">\n'
                      + '<img src="https://example.invalid/REMOTE_RESOURCE_CANARY">\n'
                      + f'<a rel="attachment" href="{local.as_uri()}">附件不可加载</a>\n'
                      + f'<link rel="attachment" href="{image_url}">\n'
                      + f'<a rel="attachment" href="{image_url}">附件标签保留</a>\n'
                      + '<svg xmlns="http://www.w3.org/2000/svg"><text>INLINE_SVG_CANARY</text></svg>AFTER_SVG_CANARY\n'
                      + f'<img src="{image_url}">\n', encoding='utf-8')
    rendered = pdf_reports.render_markdown_pdf(source, output)
    reader = pypdf.PdfReader(rendered)
    assert len(reader.pages) >= 3
    extracted = '\n'.join(page.extract_text() for page in reader.pages)
    assert '中文报告测试' in extracted
    assert '正常中文内容' in extracted
    assert 'LOCAL_DOCUMENT_CANARY' not in extracted
    assert 'INLINE_SVG_CANARY' not in extracted and 'AFTER_SVG_CANARY' in extracted
    assert '附件标签保留' in extracted
    assert not reader.attachments
    assert all(annotation.get_object().get('/Subtype') != '/FileAttachment'
               for page in reader.pages for annotation in page.get('/Annots', []))
    assert any(list(page.images) for page in reader.pages)
    assert f'1 / {len(reader.pages)}' in reader.pages[0].extract_text()
    # Exercise the real worker's safe diagnostic channel, not only a mock.
    checked = subprocess.run([sys.executable, '-m', 'oopz_capture.weasy_pdf', str(source), str(output),
                              '--node', node], cwd=pdf_reports.PROJECT_ROOT,
                             text=True, capture_output=True, timeout=60)
    assert checked.returncode == 0, checked.stderr
    assert 'LOCAL_DOCUMENT_CANARY' not in checked.stderr
    assert 'REMOTE_RESOURCE_CANARY' not in checked.stderr
    assert json.loads(checked.stdout)['backend'] == 'weasyprint'
