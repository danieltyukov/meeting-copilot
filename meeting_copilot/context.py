"""Gather a compact, text description of a project directory.

The result is injected into the prompt sent to Claude when you ask for help, so
the answer can reference the actual project the meeting is about. The
output is deliberately size-bounded so it stays cheap to send and fast to read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Directories that never carry useful meeting context and would blow the
# budget if walked.
IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt", "target",
    "out", ".idea", ".vscode", ".cache", "coverage", ".gradle", "vendor",
    ".terraform", "bin", "obj", ".turbo", ".parcel-cache", "Pods",
}

# Files worth reading in full (within budget) because they describe the project.
KEY_FILES = [
    "README.md", "README.rst", "README.txt", "README",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "composer.json", "CMakeLists.txt", "Makefile",
    "ARCHITECTURE.md", "CONTRIBUTING.md", "docs/README.md",
    # Hand-written context (e.g. a presentation/exam prep) read in full so the
    # copilot can answer slide-specific and first-person Q&A.
    "SPEECH_NOTES.md", "NOTES.md", "CONTEXT.md",
]


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _git_info(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    remote = _run(["git", "remote", "get-url", "origin"], root)
    log = _run(["git", "log", "--oneline", "-8"], root)
    parts = []
    if branch:
        parts.append(f"branch: {branch}")
    if remote:
        parts.append(f"remote: {remote}")
    if log:
        parts.append("recent commits:\n" + log)
    return "\n".join(parts)


def _file_tree(root: Path, max_entries: int = 220, max_depth: int = 3) -> str:
    lines: list[str] = []
    count = 0

    def walk(path: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(
                path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except (PermissionError, OSError):
            return
        for entry in entries:
            if count >= max_entries:
                lines.append("    " * depth + "... (truncated)")
                return
            if entry.name.startswith(".") and entry.name not in {".env.example"}:
                continue
            if entry.is_dir() and entry.name in IGNORE_DIRS:
                continue
            indent = "    " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                count += 1
                walk(entry, depth + 1)
            else:
                lines.append(f"{indent}{entry.name}")
                count += 1

    walk(root, 0)
    return "\n".join(lines)


def _read_key_files(root: Path, per_file: int = 30000, total_budget: int = 50000) -> str:
    chunks: list[str] = []
    used = 0
    seen: set[Path] = set()
    for name in KEY_FILES:
        fpath = root / name
        if not fpath.is_file() or fpath in seen:
            continue
        seen.add(fpath)
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = text[:per_file]
        if len(text) > per_file:
            snippet += "\n... (truncated)"
        block = f"----- {name} -----\n{snippet}"
        if used + len(block) > total_budget:
            block = block[: max(0, total_budget - used)]
            if block:
                chunks.append(block)
            break
        chunks.append(block)
        used += len(block)
    return "\n\n".join(chunks)


def gather_context(root: Path | str, char_budget: int = 60000) -> str:
    """Return a compact textual description of ``root``.

    Includes the directory name/path, git status, a depth-limited file tree, and
    the contents of recognised project files, all clipped to ``char_budget``.
    """
    root = Path(root).expanduser().resolve()
    sections: list[str] = [f"PROJECT DIRECTORY: {root.name}\nFull path: {root}"]

    git = _git_info(root)
    if git:
        sections.append("GIT:\n" + git)

    tree = _file_tree(root)
    if tree:
        sections.append("FILE TREE:\n" + tree)

    files = _read_key_files(root)
    if files:
        sections.append("KEY FILES:\n" + files)

    blob = "\n\n".join(sections)
    if len(blob) > char_budget:
        blob = blob[:char_budget] + "\n... (context truncated)"
    return blob
