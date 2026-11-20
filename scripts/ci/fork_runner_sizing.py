"""Size a workflow's test parallelism for the hosted runner that replaces its upstream one.

Upstream sizes its test lanes for the runner each job names: ``HERMES_TEST_WORKERS: 32``
on ``blacksmith-32vcpu-ubuntu-2404`` across three slices. A 4-vCPU hosted runner runs
those 32 workers at 8 per core, so the suite's wall-clock guards and waits time out.
This keeps each core's load at upstream's: integer literals in ``HERMES_TEST_WORKERS``
scale by hosted/upstream vCPUs, and the job's test slices multiply by upstream//hosted
(a ``slice: [1, 2, 3]`` list with ``matrix.slice }}/3`` totals, or ``include`` entries
each carrying ``slice: i/n``). It runs on upstream's text, which still names the
Blacksmith runner, so the size comes from the label being replaced. Every edited line
carries upstream's value after ``MARK``, and an edited line is never edited again.
"""

from __future__ import annotations

import re

MARK = "fork: hosted runner; upstream"
# vCPUs of the standard GitHub-hosted runner each OS maps to, for a public repository.
HOSTED_VCPUS = {"ubuntu": 4, "windows": 4, "macos": 3}

_SIZED = re.compile(
    r"\bblacksmith-(?P<n>\d+)vcpu-(?P<os>ubuntu|macos|windows)-"
    r"|\b(?P<los>ubuntu|windows)-latest-(?P<ln>\d+)-(?:arm-)?core\b"
)
_COMMENT = re.compile(r"(^|\s)#")
_JOB = re.compile(r"^  [\w-]+:\s*(?:#.*)?$")
_TOP = re.compile(r"^\S")
_RUNS_ON = re.compile(r"^\s*(?:runs-on|runner):(.*)$")
_ENTRY = re.compile(r"^(?P<dash>\s*)- name:\s*(?P<name>.*?)\s*$")
_WORKERS = re.compile(r"^(\s*HERMES_TEST_WORKERS:\s*)(.*?)\s*$")
_INT = re.compile(r"(?<![\w.$])(['\"]?)(\d+)\1(?![\w.])")
_SLICE_LIST = re.compile(r"^(\s*slice:\s*)\[([\d,\s]+)\]\s*$")
_SLICE_ENTRY = re.compile(r"^(\s*)slice:\s*(\d+)/(\d+)\s*$")
_SLICE_TOTAL = re.compile(r"(matrix\.slice\s*}}\s*/)(\d+)\b")


def _code(line: str) -> str:
    return _COMMENT.split(line, maxsplit=1)[0]


def _size(text: str) -> tuple[int, int] | None:
    """(upstream vCPUs, hosted vCPUs) of the runner label in ``text`` that shrinks most."""
    sizes = [(int(m["n"] or m["ln"]), HOSTED_VCPUS[m["os"] or m["los"]])
             for m in _SIZED.finditer(text)]
    sizes = [s for s in sizes if s[0] > s[1]]
    return max(sizes, key=lambda s: s[0] / s[1]) if sizes else None


def _runner_size(lines: list[str]) -> tuple[int, int] | None:
    sizes = [s for ln in lines if (r := _RUNS_ON.match(_code(ln))) and (s := _size(r[1]))]
    return max(sizes, key=lambda s: s[0] / s[1]) if sizes else None


def _workers(line: str, size: tuple[int, int]) -> str:
    m = _WORKERS.match(line)
    if not m or _COMMENT.search(m[2]):
        return line
    upstream, hosted = size

    def per_core(i: re.Match) -> str:
        return f"{i[1]}{max(1, round(int(i[2]) * hosted / upstream))}{i[1]}"

    scaled = _INT.sub(per_core, m[2])
    return line if scaled == m[2] else f"{m[1]}{scaled}  # {MARK} {m[2]}"


def _entry_spans(job: list[str]) -> list[tuple[int, int]]:
    """[start, end) spans of the ``- name:`` entries of a matrix ``include`` list."""
    at = next((k for k, ln in enumerate(job) if ln.strip() == "include:"), None)
    if at is None:
        return []
    spans, start, dash, stop = [], None, None, len(job)
    for k in range(at + 1, len(job)):
        line = job[k]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = _ENTRY.match(line)
        if m and (dash is None or len(m["dash"]) == dash):
            if start is not None:
                spans.append((start, k))
            start, dash = k, len(m["dash"])
        elif dash is not None and indent <= dash:
            stop = k
            break
    if start is not None:
        spans.append((start, stop))
    # An entry ends at its last content line; blank and comment lines after it stay put.
    trimmed = []
    for s, e in spans:
        while e > s + 1 and (not job[e - 1].strip() or job[e - 1].lstrip().startswith("#")):
            e -= 1
        trimmed.append((s, e))
    return trimmed


def _split_entry(entry: list[str], factor: int) -> list[str]:
    """One ``include`` entry as ``factor`` entries, each a slice of the slice it covered."""
    head = _ENTRY.match(entry[0])
    assert head
    at = next((k for k, ln in enumerate(entry) if _SLICE_ENTRY.match(ln)), None)
    if at is None:
        index, total = 1, 1
        key = " " * (len(head["dash"]) + 2)
        entry = [*entry, f"{key}slice: 1/1"]
        at = len(entry) - 1
    else:
        s = _SLICE_ENTRY.match(entry[at])
        assert s
        key, index, total = s[1], int(s[2]), int(s[3])
    out = []
    for j in range((index - 1) * factor + 1, index * factor + 1):
        tag = f"{j}/{total * factor}"
        own = re.compile(rf"\b{index}/{total}\b")
        name = own.sub(tag, head["name"], count=1) if total > 1 and own.search(head["name"]) \
            else f"{head['name']} ({tag})"
        lines = [f"{head['dash']}- name: {name}", *entry[1:]]
        lines[at] = f"{key}slice: {tag}  # {MARK} {index}/{total}"
        out += lines
    return out


def _size_job(job: list[str]) -> list[str]:
    size = _runner_size(job)
    if size is None:
        return job
    factor = size[0] // size[1]
    out = list(job)
    sliced = any("matrix.slice" in _code(ln) for ln in job)
    # A slice list grows by the job's factor; its ``/N`` totals follow.
    for k, line in enumerate(out):
        s = _SLICE_LIST.match(line)
        if not (sliced and s and factor > 1):
            continue
        old = [int(x) for x in s[2].split(",") if x.strip()]
        if old != list(range(1, len(old) + 1)):
            continue
        new = len(old) * factor
        out[k] = f"{s[1]}[{', '.join(map(str, range(1, new + 1)))}]  # {MARK} [{s[2].strip()}]"
        out = [_SLICE_TOTAL.sub(lambda t: t[1] + str(new) if int(t[2]) == len(old) else t[0], ln)
               for ln in out]
    out = [_workers(ln, size) for ln in out]
    # ``include`` entries each split by the runner they name.
    if sliced:
        for start, end in reversed(_entry_spans(out)):
            entry_size = _runner_size(out[start:end])
            if entry_size and entry_size[0] // entry_size[1] > 1:
                out[start:end] = _split_entry(out[start:end], entry_size[0] // entry_size[1])
    return out


def size_jobs(text: str) -> str:
    """Scale every job's test parallelism to the hosted runner replacing its upstream one."""
    body = text.splitlines()
    if "jobs:" not in body:
        return text
    at = body.index("jobs:")
    end = next((k for k in range(at + 1, len(body)) if _TOP.match(body[k])
                and not body[k].startswith("#")), len(body))
    starts = [k for k in range(at + 1, end) if _JOB.match(body[k])]
    out = body[:at + 1]
    prev = at + 1
    for s, e in zip(starts, [*starts[1:], end]):
        out += body[prev:s] + _size_job(body[s:e])
        prev = e
    out += body[prev:]
    eol = "\r\n" if "\r\n" in text else "\n"
    return eol.join(out) + (eol if text.endswith(("\n", "\r")) else "")
