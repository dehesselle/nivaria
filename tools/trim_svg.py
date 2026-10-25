"""Shrink an SVG screenshot exported by Textual (Rich's `Console.export_svg`).

Self-contained: this file imports nothing from nivaria and needs only the
standard library. Rich writes one background `<rect>` and one `<text>` per
styled segment, each with its own clip path and a long per-export prefix on
every class and id. This rewrites the same picture with

- one `<path>` per background colour, adjacent cells merged;
- adjacent text runs of one style merged, and blank runs dropped;
- the per-line clip paths removed;
- the `terminal-<n>-` prefix removed (keep it with `--keep-ids` when several
  screenshots are inlined into one HTML page);
- indentation and CSS whitespace collapsed.

A typical nivaria screenshot shrinks to about a quarter of its size.

    python tools/trim_svg.py shot.svg              # writes shot.min.svg
    python tools/trim_svg.py shot.svg -o out.svg
    python tools/trim_svg.py *.svg --in-place
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

RECT = re.compile(
    r'<rect fill="(?P<fill>[^"]+)" x="(?P<x>[\d.]+)" y="(?P<y>[\d.]+)" '
    r'width="(?P<w>[\d.]+)" height="(?P<h>[\d.]+)" shape-rendering="crispEdges"/>'
)
TEXT = re.compile(
    r'<text class="(?P<cls>[^"]+)" x="(?P<x>[\d.]+)" y="(?P<y>[\d.]+)" '
    r'textLength="(?P<w>[\d.]+)" clip-path="url\(#[^)]+-line-\d+\)">'
    r"(?P<body>[^<]*)</text>"
)
EPSILON = 0.01


class NotRichSvg(Exception):
    pass


def _num(value: float) -> str:
    # Summed widths come out as 146.39999999; three decimals is finer than
    # Rich's own one-decimal cell grid.
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _merge_runs(matches: list[re.Match[str]], key: str) -> list[dict]:
    """Join matches that sit edge to edge on one row and share `key`."""
    runs: list[dict] = []
    for m in matches:
        x, w = float(m["x"]), float(m["w"])
        last = runs[-1] if runs else None
        if (
            last is not None
            and last[key] == m[key]
            and last["y"] == m["y"]
            and abs(last["x"] + last["w"] - x) < EPSILON
        ):
            last["w"] += w
            if "body" in last:
                last["body"] += m["body"]
        else:
            runs.append({**m.groupdict(), "x": x, "w": w})
    return runs


def _replace_all(
    svg: str, pattern: re.Pattern[str], build: Callable[[list[re.Match[str]]], str]
) -> str:
    """Remove every match and put `build(matches)` where the first one was."""
    matches = list(pattern.finditer(svg))
    if not matches:
        return svg
    pieces = [svg[: matches[0].start()], build(matches)]
    for prev, m in pairwise(matches):
        pieces.append(svg[prev.end() : m.start()])
    pieces.append(svg[matches[-1].end() :])
    return "".join(pieces)


def _backgrounds(matches: list[re.Match[str]]) -> str:
    # Dict order is first appearance, so colours stack roughly as Rich drew
    # them; rows overlap by Rich's 0.25 px anti-seam margin only.
    paths: dict[str, list[str]] = {}
    for run in _merge_runs(matches, "fill"):
        w = _num(run["w"])
        paths.setdefault(run["fill"], []).append(
            f"M{_num(run['x'])} {run['y']}h{w}v{run['h']}h-{w}z"
        )
    return "".join(
        f'<path fill="{fill}" shape-rendering="crispEdges" d="{"".join(d)}"/>'
        for fill, d in paths.items()
    )


def _texts(css_by_class: dict[str, str]) -> Callable[[list[re.Match[str]]], str]:
    def build(matches: list[re.Match[str]]) -> str:
        out = []
        for run in _merge_runs(matches, "cls"):
            blank = not run["body"].replace("&#160;", "").strip()
            # A blank run still paints its underline or strike-through.
            if blank and "text-decoration" not in css_by_class.get(run["cls"], ""):
                continue
            out.append(
                f'<text class="{run["cls"]}" x="{_num(run["x"])}" y="{run["y"]}" '
                f'textLength="{_num(run["w"])}">{run["body"]}</text>'
            )
        return "".join(out)

    return build


def _minify_css(css: str) -> str:
    css = re.sub(r"\s+", " ", css)
    return re.sub(r"\s*([{};:,])\s*", r"\1", css).strip()


def trim(svg: str, *, keep_ids: bool = False) -> str:
    found = re.search(r'<clipPath id="([^"]+)-line-0">', svg)
    if found is None:
        raise NotRichSvg("not an untrimmed Rich/Textual SVG")
    prefix = found[1]

    css_by_class = dict(
        re.findall(rf"\.({re.escape(prefix)}-r\d+) \{{([^}}]*)\}}", svg)
    )
    svg = _replace_all(svg, RECT, _backgrounds)
    svg = _replace_all(svg, TEXT, _texts(css_by_class))
    svg = re.sub(
        rf'\s*<clipPath id="{re.escape(prefix)}-line-\d+">\s*<rect[^>]*/>\s*</clipPath>',
        "",
        svg,
    )
    if f"{prefix}-line-" in svg:
        raise NotRichSvg("unexpected Rich SVG layout; left some elements untrimmed")

    if not keep_ids:
        svg = svg.replace(f"{prefix}-", "")
    svg = re.sub(
        r"<style>(.*?)</style>",
        lambda m: f"<style>{_minify_css(m[1])}</style>",
        svg,
        flags=re.DOTALL,
    )
    return re.sub(r">\s+<", "><", svg).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shrink SVG screenshots exported by Textual/Rich."
    )
    parser.add_argument("svg", nargs="+", type=Path)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("-o", "--out", type=Path, help="output file (one input only)")
    where.add_argument(
        "--in-place", action="store_true", help="overwrite each input file"
    )
    parser.add_argument(
        "--keep-ids",
        action="store_true",
        help="keep Rich's per-export prefix, for inlining several SVGs in one page",
    )
    args = parser.parse_args()
    if args.out and len(args.svg) > 1:
        parser.error("--out takes a single input")

    failed = False
    for src in args.svg:
        original = src.read_text(encoding="utf-8")
        try:
            trimmed = trim(original, keep_ids=args.keep_ids)
        except NotRichSvg as error:
            print(f"{src}: {error}", file=sys.stderr)
            failed = True
            continue
        dst = src if args.in_place else args.out or src.with_suffix(".min.svg")
        dst.write_text(trimmed, encoding="utf-8")
        print(f"{src} -> {dst}: {len(original):,} -> {len(trimmed):,} bytes")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
