#!/usr/bin/env python3
"""
render_pdfs.py - regenerate the PDF docs from the markdown sources.

Run from the PACKAGE ROOT (the folder containing README.md):
    python docs/render_pdfs.py

Requires:  python -m pip install weasyprint markdown pypdf
Outputs to docs/pdf/.
"""
import os
import re
import sys

try:
    import markdown
    import weasyprint
except ImportError:
    sys.exit("Missing deps. Run: python -m pip install weasyprint markdown pypdf")

CSS = """
@page {
  size: letter;
  margin: 2cm;
  @bottom-center { content: counter(page) " / " counter(pages); font-size: 8pt; color: #999; }
}
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 22pt; color: #0d3b66; border-bottom: 3px solid #0d3b66;
  padding-bottom: 6px; margin-top: 0; }
h2 { font-size: 15pt; color: #0d3b66; border-bottom: 1px solid #d0d7de;
  padding-bottom: 3px; margin-top: 22px; }
h3 { font-size: 12pt; color: #1a4d7a; margin-top: 16px; }
h4 { font-size: 10.5pt; color: #333; margin-top: 12px; }
p { margin: 6px 0; }
code { font-family: 'SF Mono','Consolas','Menlo',monospace; font-size: 9pt;
  background: #f3f4f6; padding: 1px 4px; border-radius: 3px; color: #b5165c; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
  padding: 10px 12px; page-break-inside: avoid; }
pre code { background: none; padding: 0; color: #24292f; font-size: 8.5pt;
  line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9pt;
  page-break-inside: avoid; }
th { background: #0d3b66; color: #fff; text-align: left; padding: 6px 8px; }
td { border: 1px solid #d0d7de; padding: 5px 8px; vertical-align: top; }
tr:nth-child(even) td { background: #f6f8fa; }
blockquote { border-left: 4px solid #ffb703; background: #fff8e6;
  margin: 10px 0; padding: 6px 12px; color: #5a4a1a; }
blockquote p { margin: 3px 0; }
a { color: #0969da; text-decoration: none; }
ul, ol { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 18px 0; }
strong { color: #0a0a0a; }
.title-block { text-align: center; margin-bottom: 30px; padding-bottom: 20px;
  border-bottom: 2px solid #d0d7de; }
"""

def _release_version():
    """SERVER_VERSION from fusion_mcp_server.py - the one version number, so
    the PDFs can never be stamped with a stale release."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, '..', 'fusion_mcp_server.py'), encoding='utf-8') as f:
        m = re.search(r'^SERVER_VERSION = "([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("Could not find SERVER_VERSION in fusion_mcp_server.py")
    return m.group(1)


VERSION = _release_version()
HEADER = ('<div class="title-block"><div style="font-size:9pt;color:#0d3b66;'
          'font-weight:600;letter-spacing:1px;">FUSION 360 MCP &middot; v'
          + VERSION + '</div></div>')

DOCS = {
    'README.md': 'Fusion-360-MCP_README.pdf',
    'RELEASE_NOTES.md': 'Fusion-360-MCP_RELEASE-NOTES.pdf',
    'CHANGELOG.md': 'Fusion-360-MCP_CHANGELOG.pdf',
    'docs/INSTALL.md': 'Fusion-360-MCP_INSTALL.pdf',
    'docs/TOOLS.md': 'Fusion-360-MCP_TOOLS.pdf',
    'docs/ARCHITECTURE.md': 'Fusion-360-MCP_ARCHITECTURE.pdf',
    'docs/GITHUB.md': 'Fusion-360-MCP_GITHUB.pdf',
    'docs/claude_project_instructions.md': 'Fusion-360-MCP_Project-Instructions.pdf',
}

# order for the combined volume
COMBINE = ['Fusion-360-MCP_README.pdf',
           'Fusion-360-MCP_RELEASE-NOTES.pdf',
           'Fusion-360-MCP_CHANGELOG.pdf',
           'Fusion-360-MCP_INSTALL.pdf', 'Fusion-360-MCP_TOOLS.pdf',
           'Fusion-360-MCP_ARCHITECTURE.pdf',
           'Fusion-360-MCP_Project-Instructions.pdf',
           'Fusion-360-MCP_GITHUB.pdf']


def main():
    if not os.path.exists('README.md'):
        sys.exit("Run this from the package root (the folder with README.md).")
    outdir = os.path.join('docs', 'pdf')
    os.makedirs(outdir, exist_ok=True)
    md = markdown.Markdown(extensions=['extra', 'sane_lists', 'tables',
                                       'fenced_code'])
    for src, out in DOCS.items():
        if not os.path.exists(src):
            print('  skip (missing): ' + src)
            continue
        text = open(src, encoding='utf-8').read()
        md.reset()
        html = ("<html><head><meta charset='utf-8'></head><body>"
                + HEADER + md.convert(text) + "</body></html>")
        weasyprint.HTML(string=html).write_pdf(
            os.path.join(outdir, out),
            stylesheets=[weasyprint.CSS(string=CSS)])
        print('  ' + out)

    # combined volume
    try:
        from pypdf import PdfWriter, PdfReader
        w = PdfWriter()
        for name in COMBINE:
            p = os.path.join(outdir, name)
            if os.path.exists(p):
                w.append(PdfReader(p))
        with open(os.path.join(outdir, 'Fusion-360-MCP_ALL-DOCS.pdf'), 'wb') as f:
            w.write(f)
        print('  Fusion-360-MCP_ALL-DOCS.pdf (combined)')
    except ImportError:
        print('  (pypdf not installed - skipped combined PDF)')
    print('done')


if __name__ == '__main__':
    main()
