"""Isolated Linux PDF worker: shared Markdown HTML, no network/file resources."""
from __future__ import annotations

import argparse
import base64
import binascii
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote_to_bytes
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
SAFE_DATA_MIME = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
PAGED_CSS = '''
@page {
  size: A4;
  margin: 18mm 15mm;
  @bottom-center {
    content: counter(page) " / " counter(pages);
    font-family: "Noto Sans CJK SC", sans-serif;
    font-size: 8pt;
    color: #64748b;
  }
}
thead { display: table-header-group; }
tr { break-inside: avoid; }
'''


class PDFConfigurationError(RuntimeError):
    """A safe configuration diagnostic, containing no document data."""


class PrivateDiagnostics(logging.Handler):
    """WeasyPrint warnings can include entire private URLs; retain counts only."""
    def __init__(self):
        super().__init__()
        self.count = 0

    def emit(self, record):
        self.count += 1


def data_only_fetcher(url: str, **_kwargs) -> dict:
    """Decode bounded raster data URLs ourselves; never delegate any URL fetch.

    SVG, CSS/font URLs, local paths, attachments, network URLs and redirects are
    deliberately unsupported. No blocked URL (which may contain private data)
    is included in our exception message.
    """
    if not isinstance(url, str) or not url.startswith('data:'):
        raise ValueError('External PDF resources are blocked')
    header, separator, payload = url[5:].partition(',')
    if not separator or len(payload) > MAX_RESOURCE_BYTES * 4:
        raise ValueError('Invalid or oversized PDF data resource')
    fields = header.lower().split(';')
    mime = fields[0]
    if mime not in SAFE_DATA_MIME or fields[1:] not in ([], ['base64']):
        raise ValueError('PDF data resource type is not allowed')
    try:
        data = (base64.b64decode(payload, validate=True) if fields[1:] == ['base64']
                else unquote_to_bytes(payload))
    except (ValueError, binascii.Error) as error:
        raise ValueError('Invalid PDF data resource') from error
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError('Oversized PDF data resource')
    signatures = {
        'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': data.startswith(b'\xff\xd8\xff'),
        'image/gif': data.startswith((b'GIF87a', b'GIF89a')),
        'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP',
    }
    if not signatures[mime]:
        raise ValueError('PDF raster content does not match its declared type')
    return {'string': data, 'mime_type': mime}


def require_weasyprint() -> None:
    try:
        installed = version('weasyprint')
    except PackageNotFoundError as error:
        raise PDFConfigurationError('WeasyPrint is missing; install the project pdf extra (WeasyPrint >=70)') from error
    if not re.fullmatch(r'70\.\d+(?:\.\d+)?(?:\.post\d+)?', installed):
        raise PDFConfigurationError('WeasyPrint >=70,<71 stable release is required for this PDF backend')


def sanitize_report_html(html: str) -> str:
    """Preserve report text/layout while excluding active/embed/attachment channels."""
    from tinyhtml5 import parse
    root = parse(html, namespace_html_elements=False)

    def tag_name(element):
        return element.tag.rsplit('}', 1)[-1].lower() if isinstance(element.tag, str) else ''

    for parent in list(root.iter()):
        for child in list(parent):
            tag = tag_name(child)
            attachment = 'attachment' in child.get('rel', '').lower().split()
            if tag in {'svg', 'object', 'embed', 'iframe', 'script'} or (tag == 'link' and attachment):
                # Removing an element must not drop ordinary text after it.
                index = list(parent).index(child)
                if child.tail:
                    if index:
                        previous = parent[index - 1]
                        previous.tail = (previous.tail or '') + child.tail
                    else:
                        parent.text = (parent.text or '') + child.tail
                parent.remove(child)
            elif tag == 'a' and attachment:
                child.attrib.pop('href', None)
                child.attrib.pop('rel', None)
    return ElementTree.tostring(root, encoding='unicode', method='html')


def data_only_url_fetcher():
    # WeasyPrint 70 uses URLFetcherResponse, not the legacy callable/dict API.
    from weasyprint.urls import URLFetcher, URLFetcherResponse

    class DataOnlyURLFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            value = data_only_fetcher(url)
            return URLFetcherResponse(url, body=value['string'],
                                      headers={'Content-Type': value['mime_type']})

    return DataOnlyURLFetcher(allowed_protocols=('data',), allow_redirects=False,
                              fail_on_errors=False)


def limit_renderer_memory() -> None:
    # Apply after Node exits: V8 reserves a large address space even for small
    # documents. The WeasyPrint worker itself is bounded independently of ASR.
    import resource
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = MEMORY_LIMIT_BYTES if hard == resource.RLIM_INFINITY else min(MEMORY_LIMIT_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


def render(input_path: Path, output_path: Path, node: Path) -> Path:
    if sys.platform != 'linux':
        raise RuntimeError('This WeasyPrint worker supports Linux only')
    input_path, output_path = input_path.resolve(), output_path.resolve()
    if input_path.suffix.lower() != '.md' or not input_path.is_file():
        raise ValueError('Expected a Markdown file')
    if input_path == output_path:
        raise ValueError('Input and PDF output must differ')
    if input_path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError('Markdown report exceeds the 10 MiB render limit')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    require_weasyprint()
    print('PDF_STAGE: markdown-html', file=sys.stderr)
    converted = subprocess.run([str(node), '--max-old-space-size=256',
                                str(ROOT / 'tools/md_to_html.mjs'), str(input_path)],
                               cwd=ROOT, check=True, text=True, encoding='utf-8',
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    limit_renderer_memory()
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration
    diagnostics = PrivateDiagnostics()
    logger = logging.getLogger('weasyprint')
    logger.handlers = [diagnostics]
    logger.propagate = False
    # Trusted CSS is read directly, not through a URL. All document-originated
    # fetches go through the same deny-by-default fetcher, including @import.
    font_config = FontConfiguration()
    fetcher = data_only_url_fetcher()
    css = (ROOT / 'tools/md_to_pdf.css').read_text(encoding='utf-8') + '\n' + PAGED_CSS
    print('PDF_STAGE: weasyprint-layout', file=sys.stderr)
    try:
        document = HTML(string=sanitize_report_html(converted.stdout), media_type='screen',
                        url_fetcher=fetcher).render(
            stylesheets=[CSS(string=css, url_fetcher=fetcher, font_config=font_config)],
            font_config=font_config,
        )
        # Defense in depth against both EmbeddedFiles and FileAttachment links.
        document.metadata.attachments = []
        for page in document.pages:
            page.links = [link for link in page.links if link[0] != 'attachment']
        document.write_pdf(str(output_path), attachments=[])
        if not output_path.is_file() or not output_path.stat().st_size:
            raise RuntimeError('WeasyPrint did not produce a nonempty PDF')
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    print('PDF_STAGE: complete', file=sys.stderr)
    if diagnostics.count:
        print(f'PDF_NOTICE: {diagnostics.count} rendering diagnostics (private details withheld)', file=sys.stderr)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--node', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = render(args.input, args.output, args.node)
    except Exception as error:
        # Avoid echoing source HTML, requested URLs or file contents.
        detail = str(error) if isinstance(error, PDFConfigurationError) else 'renderer failed; source data not logged'
        print(f'WeasyPrint PDF failed: {type(error).__name__}: {detail}', file=sys.stderr)
        return 1
    print(json.dumps({'backend': 'weasyprint', 'bytes': result.stat().st_size, 'result': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
