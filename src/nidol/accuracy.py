"""Accuracy test set (D14) and its manual runner (D15).

    uv run label <link>…                 resolve, show candidates, pick the right one(s)
    uv run label --inbox tests/accuracy/inbox.txt
    uv run accuracy                      run every case against the live upstreams

Cases live in tests/accuracy/cases.yaml. A case passes top-1 when candidates[0] is one of its
accepted videoIds, and top-3 when any of the first three is.
"""

import argparse
import asyncio
import logging
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nidol.client import make_client
from nidol.inputs import parse_input
from nidol.models import Canonical, NidolError, ResolveRequest, ResolveResponse
from nidol.resolve import resolve

DEFAULT_CASES = Path("tests/accuracy/cases.yaml")
HEADER = """\
# nidol accuracy test set (D14). Add cases with `uv run label <link>`; run with `uv run accuracy`.
# accept: every videoId that counts as correct (e.g. the official audio AND the official video).
"""
TARGET_TOP1 = 0.95

Resolver = Callable[[str], Awaitable[ResolveResponse]]


@dataclass
class Case:
    input: str
    accept: list[str]
    tags: list[str] = field(default_factory=list)
    notes: str | None = None


def load_cases(path: Path) -> list[Case]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or []
    return [Case(**item) for item in raw]


def save_cases(path: Path, cases: list[Case]) -> None:
    items = [
        {k: v for k, v in vars(c).items() if v not in (None, [])} | {"accept": c.accept}
        for c in cases
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        items, allow_unicode=True, sort_keys=False, width=100, default_flow_style=None
    )
    path.write_text(HEADER + "\n" + body)


def auto_tags(link: str, canonical: Canonical) -> list[str]:
    """Tags for slicing the report: platform plus the hard-case kinds the brief asks for."""
    parsed = parse_input(link)
    tags = [parsed.platform] if parsed else []
    title = canonical.title.casefold()
    for tag, words in {
        "remaster": ("remaster",),
        "live": ("live",),
        "edit": ("edit", "version", "mix"),
        "soundtrack": ("soundtrack", " from "),
    }.items():
        if any(w in f" {title} " for w in words):
            tags.append(tag)
    if len(canonical.artists) > 1 or "feat" in title:
        tags.append("feat")
    if any(c.isalpha() and not c.isascii() and ord(c) > 0x24F for c in canonical.title):
        tags.append("non-latin")
    if canonical.explicit:
        tags.append("explicit")
    return tags


# --- running ------------------------------------------------------------------------------


@dataclass
class Outcome:
    case: Case
    picked: list[str]
    error: str | None
    needs_confirmation: bool
    latency_s: float

    @property
    def top1(self) -> bool:
        return bool(self.picked) and self.picked[0] in self.case.accept

    @property
    def top3(self) -> bool:
        return any(v in self.case.accept for v in self.picked[:3])


async def run_cases(cases: list[Case], resolver: Resolver, concurrency: int = 1) -> list[Outcome]:
    sem = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> Outcome:
        async with sem:
            start = time.monotonic()
            try:
                res = await resolver(case.input)
                picked = [c.videoId for c in res.candidates]
                return Outcome(case, picked, None, res.needsConfirmation, time.monotonic() - start)
            except NidolError as e:
                return Outcome(case, [], e.body.error, True, time.monotonic() - start)

    return await asyncio.gather(*(one(c) for c in cases))


def _pct(n: int, d: int) -> str:
    return f"{n / d:6.1%}" if d else "     –"


def report(outcomes: list[Outcome]) -> str:
    n = len(outcomes)
    lines = [f"{n} cases"]
    if not n:
        return lines[0]
    top1 = sum(o.top1 for o in outcomes)
    top3 = sum(o.top3 for o in outcomes)
    confirm = sum(o.needs_confirmation for o in outcomes)
    errors = sum(o.error is not None for o in outcomes)
    latencies = sorted(o.latency_s for o in outcomes)
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    lines += [
        f"top-1 {_pct(top1, n)}  ({top1}/{n}, target {TARGET_TOP1:.0%})",
        f"top-3 {_pct(top3, n)}  ({top3}/{n})",
        f"needsConfirmation {_pct(confirm, n)}   errors {errors}",
        f"latency p50 {statistics.median(latencies):.2f}s  p95 {p95:.2f}s",
        "",
        f"{'tag':<14}{'cases':>6}{'top-1':>8}{'top-3':>8}",
    ]
    tags = sorted({t for o in outcomes for t in o.case.tags})
    for tag in tags:
        group = [o for o in outcomes if tag in o.case.tags]
        lines.append(
            f"{tag:<14}{len(group):>6}{_pct(sum(o.top1 for o in group), len(group)):>8}"
            f"{_pct(sum(o.top3 for o in group), len(group)):>8}"
        )
    misses = [o for o in outcomes if not o.top1]
    if misses:
        lines += ["", "top-1 misses:"]
        for o in misses:
            got = o.error or (o.picked[0] if o.picked else "nothing")
            note = f"  # {o.case.notes}" if o.case.notes else ""
            lines.append(f"  {o.case.input}\n    got {got}, want {' | '.join(o.case.accept)}{note}")
    return "\n".join(lines)


def _live_resolver(country: str | None) -> tuple[Resolver, Callable[[], Awaitable[None]]]:
    client = make_client()

    async def resolver(link: str) -> ResolveResponse:
        return await resolve(ResolveRequest(input=link, country=country), client)

    return resolver, client.aclose


def accuracy_main() -> None:
    ap = argparse.ArgumentParser(prog="accuracy", description="Run the accuracy test set live.")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--tag", action="append", help="only cases with this tag (repeatable)")
    ap.add_argument("--country", default="CL")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--min-top1", type=float, default=TARGET_TOP1, help="exit 1 below this")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    cases = load_cases(args.cases)
    if args.tag:
        cases = [c for c in cases if set(args.tag) & set(c.tags)]
    if not cases:
        sys.exit(f"no cases in {args.cases}; add some with `uv run label <link>`")

    async def go() -> list[Outcome]:
        resolver, close = _live_resolver(args.country)
        try:
            return await run_cases(cases, resolver, args.concurrency)
        finally:
            await close()

    outcomes = asyncio.run(go())
    print(report(outcomes))
    if sum(o.top1 for o in outcomes) / len(outcomes) < args.min_top1:
        sys.exit(1)


# --- labelling ----------------------------------------------------------------------------

HELP = (
    "pick: numbers (e.g. 1 or 1,2) · o <videoId>[,<videoId>] for one not listed · "
    "s skip · q quit · append '# note' to any pick"
)


def parse_pick(answer: str, candidates: list[str]) -> list[str] | str | None:
    """'1,2 # note' → accepted videoIds. Returns 'skip' / 'quit', or None if unreadable."""
    choice = answer.split("#", 1)[0].strip()
    if choice in ("s", "q"):
        return {"s": "skip", "q": "quit"}[choice]
    if choice.startswith("o "):
        ids = [v.strip() for v in choice[2:].split(",") if v.strip()]
        return ids if all(len(v) == 11 for v in ids) and ids else None
    try:
        picks = [int(p) for p in choice.split(",")]
    except ValueError:
        return None
    if not picks or any(not 1 <= p <= len(candidates) for p in picks):
        return None
    return [candidates[p - 1] for p in picks]


def _show(link: str, res: ResolveResponse) -> None:
    c = res.canonical
    secs = f"{c.durationMs // 1000}s" if c.durationMs else "?"
    print(f"\n{link}\n  {', '.join(c.artists)} — {c.title} ({secs})")
    for i, cand in enumerate(res.candidates, 1):
        dur = f"{cand.durationMs // 1000}s" if cand.durationMs else "?"
        print(
            f"  {i}. {cand.confidence:.2f} {cand.kind:<14} {cand.title} | {cand.channel} | {dur}"
            f"\n     https://music.youtube.com/watch?v={cand.videoId}"
        )


def _inbox_links(path: Path) -> list[str]:
    return [
        line.split("#", 1)[0].strip()
        for line in path.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]


def label_main() -> None:
    ap = argparse.ArgumentParser(prog="label", description="Add cases to the accuracy test set.")
    ap.add_argument("links", nargs="*")
    ap.add_argument("--inbox", type=Path, help="file with one link per line ('#' comments ok)")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--tag", action="append", default=[], help="extra tag for these cases")
    ap.add_argument("--country", default="CL")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    links = args.links + (_inbox_links(args.inbox) if args.inbox else [])
    cases = load_cases(args.cases)
    done = {c.input for c in cases}
    todo = [link for link in dict.fromkeys(links) if link not in done]
    if not todo:
        sys.exit("nothing to label (no links, or all already in the test set)")
    print(f"{len(todo)} to label ({len(done)} already in {args.cases}). {HELP}")

    async def go() -> None:
        resolver, close = _live_resolver(args.country)
        try:
            for n, link in enumerate(todo, 1):
                try:
                    res = await resolver(link)
                except NidolError as e:
                    print(f"\n{link}\n  error: {e.body.error} (skipped)")
                    continue
                _show(link, res)
                ids = [c.videoId for c in res.candidates]
                while True:
                    answer = input(f"[{n}/{len(todo)}] > ").strip()
                    pick = parse_pick(answer, ids)
                    if pick is not None:
                        break
                    print(HELP)
                if pick == "quit":
                    return
                if pick == "skip":
                    continue
                note = answer.split("#", 1)[1].strip() if "#" in answer else None
                cases.append(Case(link, pick, auto_tags(link, res.canonical) + args.tag, note))
                save_cases(args.cases, cases)  # after every pick, so quitting loses nothing
        finally:
            await close()

    try:
        asyncio.run(go())
    except KeyboardInterrupt, EOFError:
        print()
    print(f"{len(cases)} cases in {args.cases}")
