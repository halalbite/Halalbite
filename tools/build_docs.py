#!/usr/bin/env python3
"""Erzeugt docs/datenschutz.html und docs/nutzungsbedingungen.html aus den Markdown-Dateien der App.
Benutzung: python3 tools/build_docs.py  → danach die Dateien aus docs/ auf GitHub hochladen."""
import html, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def md_to_html(md):
    out, para, inlist = [], [], False
    def inline(t):
        t = html.escape(t)
        t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
        return re.sub(r'(https?://[^\s<,)]+)', r'<a href="\1">\1</a>', t)
    def flush():
        nonlocal para
        if para:
            out.append('<p>' + '<br>'.join(inline(x) for x in para) + '</p>')
            para = []
    for line in md.splitlines():
        l = line.strip()
        if not l:
            flush()
            if inlist: out.append('</ul>'); inlist = False
            continue
        if l.startswith('- '):
            flush()
            if not inlist: out.append('<ul>'); inlist = True
            out.append('<li>' + inline(l[2:]) + '</li>'); continue
        if inlist: out.append('</ul>'); inlist = False
        if l.startswith('## '): flush(); out.append('<h2>' + inline(l[3:]) + '</h2>')
        elif l.startswith('# '): flush(); out.append('<h1>' + inline(l[2:]) + '</h1>')
        else: para.append(l)
    flush()
    if inlist: out.append('</ul>')
    return '\n'.join(out)

def page(title, body):
    return f'''<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} – HalalBite</title>
<style>
:root{{color-scheme:light dark;--g:#00553c}}
body{{font:17px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:720px;margin:0 auto;padding:24px}}
header b{{font-size:24px;color:var(--g)}} h1{{font-size:28px}} h2{{font-size:20px;margin-top:28px}}
a{{color:var(--g)}} nav a{{margin-right:16px}}
@media (prefers-color-scheme:dark){{a,header b{{color:#6ed7a5}}}}
footer{{margin-top:40px;font-size:14px;opacity:.7}}
</style></head><body>
<header><b>HalalBite</b></header>
<nav><a href="index.html">Support</a><a href="datenschutz.html">Datenschutz</a><a href="nutzungsbedingungen.html">Nutzungsbedingungen</a></nav>
{body}
<footer>© 2026 Ciraje Beltifa · <a href="mailto:halalbiteapp@gmail.com">halalbiteapp@gmail.com</a></footer>
</body></html>'''

for src, dst, title in [("Datenschutz.md", "datenschutz.html", "Datenschutz"),
                        ("Nutzungsbedingungen.md", "nutzungsbedingungen.html", "Nutzungsbedingungen")]:
    md = (ROOT / "HalalBite2" / src).read_text()
    (ROOT / "docs" / dst).write_text(page(title, md_to_html(md)))
    print("✅", "docs/" + dst)
