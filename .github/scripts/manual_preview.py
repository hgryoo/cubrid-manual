#!/usr/bin/env python3
"""Render a cubrid-manual change as HTML with the additions and deletions marked.

Per ``issue/methodology/manual-writing.md`` §6.6–§6.7, the author shows the
rendered change before opening the PR. Reviewers read the built pages, not
the RST diff, so this builds the manual twice and marks the difference on the
rendered page:

1. Find the ko/en documents the change touches (``BASE...HEAD``). A changed
   ``.inc`` file brings in every page that includes it.
2. Build the HEAD checkout (the worktree given by ``--repo``) and BASE (a
   temporary detached worktree) with ``sphinx-build -b html``.
3. For each changed page, diff the ``div#content`` body of BASE and HEAD with
   ``lxml.html.diff.htmldiff`` and write it into a copy of the HEAD site:
   ``<ins>`` (green) marks added text, ``<del>`` (red, struck through) removed
   text. A new page is marked as added in full.
4. Write ``index.html`` listing the changed pages per language, then serve the
   site with ``--serve PORT``.

CI mode (the manual's GitHub Actions PR preview) takes the HEAD build that
``make html`` already made (``--head-html``, one language) and, with
``--inplace``, writes each marked page next to its original as
``<page>.diff.html`` plus a list page ``_changes.html`` in that directory, so
the marked pages are published with the normal preview and keep its links,
styles and images. It exits 0 when the language has no changed page.

Needs ``lxml``: run it with the manual venv's interpreter, e.g.
``/data/workspace/venv/cubrid-manual/bin/python manual_preview.py …``.

Usage::

    manual_preview.py --repo <manual worktree> [--base origin/develop]
        [--lang ko,en] [--out DIR] [--sphinx-build PATH] [--serve PORT]
    manual_preview.py --repo . --base origin/develop --lang ko
        --head-html ko/_build/html --inplace [--sphinx-build PATH]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import http.server
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import lxml.html as LH
    from lxml.html.diff import htmldiff
except ImportError:  # pragma: no cover - environment check
    sys.exit("manual_preview.py needs lxml; run it with the manual venv's python")

STYLE = """
ins.mp-diff, ins { background: #c8f2c8; color: inherit; text-decoration: none; }
del.mp-diff, del { background: #f8d0d0; color: #7a1f1f; text-decoration: line-through; }
@media (prefers-color-scheme: dark) {
  ins { background: #1f5130; } del { background: #5c2424; color: #f2c4c4; }
}
.mp-banner { position: sticky; top: 0; z-index: 50; margin: 0 0 1rem; padding: .5rem .75rem;
  border: 1px solid #9ac79a; border-radius: 6px; background: #f2fbf2; color: #1d3b1d; font-size: .9rem; }
@media (prefers-color-scheme: dark) { .mp-banner { background: #16281a; color: #cfe8cf; border-color: #2f6b3a; } }
.mp-banner a { text-decoration: underline; }
.mp-code pre, .mp-delblock pre { white-space: pre; overflow-x: auto; padding: .75rem 1rem; border-radius: 6px;
  background: #f6f8fa; color: #1b1b1b; font-size: .85rem; line-height: 1.5; }
.mp-code ins.mp-line, .mp-code del.mp-line { display: block; }
.mp-newblock { outline: 3px solid #5cb85c; border-radius: 6px; }
.mp-delblock pre { background: #f8d0d0; text-decoration: line-through; }
@media (prefers-color-scheme: dark) { .mp-code pre, .mp-delblock pre { background: #1e1e1e; color: #e6e6e6; } }
.mp-changed { box-shadow: -8px 0 0 #5cb85c; scroll-margin-top: 140px; }
tr.mp-changed > td:first-child { box-shadow: inset 6px 0 0 #5cb85c; }
.mp-current { outline: 3px solid #f0ad4e; outline-offset: 2px; }
.mp-nav { margin-left: .5rem; white-space: nowrap; }
.mp-nav button { padding: 0 .5rem; border: 1px solid #9ac79a; border-radius: 4px; background: #fff; cursor: pointer; }
@media (prefers-color-scheme: dark) { .mp-nav button { background: #1e1e1e; color: #cfe8cf; } }
"""


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def changed_docs(repo: pathlib.Path, base: str, head: str, langs: list[str]) -> dict[str, list[str]]:
    """Map language -> docnames (path under the language dir, no suffix) touched by BASE...HEAD."""
    names = git(repo, "diff", "--name-only", f"{base}...{head}", "--", *langs).split()
    out: dict[str, set[str]] = {lang: set() for lang in langs}
    for name in names:
        lang, _, rel = name.partition("/")
        if lang not in out or not rel.endswith((".rst", ".inc")):
            continue
        if rel.endswith(".inc"):
            inc = pathlib.Path(rel).name
            for rst in (repo / lang).rglob("*.rst"):
                if re.search(rf"^\s*\.\. include::\s+\S*{re.escape(inc)}\s*$",
                             rst.read_text(encoding="utf-8", errors="replace"), re.M):
                    out[lang].add(str(rst.relative_to(repo / lang).with_suffix("")))
        out[lang].add(str(pathlib.Path(rel).with_suffix("")))
    return {lang: sorted(docs) for lang, docs in out.items()}


def build(src: pathlib.Path, lang: str, dest: pathlib.Path, sphinx: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sphinx, "-q", "-b", "html", "-d", str(dest.parent / f".doctrees-{lang}"),
                           str(src / lang), str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"sphinx-build failed for {src / lang}:\n{proc.stderr[-2000:]}")


def content_div(path: pathlib.Path):
    if not path.exists():
        return None, None
    tree = LH.parse(str(path))
    found = tree.xpath('//div[@id="content"]')
    return tree, (found[0] if found else None)


def inner_html(el) -> str:
    return (el.text or "") + "".join(LH.tostring(c, encoding="unicode") for c in el)


def pull_code_blocks(div) -> dict[str, tuple[str, str]]:
    """Replace each rendered code block in ``div`` with a placeholder paragraph.

    htmldiff diffs word by word and drops the whitespace between the highlight
    spans of a ``<pre>``, which garbles even unchanged examples. Code blocks are
    therefore taken out before the diff: the placeholder carries a hash of the
    block's text, so an unchanged block yields the same placeholder on both
    sides and a changed one yields a deleted and an inserted placeholder.
    Returns placeholder key -> (block HTML, block text).
    """
    blocks: dict[str, tuple[str, str]] = {}
    for pre in div.xpath(".//pre"):
        block = pre
        for anc in pre.iterancestors():
            if anc is div:
                break
            if anc.tag == "div" and any(c.startswith("highlight-") for c in (anc.get("class") or "").split()):
                block = anc
                break
        if block.getparent() is None:
            continue  # already replaced through another <pre> in the same block
        text = pre.text_content()
        key = hashlib.sha1(text.encode()).hexdigest()[:12]
        blocks[key] = (LH.tostring(block, encoding="unicode"), text)
        ph = LH.fromstring(f'<p class="mp-ph">@@P{key}@@</p>')
        ph.tail = block.tail
        block.getparent().replace(block, ph)
    return blocks


PH_RE = re.compile(r'(?:<p[^>]*>)?\s*(?:<(ins|del)>)?\s*@@P([0-9a-f]{12})@@\s*(?:</(?:ins|del)>)?\s*(?:</p>)?')


def code_line_diff(old: str, new: str) -> str:
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old.splitlines(), new.splitlines()).get_opcodes():
        if tag in ("replace", "delete"):
            rows += [f'<del class="mp-line">{html.escape(x) or " "}</del>' for x in old.splitlines()[i1:i2]]
        if tag in ("replace", "insert"):
            rows += [f'<ins class="mp-line">{html.escape(x) or " "}</ins>' for x in new.splitlines()[j1:j2]]
        if tag == "equal":
            rows += [html.escape(x) for x in new.splitlines()[j1:j2]]
    return '<div class="mp-code"><pre>' + "\n".join(rows) + "</pre></div>"


def restore_code_blocks(marked: str, base_blocks: dict, head_blocks: dict) -> str:
    """Put code blocks back: unchanged as rendered, changed as a line diff."""
    hits = list(PH_RE.finditer(marked))
    out, pos, i = [], 0, 0
    while i < len(hits):
        m = hits[i]
        kind, key = m.group(1), m.group(2)
        out.append(marked[pos:m.start()])
        nxt = hits[i + 1] if i + 1 < len(hits) else None
        if (kind and nxt is not None and nxt.group(1) and nxt.group(1) != kind
                and nxt.start() - m.end() < 80):  # a changed block: htmldiff emits del/ins in either order
            old_key, new_key = (key, nxt.group(2)) if kind == "del" else (nxt.group(2), key)
            out.append(code_line_diff(base_blocks[old_key][1], head_blocks[new_key][1]))
            pos, i = nxt.end(), i + 2
            continue
        if kind == "ins":
            out.append(f'<div class="mp-newblock">{head_blocks[key][0]}</div>')
        elif kind == "del":
            out.append(f'<div class="mp-delblock"><pre>{html.escape(base_blocks[key][1])}</pre></div>')
        else:
            out.append(head_blocks.get(key, base_blocks.get(key, ("", "")))[0])
        pos, i = m.end(), i + 1
    out.append(marked[pos:])
    return "".join(out)


BLOCK_TAGS = {"p", "li", "tr", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
MARK_DIVS = {"mp-code", "mp-newblock", "mp-delblock"}

NAV_JS = """
(function () {
  var m = [].slice.call(document.querySelectorAll('[data-mp]')), i = -1;
  function go(k) {
    if (!m.length) return;
    m.forEach(function (e) { e.classList.remove('mp-current'); });
    i = k; m[i].classList.add('mp-current');
    // scroll directly: the theme's smooth scrolling does not settle reliably on table rows
    var r = (m[i].tagName === 'TR' ? m[i].cells[0] : m[i]).getBoundingClientRect();
    window.scrollTo({top: window.scrollY + r.top - window.innerHeight / 3, behavior: 'instant'});
    var c = document.getElementById('mp-pos'); if (c) c.textContent = (i + 1) + ' / ' + m.length;
  }
  window.mpStep = function (d) { go((i + d + m.length) % m.length); };
  document.addEventListener('keydown', function (e) {
    if (/INPUT|TEXTAREA/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'n') mpStep(1); else if (e.key === 'p') mpStep(-1);
  });
  window.addEventListener('load', function () {
    var h = /^#mp-(\\d+)$/.exec(location.hash);
    if (h) go(Math.min(+h[1], m.length) - 1);
  });
})();
"""

# Banner and list-page texts per language; "changes" is formatted with n and s (English plural).
TEXT = {
    "ko": {"preview": "변경 미리보기", "added": "추가", "deleted": "삭제", "list": "변경된 페이지 목록",
           "unmarked": "표시 없이 보기", "changes": "변경 {n}곳", "prev": "◀ 이전", "next": "다음 ▶",
           "keys": "(단축키 n / p)", "title": "매뉴얼 변경 미리보기", "page": "페이지", "blocks": "변경 블록",
           "new": "새 페이지", "nohtml": "head 에 HTML 페이지 없음",
           "none": "이 언어({lang})에는 바뀐 페이지가 없다.",
           "intro": "페이지 안에서 초록 배경은 추가, 빨간 취소선은 삭제다. 페이지 링크는 첫 번째 변경 위치로 열리고, "
                    "페이지 위 배너의 이전·다음 버튼(단축키 n / p)으로 바뀐 곳을 차례로 옮겨 다닌다. "
                    "바뀐 블록 왼쪽에는 초록 막대가 있다. \"변경 블록\"은 바뀐 문단·목록 항목·표 행·코드 블록의 수이고, 추가·삭제는 표시된 구간의 수다."},
    "en": {"preview": "Change preview", "added": "added", "deleted": "deleted", "list": "list of changed pages",
           "unmarked": "view without marks", "changes": "{n} change{s}", "prev": "◀ prev", "next": "next ▶",
           "keys": "(keys n / p)", "title": "Manual change preview", "page": "Page", "blocks": "Changed blocks",
           "new": "new page", "nohtml": "no HTML page in head",
           "none": "No page changed in this language ({lang}).",
           "intro": "On a page, a green background marks added text and red strikethrough deleted text. A page link "
                    "opens at the first change; the prev/next buttons in the banner (keys n / p) step through the "
                    "changes, and a green bar marks the left of each changed block. \"Changed blocks\" counts the changed paragraphs, list items, table rows and code "
                    "blocks; added and deleted count the marked spans."},
}


def drop_invisible_marks(div) -> None:
    """Unmark changes with no text, e.g. a heading's permalink whose generated id moved."""
    for el in list(div.iter("ins", "del")):
        if el.text_content().strip() or el.getparent() is None:
            continue
        if el.tag == "del":
            el.drop_tree()  # keeps its tail text
        else:
            el.drop_tag()


def number_changes(div) -> int:
    """Number the blocks that hold a change (``data-mp``, anchors ``#mp-N``) for the nav bar."""
    drop_invisible_marks(div)
    blocks = []
    for el in div.iter("ins", "del", "div"):
        classes = set((el.get("class") or "").split())
        if el.tag == "div":
            if not classes & MARK_DIVS:
                continue
            blk = el
        else:
            anc = list(el.iterancestors())
            if any(set((a.get("class") or "").split()) & (MARK_DIVS | {"mp-banner"}) for a in anc):
                continue  # inside a code diff (already its own block) or the banner
            inside = anc[:anc.index(div)] if div in anc else anc
            # a table row or list item is one change, not one per cell or paragraph
            blk = el
            for wanted in ({"tr"}, {"li"}, BLOCK_TAGS):
                found = next((a for a in inside if a.tag in wanted), None)
                if found is not None:
                    blk = found
                    break
        if blk not in blocks and not any(b in blk.iterancestors() for b in blocks):
            blocks.append(blk)
    for n, blk in enumerate(blocks, 1):
        blk.set("data-mp", str(n))
        blk.set("class", ((blk.get("class") or "") + " mp-changed").strip())
        holder = blk
        if blk.tag == "tr":  # header rows have th cells only
            holder = next((c for c in blk if c.tag in ("td", "th")), blk)
        anchor = LH.fromstring(f'<a id="mp-{n}" class="mp-anchor"></a>')
        anchor.tail, holder.text = holder.text, None
        holder.insert(0, anchor)
    return len(blocks)


def mark_page(base_page: pathlib.Path, head_page: pathlib.Path, out_page: pathlib.Path,
              banner: str, t: dict[str, str]) -> tuple[int, int, int]:
    """Write HEAD's page with BASE->HEAD differences marked; return (#ins, #del, #changed blocks)."""
    head_tree, head_div = content_div(head_page)
    if head_div is None:
        return 0, 0, 0  # page not built (e.g. an .inc rendered only through its includer)
    _, base_div = content_div(base_page)
    if base_div is None:
        marked = f"<ins>{inner_html(head_div)}</ins>"
    else:
        base_blocks, head_blocks = pull_code_blocks(base_div), pull_code_blocks(head_div)
        marked = htmldiff(inner_html(base_div), inner_html(head_div))
        marked = re.sub(r"\s*Link: [^\s<]+", "", marked)  # htmldiff spells out changed hrefs
        # htmldiff also wraps the whitespace between cells, e.g. </td><ins> </ins><td>. A browser
        # moves such an <ins> out of the <tr>, which breaks the table, so drop the empty marks.
        marked = re.sub(r"<(ins|del)>(\s*)</\1>", r"\2", marked)
        marked = restore_code_blocks(marked, base_blocks, head_blocks)
    for child in list(head_div):
        head_div.remove(child)
    head_div.text = None
    for frag in LH.fragments_fromstring(banner + marked):
        if isinstance(frag, str):
            head_div.text = (head_div.text or "") + frag
        else:
            head_div.append(frag)
    n_blocks = number_changes(head_div)
    n_ins = sum(1 for _ in head_div.iter("ins")) - banner.count("<ins")  # the banner's legend
    n_del = sum(1 for _ in head_div.iter("del")) - banner.count("<del")
    nav = LH.fromstring(f'<span class="mp-nav">{t["changes"].format(n=n_blocks, s="" if n_blocks == 1 else "s")} '
                        f'<button type="button" onclick="mpStep(-1)">{t["prev"]}</button> '
                        f'<span id="mp-pos">– / {n_blocks}</span> '
                        f'<button type="button" onclick="mpStep(1)">{t["next"]}</button> {t["keys"]}</span>')
    head_div.find('div[@class="mp-banner"]').append(nav)
    root = head_tree.getroot()
    root.find("head").append(LH.fromstring(f"<style>{STYLE}</style>"))
    script = LH.Element("script")
    script.text = NAV_JS
    root.find("body").append(script)
    out_page.parent.mkdir(parents=True, exist_ok=True)
    out_page.write_bytes(LH.tostring(head_tree, encoding="utf-8", doctype="<!DOCTYPE html>"))
    return n_ins, n_del, n_blocks


LIST_STYLE = """body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:860px;padding:0 16px;background:#fff;color:#1b1b1b}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.4rem .5rem;text-align:left}
@media (prefers-color-scheme: dark){body{background:#121212;color:#e6e6e6}td,th{border-color:#333}a{color:#8ab4f8}}"""


def write_changes(path: pathlib.Path, lang: str, rows: list, base_sha: str, head_sha: str) -> None:
    """Write ``--inplace``'s list of one language's changed pages; its links are relative to ``path``."""
    t = TEXT[lang]
    items = []
    for _, doc, rel, n_ins, n_del, n_blk, is_new in rows:
        if not (path.parent / rel).exists():  # an .inc without its own page, or a page deleted in head
            items.append(f'<tr><td>{html.escape(doc)} ({t["nohtml"]})</td><td></td><td></td><td></td><td></td></tr>')
            continue
        diff = html.escape(f"{doc}.diff.html") + ("#mp-1" if n_blk else "")
        new = f' ({t["new"]})' if is_new else ""
        items.append(f'<tr><td><a href="{diff}">{html.escape(doc)}</a>{new}</td><td>{n_blk}</td><td>{n_ins}</td>'
                     f'<td>{n_del}</td><td><a href="{html.escape(rel)}">{t["unmarked"]}</a></td></tr>')
    shas = f'base <code>{base_sha[:9]}</code> → head <code>{head_sha[:9]}</code>.'
    if rows:
        body = (f'<p>{shas} {t["intro"]}</p>\n<table><tr><th>{t["page"]}</th><th>{t["blocks"]}</th>'
                f'<th>{t["added"].capitalize()}</th><th>{t["deleted"].capitalize()}</th>'
                f'<th>{t["unmarked"].capitalize()}</th></tr>\n' + "\n".join(items) + "\n</table>")
    else:
        body = f'<p>{shas} {t["none"].format(lang=lang)}</p>'
    path.write_text(f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{t["title"]} ({lang})</title>
<style>{LIST_STYLE}</style>
</head><body><h1>{t["title"]} ({lang})</h1>
{body}
</body></html>""", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=".", help="manual worktree at HEAD (default: cwd)")
    p.add_argument("--base", default="origin/develop")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--lang", default="ko,en")
    p.add_argument("--out", default=None, help="output dir (default: a temp dir)")
    p.add_argument("--sphinx-build", default=shutil.which("sphinx-build")
                   or str(pathlib.Path(sys.executable).with_name("sphinx-build")))
    p.add_argument("--head-html", metavar="DIR",
                   help="HEAD's HTML already built for the one language in --lang (HEAD is not rebuilt)")
    p.add_argument("--inplace", action="store_true",
                   help="write <page>.diff.html next to each page in --head-html and _changes.html there")
    p.add_argument("--serve", type=int, metavar="PORT", help="serve the marked site on this port")
    p.add_argument("--bind", default="0.0.0.0")
    a = p.parse_args(argv)

    langs = [x for x in a.lang.split(",") if x]
    if not langs or set(langs) - TEXT.keys():
        p.error("--lang takes ko, en or ko,en")
    if a.head_html and len(langs) != 1:
        p.error("--head-html needs exactly one language in --lang")
    if a.inplace and not a.head_html:
        p.error("--inplace needs --head-html")
    if a.inplace and a.serve:
        p.error("--serve cannot be used with --inplace")
    head_html = pathlib.Path(a.head_html).resolve() if a.head_html else None
    if head_html and not (head_html / "index.html").is_file():
        p.error(f"--head-html {a.head_html}: no index.html (build it first)")

    repo = pathlib.Path(git(pathlib.Path(a.repo), "rev-parse", "--show-toplevel").strip())
    base_sha = git(repo, "merge-base", a.base, a.head).strip()
    head_sha = git(repo, "rev-parse", a.head).strip()
    if git(repo, "rev-parse", "HEAD").strip() != head_sha:
        sys.exit("--repo must be checked out at --head (build HEAD from its worktree)")
    docs = changed_docs(repo, a.base, a.head, langs)
    if not any(docs.values()):
        if a.inplace:  # e.g. a PR that changes only the other language: not an error in CI
            write_changes(head_html / "_changes.html", langs[0], [], base_sha, head_sha)
            print(f"no {langs[0]} .rst/.inc changes in {a.base}...{a.head}; wrote {head_html / '_changes.html'}")
            return 0
        sys.exit(f"no ko/en .rst/.inc changes in {a.base}...{a.head}")

    out = pathlib.Path(a.out or tempfile.mkdtemp(prefix="manual-preview-"))
    if out.exists():
        shutil.rmtree(out)
    site = out / "site"
    base_wt = out / "base-src"
    heads = {lang: head_html or out / "head" / lang for lang in langs}
    git(repo, "worktree", "add", "--detach", str(base_wt), base_sha)
    try:
        for lang in langs:
            if head_html is None:
                print(f"building {lang}: head …", flush=True)
                build(repo, lang, heads[lang], a.sphinx_build)
            print(f"building {lang}: base {base_sha[:9]} …", flush=True)
            build(base_wt, lang, out / "base" / lang, a.sphinx_build)
            if not a.inplace:
                shutil.copytree(heads[lang], site / lang)
    finally:
        git(repo, "worktree", "remove", "--force", str(base_wt))

    rows = []
    for lang in langs:
        t = TEXT[lang]
        for doc in docs[lang]:
            rel = f"{doc}.html"
            if a.inplace:  # the marked page sits next to the original; links stay relative to it
                out_page = head_html / f"{doc}.diff.html"
                links = (f'<a href="{"../" * rel.count("/")}_changes.html">{t["list"]}</a>'
                         f' · <a href="{html.escape(pathlib.PurePosixPath(rel).name)}">{t["unmarked"]}</a>')
            else:
                out_page = site / lang / rel
                links = f'<a href="{"../" * (rel.count("/") + 1)}index.html">{t["list"]}</a>'
            banner = (f'<div class="mp-banner">{t["preview"]} — <ins>{t["added"]}</ins> · <del>{t["deleted"]}</del>'
                      f' · base <code>{base_sha[:9]}</code> → head <code>{head_sha[:9]}</code> · {links}</div>')
            n_ins, n_del, n_blk = mark_page(out / "base" / lang / rel, heads[lang] / rel, out_page, banner, t)
            is_new = not (out / "base" / lang / rel).exists()
            rows.append((lang, doc, rel, n_ins, n_del, n_blk, is_new))

    if a.inplace:
        write_changes(head_html / "_changes.html", langs[0], rows, base_sha, head_sha)
        if a.out is None:
            shutil.rmtree(out, ignore_errors=True)
        print(f"changes: {head_html / '_changes.html'}")
        for lang, doc, rel, i, d, b, new in rows:
            print(f"  {lang}/{doc}.diff.html: {b} block(s), +{i} -{d}{' (new)' if new else ''}")
        return 0

    items = "\n".join(
        f'<tr><td>{lang}</td><td><a href="{lang}/{html.escape(rel)}{"#mp-1" if b else ""}">{html.escape(doc)}</a>'
        f'{" (새 페이지)" if new else ""}</td><td>{b}</td><td>{i}</td><td>{d}</td></tr>'
        for lang, doc, rel, i, d, b, new in rows)
    (site / "index.html").write_text(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>매뉴얼 변경 미리보기</title>
<style>{LIST_STYLE}</style>
</head><body><h1>매뉴얼 변경 미리보기</h1>
<p>base <code>{base_sha[:9]}</code> → head <code>{head_sha[:9]}</code>. 페이지 안에서 초록 배경은 추가, 빨간 취소선은 삭제다.
페이지 링크는 첫 번째 변경 위치로 열리고, 페이지 위 배너의 이전·다음 버튼(단축키 n / p)으로 바뀐 곳을 차례로 옮겨 다닌다. 바뀐 블록 왼쪽에는 초록 막대가 있다.
"변경 블록"은 바뀐 문단·목록 항목·표 행·코드 블록의 수이고, 추가·삭제는 표시된 구간의 수다. 바뀌지 않은 페이지는 <a href="ko/index.html">ko</a> · <a href="en/index.html">en</a> 에서 그대로 볼 수 있다.</p>
<table><tr><th>언어</th><th>페이지</th><th>변경 블록</th><th>추가</th><th>삭제</th></tr>
{items}
</table></body></html>""", encoding="utf-8")
    print(f"site: {site}")
    for lang, doc, rel, i, d, b, new in rows:
        print(f"  {lang}/{rel}: {b} block(s), +{i} -{d}{' (new)' if new else ''}")

    if a.serve:
        handler = lambda *x, **k: http.server.SimpleHTTPRequestHandler(*x, directory=str(site), **k)
        srv = http.server.ThreadingHTTPServer((a.bind, a.serve), handler)
        print(f"serving on http://{a.bind}:{a.serve}/  (Ctrl-C to stop)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
