#!/usr/bin/env python3
"""Render a private, self-contained HTML chart of fleet quota history."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
import re
from pathlib import Path

if __package__:
    from .fleet_monitor_probe import number, timestamp
else:
    from fleet_monitor_probe import number, timestamp

WIDTH, HEIGHT = 790, 236
LEFT, RIGHT, TOP, BOTTOM = 47, 18, 14, 36
COLORS = ("#2166ac", "#ad4522", "#772dae", "#087666", "#9e2c68", "#6c591d")


def history_lines(history, start, end):
    if not history.is_dir():
        with history.open(encoding="utf-8") as source:
            yield from source
        return

    month = start.astimezone(timezone.utc).date().replace(day=1)
    last = end.astimezone(timezone.utc).date().replace(day=1)
    while month <= last:
        try:
            source = (history / f"quota-history-{month:%Y-%m}.jsonl").open(encoding="utf-8")
        except FileNotFoundError:
            pass
        else:
            with source:
                yield from source
        month = (month + timedelta(days=32)).replace(day=1)


def read_series(history, start, end, account=None, window=None):
    accounts = defaultdict(lambda: defaultdict(list))
    labels = {}
    for line in history_lines(history, start, end):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
        except ValueError:
            continue
        at = timestamp(row.get("captured_at"))
        provider, label, account_id = (row.get(key) for key in ("provider", "label", "account_id"))
        if at is None or not start <= at <= end or not all(isinstance(v, str) for v in (provider, label, account_id)):
            continue
        if account and account.casefold() not in (provider + " " + label + " " + account_id).casefold():
            continue
        key = provider, account_id
        if at >= labels.get(key, (start, ""))[0]:
            labels[key] = at, label
        for metric in row.get("windows") or []:
            if not isinstance(metric, dict):
                continue
            name, used = metric.get("window"), number(metric.get("used_percent"))
            if (isinstance(name, str) and (window is None or window == name)
                    and used is not None and 0 <= used <= 100):
                accounts[key][name].append((at, used))
    return [(provider, labels[provider, tag][1], series)
            for (provider, tag), series in sorted(accounts.items())]


def chart_svg(series, start, end):
    span = (end - start).total_seconds()
    plot_width, plot_height = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def x(at):
        return LEFT + plot_width * (at - start).total_seconds() / span

    def y(used):
        return TOP + plot_height * (100 - used) / 100

    def point(at, used):
        return f"{x(at):.2f} {y(used):.2f}"

    parts = [f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Quota percent used over time">']
    for used in (0, 25, 50, 75, 90, 100):
        css = ' class="threshold"' if used == 90 else ''
        parts.append(f'<line{css} x1="{LEFT}" x2="{WIDTH-RIGHT}" y1="{y(used):.2f}" y2="{y(used):.2f}"/>')
        if used != 90:
            parts.append(f'<text x="{LEFT-7}" y="{y(used)+4:.2f}" text-anchor="end">{used}%</text>')
    for index in range(4):
        date = start + (end - start) * index / 3
        date_label = date.strftime("%b %d %H:%M")
        align = ("start", "middle", "middle", "end")[index]
        parts.append(f'<text x="{x(date):.2f}" y="{HEIGHT-12}" text-anchor="{align}">{date_label}</text>')

    legend = []
    for index, (name, readings) in enumerate(sorted(series.items())):
        color = COLORS[index % len(COLORS)]
        path, previous = [], None
        for at, used in sorted(dict(readings).items()):
            # Draw gaps when a whole scheduled sample was missed.
            if previous is None or (at - previous).total_seconds() > 450:
                path.append("M" + point(at, used) + "h0.01")
            else:
                path.append("L" + point(at, used))
            previous = at
        parts.append(f'<path d="{" ".join(path)}" stroke="{color}"/>')
        at, used = max(readings)
        tooltip = escape(f"{name}: {used:g}% on {at:%b %d %H:%M} UTC")
        parts.append(f'<circle cx="{x(at):.2f}" cy="{y(used):.2f}" r="4" fill="{color}"><title>{tooltip}</title></circle>')
        legend.append(f'<li><span style="color:{color}" aria-hidden="true">●</span> {escape(name)} · {used:g}%</li>')
    parts.append('</svg><ul class="legend">' + "".join(legend) + "</ul>")
    return "\n".join(parts)


def render(history, start, end, account=None, window=None):
    cards = []
    for provider, label, series in read_series(history, start, end, account, window):
        cards.append(f'<section><h2>{escape(provider)} <span>{escape(label)}</span></h2>'
                     + chart_svg(series, start, end) + "</section>")
    content = "\n".join(cards) or "<p>No quota readings in the selected time range.</p>"
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CLIProxy quota history</title>
<style>
  * {{ box-sizing: border-box }}
  body {{ max-width: 57rem; margin: auto; padding: 1.5rem; color: #203147; background: #f3f6fa;
          font: 16px/1.5 system-ui, sans-serif }}
  h1 {{ margin: 0; font-size: 1.8rem }}
  h2 {{ margin: 0 0 .7rem; font-size: 1.15rem }}
  h2 span {{ font-weight: normal }}
  section {{ background: white; border: 1px solid #c5d0dc; border-radius: .5rem;
             padding: 1rem; margin: 1rem 0 }}
  svg {{ width: 100%; height: auto }}
  svg text {{ fill: #465a72; font-size: 13px }}
  svg line {{ stroke: #dce3eb }}
  svg .threshold {{ stroke: #a26533; stroke-dasharray: 4 4 }}
  svg path {{ fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round }}
  .legend {{ display: flex; flex-wrap: wrap; gap: .4rem 1.2rem; list-style: none; margin: 0; padding: 0 }}
</style>
<body>
<h1>CLIProxy quota history</h1>
<p>{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC · dashed line at 90% used</p>
{content}
</body>
</html>
"""


def main(argv=None):
    root = Path(os.environ.get("CLIPROXY_ROOT", "~/cliproxy")).expanduser() / "ops/runtime/fleet"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=root,
                        help="directory of monthly history files or an explicit JSONL file")
    parser.add_argument("--output", type=Path, default=root / "quota-chart.html")
    parser.add_argument("--days", type=int, default=7, help="days of history to chart")
    parser.add_argument("--account", help="filter by provider, account label or account ID")
    parser.add_argument("--window", help="filter by quota window name")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3660:
        parser.error("--days must be between 1 and 3660")
    history, output = args.history.resolve(), args.output.resolve()
    if (history == output or (history == output.parent
                              and re.fullmatch(r"quota-history-\d{4}-\d{2}\.jsonl", output.name))):
        parser.error("--output must be different from the history files")
    now = datetime.now(timezone.utc)
    try:
        html = render(args.history, now - timedelta(days=args.days), now, args.account, args.window)
        fd = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(fd, 0o600)
            out.write(html)
    except OSError:
        parser.exit(1, "Could not read quota history or write the chart.\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
