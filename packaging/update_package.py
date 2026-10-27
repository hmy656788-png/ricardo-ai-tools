#!/usr/bin/env python3
"""Update a sanitized baseline archive with reviewed UI files only.

Never copy a whole live service tree: it may contain accounts and credentials.
Build frontend assets from the sanitized staging sources, not the live assets.
"""
import argparse
import ast
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

WORKSPACE = Path(__file__).resolve().parents[1]
MAIN = "gpt-at-refresh-lite"
REGISTER = "turb-gpt-free-register"
CHANGE = "change-2fa-password-gpt-auto"
OVERLAYS = {
    MAIN: [
        "README.md", "webui/frontend/package.json", "webui/frontend/package-lock.json",
        "webui/frontend/src/main.js",
        "webui/frontend/index.html", "webui/frontend/public/ricardo-ai-logo-gold.png",
        "webui/frontend/src/layouts/AdminLayout.vue", "webui/frontend/src/router/index.js",
        "webui/frontend/src/views/Change2FA.vue", "webui/frontend/src/views/TurbRegister.vue",
        "webui/frontend/src/views/TwoFactorCode.vue", "webui/frontend/src/utils/totp.js",
        "webui/frontend/tests/totp.test.mjs",
        # Reviewed backend/UI integration updates kept in the live workspace.
        "webui/app.py", "webui/backup.py", "webui/registrar.py",
    ],
    REGISTER: [
        "webui/templates/index.html", "webui/templates/login.html",
        "webui/static/embedded.js", "webui/static/ricardo-embedded.css",
        "tests/embedded-ui.test.mjs",
    ],
    CHANGE: [
        "static/index.html", "static/app.js", "static/embedded.js",
        "static/ricardo-embedded.css", "tests/embedded-ui.test.mjs",
    ],
}
BLOCKED_PARTS = {
    ".git", ".venv", "node_modules", "__pycache__", ".DS_Store", "logs",
    "data", "runtime", "exports", "credentials", "注册日志",
}


def sanitized_registered_view(source, baseline):
    """Keep current UI changes while retaining the reviewed import example."""
    pattern = re.compile(r'(<el-input\b[^>]*\bv-model="importText"[^>]*\bplaceholder=")([^"]*)(")')
    source_matches = list(pattern.finditer(source))
    baseline_matches = list(pattern.finditer(baseline))
    if len(source_matches) != 1 or len(baseline_matches) != 1:
        raise ValueError("Expected exactly one import placeholder; refusing an unsanitized release.")
    safe_example = baseline_matches[0].group(2)
    return pattern.sub(lambda match: match.group(1) + safe_example + match.group(3), source)


def checked_path(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    if BLOCKED_PARTS.intersection(p.parts):
        raise ValueError(f"Runtime data in archive: {name}")
    if p.name == ".env" or re.search(r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$|\.(log|pyc|jsonl|har)$", p.name):
        raise ValueError(f"Private/runtime file in archive: {name}")
    if p.name.startswith(".env") and p.name != ".env.example":
        raise ValueError(f"Non-example environment file: {name}")
    return p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--change-service", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; choose a new versioned filename.")
    roots = {
        MAIN: WORKSPACE / MAIN,
        REGISTER: WORKSPACE / ".audit-turb-gpt-free-register/repo",
        CHANGE: args.change_service.resolve(),
    }
    # The main panel's router lazy-loads several view/component files. Keep
    # the explicit reviewed overlays above, but also include every current
    # frontend source file so a newly added route cannot produce a package
    # that fails only at the staged Vite build step.
    main_src = roots[MAIN] / "webui/frontend/src"
    main_overlay = set(OVERLAYS[MAIN])
    main_overlay.update(
        p.relative_to(roots[MAIN]).as_posix()
        for p in main_src.rglob("*")
        if p.is_file()
    )
    main_tests = roots[MAIN] / "webui/frontend/tests"
    main_overlay.update(
        p.relative_to(roots[MAIN]).as_posix()
        for p in main_tests.rglob("*")
        if p.is_file()
    )
    overlays = {**OVERLAYS, MAIN: sorted(main_overlay)}
    stage = Path(tempfile.mkdtemp(prefix="ricardo-share-")) / "Ricardo-AI-Tools"
    stage.mkdir()
    with ZipFile(args.baseline) as old:
        for entry in old.infolist():
            if entry.is_dir():
                continue
            name = entry.filename
            if not entry.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeError, LookupError):
                    pass
            rel = checked_path("/".join(name.split("/")[1:]))
            # A previous release's manifest must be regenerated, never include itself.
            if str(rel) == "MANIFEST.sha256":
                continue
            if str(rel).startswith(f"{MAIN}/webui/static/"):
                continue
            dest = stage / rel
            if dest.exists():
                raise ValueError(f"Duplicate path: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old.read(entry))
    for service, files in overlays.items():
        for rel in files:
            source = roots[service] / rel
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Missing/non-regular source: {source}")
            dest = stage / service / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
    registered_rel = "webui/frontend/src/views/Registered.vue"
    registered_dest = stage / MAIN / registered_rel
    registered_dest.write_text(sanitized_registered_view(
        (roots[MAIN] / registered_rel).read_text(encoding="utf-8"),
        registered_dest.read_text(encoding="utf-8"),
    ), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("README.md"), stage / "README.md")
    env = stage / REGISTER / ".env.example"
    env_text = env.read_text(encoding="utf-8")
    if not re.search(r"^WEBUI_NO_AUTH=", env_text, re.M):
        env.write_text(env_text + '\n# Ricardo local-only mode; never expose this service publicly.\nWEBUI_NO_AUTH="true"\n', encoding="utf-8")

    frontend = stage / MAIN / "webui/frontend"
    # Reuse installed build dependencies without bundling or copying them.
    deps = frontend / "node_modules"
    deps.symlink_to(roots[MAIN] / "webui/frontend/node_modules", target_is_directory=True)
    try:
        subprocess.run(["npm", "test"], cwd=frontend, check=True)
        subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    finally:
        deps.unlink()
    for service in (REGISTER, CHANGE):
        subprocess.run(["node", "--test", "tests/embedded-ui.test.mjs"], cwd=stage / service, check=True)

    layout = (frontend / "src/layouts/AdminLayout.vue").read_text()
    assert "['账号', '配置', '安全工具', '自动化工具']" in layout
    register_html = (stage / REGISTER / "webui/templates/index.html").read_text()
    assert "TG 交流群" not in register_html and "t.me/picloudyouxiang" not in register_html
    assert 'action="/logout"' in register_html
    assert "http://localhost:5050" in (frontend / "src/views/TurbRegister.vue").read_text()
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    syntax_count = 0
    for file in files:
        checked_path(file.relative_to(stage).as_posix())
        if file.is_symlink():
            raise ValueError(f"Unexpected symlink: {file}")
        if file.suffix == ".py":
            ast.parse(file.read_bytes(), filename=str(file.relative_to(stage)))
            syntax_count += 1
    manifest = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(stage).as_posix()}\n" for p in files)
    (stage / "MANIFEST.sha256").write_text(manifest, encoding="utf-8")
    files.append(stage / "MANIFEST.sha256")
    with ZipFile(args.output, "x", compression=ZIP_DEFLATED, compresslevel=9) as new:
        for file in sorted(files):
            new.write(file, arcname=f"Ricardo-AI-Tools/{file.relative_to(stage).as_posix()}")
    with ZipFile(args.output) as new:
        assert new.testzip() is None
        assert len(new.namelist()) == len(files)
    print(f"Stage: {stage}")
    print(f"Python syntax: {syntax_count} files passed")
    print(f"Archive: {args.output.resolve()} ({args.output.stat().st_size} bytes; {len(files)} files)")
    print(f"SHA256: {hashlib.sha256(args.output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
