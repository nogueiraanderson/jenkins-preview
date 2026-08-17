"""Render demo/loop.svg from a scene spec of captured terminal output.

Usage: uv run python demo/record.py demo/loop-scenes.json > demo/loop.svg

The spec is JSON: a title, the scene index to pin when the viewer prefers
reduced motion, and one scene per command. Every text line is a [role, text]
pair whose text is pasted verbatim from a captured session; this script only
lays out, colors, truncates and schedules, it never invents content.
"""

import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

WIDTH, HEIGHT = 860, 364
LEFT, TOP, LINE_STEP = 18, 75.5, 17.5
MAX_LINES = 16
TRUNCATE_AT = 104  # characters, including the ellipsis that marks the cut

COLORS = {
    "cmd": "#eef4fb",
    "out": "#dbe5f0",
    "ok": "#4fd581",
    "warn": "#ffb454",
    "err": "#ff6b78",
    "dim": "#8496a8",
    "url": "#4fd1ff",
}

SCENE_FADE = 0.45
FIRST_LINE_DELAY = 0.35
TYPE_PAUSE = 0.9  # between the command line and its first output line
LINE_STEP_SECONDS = 0.4
SCENE_DWELL = 2.4  # reading time after the last line
SCENE_GAP = 0.25
LINE_FADE = 0.1


def clip(text: str) -> str:
    if len(text) <= TRUNCATE_AT:
        return text
    return text[: TRUNCATE_AT - 1].rstrip() + "…"


def pct(seconds: float, total: float) -> float:
    return round(min(seconds / total * 100.0, 100.0), 3)


def schedule(scenes: list[dict]) -> tuple[list[dict], float]:
    """Assign absolute times: scene visibility windows and per-line reveals."""
    timed = []
    clock = 0.0
    for scene in scenes:
        start = clock
        line_times = []
        t = start + SCENE_FADE + FIRST_LINE_DELAY
        for index, (_, _) in enumerate(scene["lines"]):
            line_times.append(t)
            t += TYPE_PAUSE if index == 0 else LINE_STEP_SECONDS
        end = t - LINE_STEP_SECONDS + SCENE_DWELL
        timed.append({**scene, "start": start, "end": end, "line_times": line_times})
        clock = end + SCENE_FADE + SCENE_GAP
    return timed, clock


def render(spec: dict) -> str:
    timed, total = schedule(spec["scenes"])
    css = [
        "text{white-space:pre} .chip{fill:#64758a;font-size:11px}"
        " .ttl{fill:#8496a8;font-size:11.5px}"
    ]
    body = []
    for number, scene in enumerate(timed):
        if len(scene["lines"]) > MAX_LINES:
            raise SystemExit(f"scene {number} has {len(scene['lines'])} lines, max {MAX_LINES}")
        s_in, s_on = pct(scene["start"], total), pct(scene["start"] + SCENE_FADE, total)
        s_off, s_out = pct(scene["end"], total), pct(scene["end"] + SCENE_FADE, total)
        css.append(
            f"@keyframes sc{number}{{0%,{s_in}%{{opacity:0}}"
            f"{s_on}%,{s_off}%{{opacity:1}}{s_out}%,100%{{opacity:0}}}}"
        )
        css.append(f"#s{number}{{animation:sc{number} {total:.1f}s linear infinite;opacity:0}}")
        body.append(f'<g id="s{number}">')
        chip = escape(scene["chip"])
        body.append(f'<text x="{WIDTH - 18}" y="40" text-anchor="end" class="chip">{chip}</text>')
        for index, (role, text) in enumerate(scene["lines"]):
            l_in = pct(scene["line_times"][index], total)
            l_on = pct(scene["line_times"][index] + LINE_FADE, total)
            css.append(
                f"@keyframes ln{number}x{index}{{0%,{l_in}%{{opacity:0}}{l_on}%,100%{{opacity:1}}}}"
            )
            css.append(
                f"#s{number} .l{index}{{animation:ln{number}x{index} "
                f"{total:.1f}s linear infinite;opacity:0}}"
            )
            y = TOP + index * LINE_STEP
            body.append(
                f'<text x="{LEFT}" y="{y}" class="ln l{index}" '
                f'fill="{COLORS[role]}">{escape(clip(text))}</text>'
            )
        body.append("</g>")
    css.append(
        "@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}"
        " .cur{animation:blink 1.1s steps(1) infinite}"
    )
    pinned = spec["static_scene"]
    css.append(
        "@media (prefers-reduced-motion:reduce){"
        " .ln,[id^=s],.cur{animation:none!important}"
        " .ln,[id^=s]{opacity:0!important}"
        f" #s{pinned},#s{pinned} .ln{{opacity:1!important}}"
        " .cur{opacity:0!important} }"
    )
    title = escape(spec["title"])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}"'
        ' font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"'
        ' font-size="12.6">',
        "<style>",
        "\n".join(css),
        "</style>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="12" fill="#0a0f16"/>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="12" fill="none" stroke="#22303f"/>',
        '<circle cx="22" cy="21" r="5.5" fill="#ff5f57"/>'
        '<circle cx="42" cy="21" r="5.5" fill="#febc2e"/>'
        '<circle cx="62" cy="21" r="5.5" fill="#28c840"/>',
        f'<text x="{WIDTH / 2}" y="25" text-anchor="middle" class="ttl">{title}</text>',
        *body,
        f'<text x="{LEFT}" y="350" class="cur" fill="#4fd1ff">▌</text>',
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: record.py <scenes.json>  (svg on stdout)")
    with Path(sys.argv[1]).open(encoding="utf-8") as handle:
        spec = json.load(handle)
    sys.stdout.write(render(spec))


if __name__ == "__main__":
    main()
