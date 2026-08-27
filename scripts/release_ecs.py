from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = "codex-aliyun"
DEFAULT_REGION = "cn-hangzhou"
DEFAULT_INSTANCE = "i-bp1fsztec7chovv51eyw"
DEFAULT_CANONICAL_URL = "https://trading.longchangwudao.cn"
REQUIRED_WHEEL_RESOURCES = (
    "master_stock_selector/web/static/app.css",
    "master_stock_selector/web/templates/base.html",
    "master_stock_selector/web/templates/ashare/owner_activity.html",
    "master_stock_selector/web/templates/ashare/shared_trading_plan.html",
    "master_stock_selector/web/static/vendor/lightweight-charts-5.1.0.js",
    "master_stock_selector/web/static/vendor/lucide-chevron-left.svg",
    "master_stock_selector/web/templates/reading/archive.html",
    "master_stock_selector/web/templates/reading/article.html",
    "master_stock_selector/web/content/reading/adam-grimes-trading-templates/index.md",
    "master_stock_selector/web/content/reading/adam-grimes-trading-templates/images/00103.jpg",
    "master_stock_selector/web/content/reading/mark-minervini-breakout-trading/index.md",
    "master_stock_selector/web/content/reading/mark-minervini-breakout-trading/"
    "images/workbook-figures/mtp-v2-p453-fwlt-breakout.jpeg",
)
FORBIDDEN_ARCHIVE_PATTERNS = (
    re.compile(r"(^|/)(data|logs|artifacts|backups)/"),
    re.compile(r"(^|/)\.env($|/)"),
    re.compile(r"\.sqlite3($|-)|\.sqlite3-(wal|shm)$"),
    re.compile(r"(^|/).*\.db$"),
)


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseConfig:
    apply: bool
    migrate_users: bool
    confirm_commit: str
    profile: str
    region: str
    instance_id: str
    canonical_url: str
    smoke_paths: tuple[str, ...]
    skip_tests: bool
    allow_dirty_worktree: bool
    upload_workers: int
    chunk_bytes: int
    command_timeout: int


@dataclass
class ReleaseContext:
    config: ReleaseConfig
    commit: str
    short_commit: str
    release_id: str
    archive: Path
    archive_sha256: str
    css_sha256: str
    candidate_dir: str
    staging_dir: str
    unit_name: str
    parts: list[Path] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def parse_args(argv: Sequence[str] | None = None) -> ReleaseConfig:
    parser = argparse.ArgumentParser(
        description="构建、演练或发布已提交的 MasterStock 版本到 ECS",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rehearse",
        action="store_true",
        help="只创建、验收并清理远端候选（默认）",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="备份用户库并原子切换正式代码",
    )
    parser.add_argument(
        "--migrate-users",
        action="store_true",
        help="仅与 --apply 同用：显式迁移用户库后再原子切换，并在失败时恢复备份",
    )
    parser.add_argument(
        "--confirm-commit",
        default="",
        help="--apply 时必须等于当前完整或短提交哈希",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--instance-id", default=DEFAULT_INSTANCE)
    parser.add_argument("--canonical-url", default=DEFAULT_CANONICAL_URL)
    parser.add_argument(
        "--smoke-path",
        action="append",
        default=[],
        help="登录候选后必须返回 200 的额外路径，可重复指定",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="仅允许演练时跳过本地 Ruff、Mypy 和 Pytest",
    )
    parser.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="仅允许演练时忽略已跟踪修改；归档仍严格来自 HEAD",
    )
    parser.add_argument("--upload-workers", type=int, default=8)
    parser.add_argument("--chunk-bytes", type=int, default=24_000)
    parser.add_argument("--command-timeout", type=int, default=600)
    args = parser.parse_args(argv)

    if args.apply and not args.confirm_commit:
        parser.error("--apply 必须同时提供 --confirm-commit")
    if args.migrate_users and not args.apply:
        parser.error("--migrate-users 只能与 --apply 同时使用")
    if args.apply and args.skip_tests:
        parser.error("--apply 不允许 --skip-tests")
    if args.apply and args.allow_dirty_worktree:
        parser.error("--apply 不允许 --allow-dirty-worktree")
    if not 1 <= args.upload_workers <= 16:
        parser.error("--upload-workers 必须在 1 到 16 之间")
    if not 1_000 <= args.chunk_bytes <= 24_000:
        parser.error("--chunk-bytes 必须在 1000 到 24000 之间")
    if args.command_timeout < 60:
        parser.error("--command-timeout 不得小于 60 秒")

    smoke_paths = tuple(dict.fromkeys(("/a/daily", *args.smoke_path)))
    for path in smoke_paths:
        if not path.startswith("/") or any(char.isspace() for char in path):
            parser.error(f"无效 --smoke-path：{path!r}")

    return ReleaseConfig(
        apply=bool(args.apply),
        migrate_users=bool(args.migrate_users),
        confirm_commit=str(args.confirm_commit),
        profile=str(args.profile),
        region=str(args.region),
        instance_id=str(args.instance_id),
        canonical_url=str(args.canonical_url).rstrip("/"),
        smoke_paths=smoke_paths,
        skip_tests=bool(args.skip_tests),
        allow_dirty_worktree=bool(args.allow_dirty_worktree),
        upload_workers=int(args.upload_workers),
        chunk_bytes=int(args.chunk_bytes),
        command_timeout=int(args.command_timeout),
    )


def shell_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def run_process(
    args: Sequence[str | Path],
    *,
    cwd: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
    capture: bool = False,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in args]
    if label:
        print(f"[{label}]", flush=True)
    print("$ " + shlex.join(_redact_content(command)), flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if result.returncode != 0:
        output = "\n".join(item for item in (result.stdout, result.stderr) if item).strip()
        raise ReleaseError(f"命令失败（{result.returncode}）：{shlex.join(command)}\n{output}")
    return result


def _redact_content(command: list[str]) -> list[str]:
    redacted = command.copy()
    for option in ("--Content", "--CommandContent"):
        if option not in redacted:
            continue
        index = redacted.index(option)
        if index + 1 < len(redacted):
            redacted[index + 1] = "<base64-content>"
    return redacted


def output_of(args: Sequence[str | Path], *, cwd: Path = PROJECT_ROOT) -> str:
    return run_process(args, cwd=cwd, capture=True).stdout.strip()


def timed(context: ReleaseContext | None, name: str):
    class Timer:
        def __enter__(self) -> None:
            self.started = time.monotonic()
            print(f"\n== {name} ==", flush=True)

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            elapsed = time.monotonic() - self.started
            if context is not None:
                context.timings[name] = round(elapsed, 2)
            print(f"== {name}: {elapsed:.1f}s ==", flush=True)

    return Timer()


def validate_archive_names(names: Sequence[str]) -> None:
    forbidden = [
        name
        for name in names
        if any(pattern.search(name) for pattern in FORBIDDEN_ARCHIVE_PATTERNS)
    ]
    if forbidden:
        raise ReleaseError("发布归档包含禁入路径：\n" + "\n".join(forbidden[:30]))


def verify_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as bundle:
        names = set(bundle.namelist())
        missing = [path for path in REQUIRED_WHEEL_RESOURCES if path not in names]
        if missing:
            raise ReleaseError("wheel 缺少资源：" + ", ".join(missing))


def installed_import_check(install_target: Path) -> str:
    return (
        "from pathlib import Path; import master_stock_selector; "
        "p=Path(master_stock_selector.__file__).resolve(); print(p); "
        f"target=Path({str(install_target)!r}).resolve(); "
        "assert p.is_relative_to(target)"
    )


def build_local_candidate(config: ReleaseConfig, temp_root: Path) -> ReleaseContext:
    for executable in ("git", "uv", "aliyun", "curl"):
        if shutil.which(executable) is None:
            raise ReleaseError(f"缺少命令：{executable}")
    venv_python = PROJECT_ROOT / ".venv/bin/python"
    if Path(sys.executable).resolve() != venv_python.resolve():
        raise ReleaseError(f"必须通过 scripts/release_ecs.sh 使用项目虚拟环境：{sys.executable}")

    tracked_status = output_of(["git", "status", "--porcelain", "--untracked-files=no"])
    if tracked_status and not config.allow_dirty_worktree:
        raise ReleaseError("工作区有已跟踪修改；发布只接受干净的已提交 HEAD")
    commit = output_of(["git", "rev-parse", "HEAD"])
    short_commit = commit[:7]
    if config.apply and config.confirm_commit not in (commit, short_commit):
        raise ReleaseError(
            f"--confirm-commit 与 HEAD 不一致：HEAD={commit} confirm={config.confirm_commit}"
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ").lower()
    release_id = f"{short_commit}-{timestamp}"
    archive = temp_root / f"masterstock-{release_id}.tar.gz"
    run_process(["git", "archive", "--format=tar.gz", f"--output={archive}", "HEAD"])
    with tarfile.open(archive, "r:gz") as bundle:
        validate_archive_names(bundle.getnames())
        bundle.extractall(temp_root / "source", filter="data")

    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = temp_root / "source"
    runtime_lock = source / "requirements-runtime-lock.txt"
    if not runtime_lock.is_file():
        raise ReleaseError("已提交 HEAD 缺少 requirements-runtime-lock.txt")

    wheel_dir = temp_root / "wheel"
    wheel_dir.mkdir()
    build_env = os.environ.copy()
    build_env["UV_CACHE_DIR"] = str(temp_root / "uv-cache")
    run_process(
        ["uv", "build", "--wheel", "--no-build-isolation", "--out-dir", wheel_dir],
        cwd=source,
        env=build_env,
        capture=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError(f"预期一个 wheel，实际得到 {len(wheels)} 个")
    verify_wheel(wheels[0])

    install_target = temp_root / "wheel-install"
    run_process(
        ["uv", "pip", "install", "--target", install_target, "--no-deps", wheels[0]],
        env=build_env,
        capture=True,
    )
    import_env = os.environ.copy()
    import_env["PYTHONPATH"] = str(install_target)
    run_process(
        [venv_python, "-c", installed_import_check(install_target)],
        env=import_env,
        capture=True,
    )

    css_path = install_target / "master_stock_selector/web/static/app.css"
    css_sha256 = hashlib.sha256(css_path.read_bytes()).hexdigest()
    mode = "apply" if config.apply else "rehearsal"
    candidate_dir = (
        f"/opt/masterstock.next-{short_commit}"
        if config.apply
        else f"/opt/masterstock.rehearsal-{release_id}"
    )
    staging_dir = f"/tmp/masterstock-release-{release_id}"
    unit_name = f"masterstock-candidate-{release_id}"
    context = ReleaseContext(
        config=config,
        commit=commit,
        short_commit=short_commit,
        release_id=release_id,
        archive=archive,
        archive_sha256=archive_sha256,
        css_sha256=css_sha256,
        candidate_dir=candidate_dir,
        staging_dir=staging_dir,
        unit_name=unit_name,
    )
    print(
        json.dumps(
            {
                "mode": mode,
                "commit": commit,
                "archive_sha256": archive_sha256,
                "css_sha256": css_sha256,
                "candidate": candidate_dir,
                "staging": staging_dir,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return context


def run_local_tests(config: ReleaseConfig) -> None:
    if config.skip_tests:
        print("跳过本地测试（仅演练允许）", flush=True)
        return
    run_process([PROJECT_ROOT / ".venv/bin/ruff", "check", "src", "tests", "scripts/release_ecs.py"])
    run_process([PROJECT_ROOT / ".venv/bin/mypy", "src"])
    test_env = os.environ.copy()
    test_env.pop("MASTERSTOCK_SECURE_COOKIES", None)
    run_process([PROJECT_ROOT / ".venv/bin/pytest", "-q"], env=test_env)
    run_process(["git", "diff", "--check"])


def aliyun_prefix(config: ReleaseConfig) -> list[str]:
    return ["aliyun", "--profile", config.profile, "ecs"]


def run_aliyun_json(config: ReleaseConfig, args: Sequence[str], *, label: str) -> dict[str, Any]:
    result = run_process(
        [*aliyun_prefix(config), *args],
        capture=True,
        label=label,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError(f"阿里云 CLI 未返回 JSON：{result.stdout}") from error


def run_cloud_script(
    config: ReleaseConfig,
    script: str,
    *,
    label: str,
    timeout: int | None = None,
) -> str:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    created = run_aliyun_json(
        config,
        [
            "RunCommand",
            "--RegionId",
            config.region,
            "--InstanceId.1",
            config.instance_id,
            "--Type",
            "RunShellScript",
            "--ContentEncoding",
            "Base64",
            "--CommandContent",
            encoded,
            "--Timeout",
            str(timeout or config.command_timeout),
        ],
        label=label,
    )
    invoke_id = str(created["InvokeId"])
    deadline = time.monotonic() + (timeout or config.command_timeout) + 30
    while time.monotonic() < deadline:
        result = run_aliyun_json(
            config,
            [
                "DescribeInvocationResults",
                "--RegionId",
                config.region,
                "--InvokeId",
                invoke_id,
            ],
            label=f"{label} · poll",
        )
        item = result["Invocation"]["InvocationResults"]["InvocationResult"][0]
        status = str(item["InvokeRecordStatus"])
        if status != "Finished":
            time.sleep(2)
            continue
        output = base64.b64decode(str(item.get("Output", ""))).decode("utf-8", errors="replace")
        if int(item.get("ExitCode", -1)) != 0 or item.get("InvocationStatus") != "Success":
            raise ReleaseError(
                f"{label} 失败：status={item.get('InvocationStatus')} "
                f"exit={item.get('ExitCode')}\n{output}"
            )
        if output:
            print(output.rstrip(), flush=True)
        return output
    raise ReleaseError(f"{label} 超时：{invoke_id}")


def baseline_script(context: ReleaseContext) -> str:
    candidate = shell_quote(context.candidate_dir)
    staging = shell_quote(context.staging_dir)
    apply_guard = ""
    if context.config.apply:
        apply_guard = (
            f"test ! -e {shell_quote(f'/opt/masterstock.before-{context.short_commit}')}\n"
            f"test ! -e {shell_quote(f'/opt/masterstock.failed-{context.short_commit}')}\n"
        )
    return f"""set -eu
test -d /opt/masterstock
test -f /opt/masterstock/.deployed-commit
test ! -e {candidate}
test ! -e {staging}
{apply_guard}for unit in masterstock-web.service masterstock-daily.timer masterstock-backup.timer nginx.service certbot.timer; do
  test "$(systemctl is-active "$unit")" = active
done
curl -fsS http://127.0.0.1:8000/healthz | grep -q '"status":"ok"'
sudo -u masterstock env MASTERSTOCK_USER_DATABASE=/var/lib/masterstock/users.sqlite3 /opt/masterstock/scripts/manage_users.sh schema-check >/dev/null
if ss -ltn | grep -q ':18000 '; then exit 31; fi
echo "LIVE_COMMIT=$(cat /opt/masterstock/.deployed-commit)"
for db in /var/lib/masterstock/market.sqlite3 /var/lib/masterstock/master_watchlist.sqlite3 /var/lib/masterstock/users.sqlite3; do
  stat -c 'DB|%n|%s|%Y|%i|%d' "$db"
done
echo BASELINE_OK
"""


def parse_db_fingerprints(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("DB|"):
            continue
        _, path, *fields = line.split("|")
        result[path] = "|".join(fields)
    expected = {
        "/var/lib/masterstock/market.sqlite3",
        "/var/lib/masterstock/master_watchlist.sqlite3",
        "/var/lib/masterstock/users.sqlite3",
    }
    if set(result) != expected:
        raise ReleaseError(f"未获得完整数据库指纹：{result}")
    return result


def parse_live_commit(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("LIVE_COMMIT="):
            return line.split("=", 1)[1].strip()
    raise ReleaseError("ECS 基线未返回当前部署提交")


def verify_rehearsal_fingerprints(
    before: dict[str, str],
    after: dict[str, str],
) -> None:
    for path in (
        "/var/lib/masterstock/market.sqlite3",
        "/var/lib/masterstock/master_watchlist.sqlite3",
    ):
        if after[path] != before[path]:
            raise ReleaseError(
                f"演练期间线上公共数据库指纹发生变化：{path} "
                f"before={before[path]} after={after[path]}"
            )
    users_path = "/var/lib/masterstock/users.sqlite3"
    before_users = before[users_path].split("|")
    after_users = after[users_path].split("|")
    if after_users[2:] != before_users[2:]:
        raise ReleaseError(
            "演练期间用户数据库 inode 或设备发生变化："
            f"before={before[users_path]} after={after[users_path]}"
        )


def check_instance_running(config: ReleaseConfig) -> None:
    result = run_aliyun_json(
        config,
        [
            "DescribeInstances",
            "--RegionId",
            config.region,
            "--InstanceIds",
            json.dumps([config.instance_id]),
        ],
        label="ECS 实例状态",
    )
    instances = result.get("Instances", {}).get("Instance", [])
    if len(instances) != 1 or instances[0].get("Status") != "Running":
        raise ReleaseError(f"目标 ECS 不是 Running：{instances}")


def split_archive(context: ReleaseContext, temp_root: Path) -> None:
    parts_dir = temp_root / "parts"
    parts_dir.mkdir()
    payload = context.archive.read_bytes()
    for index, offset in enumerate(range(0, len(payload), context.config.chunk_bytes)):
        path = parts_dir / f"part-{index:03d}"
        path.write_bytes(payload[offset : offset + context.config.chunk_bytes])
        context.parts.append(path)


def send_part(context: ReleaseContext, part: Path) -> None:
    config = context.config
    content = base64.b64encode(part.read_bytes()).decode("ascii")
    run_aliyun_json(
        config,
        [
            "SendFile",
            "--RegionId",
            config.region,
            "--InstanceId.1",
            config.instance_id,
            "--Name",
            part.name,
            "--ContentType",
            "Base64",
            "--Content",
            content,
            "--TargetDir",
            f"{context.staging_dir}/parts",
            "--FileMode",
            "0600",
            "--Overwrite",
            "false",
            "--Timeout",
            "120",
        ],
        label=f"上传 {part.name}",
    )


def upload_archive(context: ReleaseContext) -> None:
    print(f"并行上传 {len(context.parts)} 个分片", flush=True)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=context.config.upload_workers
    ) as executor:
        futures = [executor.submit(send_part, context, part) for part in context.parts]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def candidate_script(context: ReleaseContext) -> str:
    candidate = shell_quote(context.candidate_dir)
    staging = shell_quote(context.staging_dir)
    unit = shell_quote(context.unit_name)
    part_count = len(context.parts)
    last_size = context.parts[-1].stat().st_size
    smoke_checks = "\n".join(
        "test \"$(curl -sS -b \"$cookies\" -o /dev/null -w '%{http_code}' "
        f"http://127.0.0.1:18000{shell_quote(path)})\" = 200"
        for path in context.config.smoke_paths
    )
    return f"""set -eu
candidate={candidate}
staging={staging}
unit={unit}
parts="$staging/parts"
archive="$staging/release.tar.gz"
state="$staging/state"
test ! -e "$candidate"
count=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  count=$(find "$parts" -maxdepth 1 -type f -name 'part-*' | wc -l)
  if [ "$count" -eq {part_count} ]; then break; fi
  sleep 1
done
test "$count" -eq {part_count}
index=0
: > "$archive"
while [ "$index" -lt {part_count} ]; do
  name=$(printf 'part-%03d' "$index")
  test -f "$parts/$name"
  if [ "$index" -lt {part_count - 1} ]; then
    test "$(stat -c %s "$parts/$name")" -eq {context.config.chunk_bytes}
  else
    test "$(stat -c %s "$parts/$name")" -eq {last_size}
  fi
  cat "$parts/$name" >> "$archive"
  index=$((index + 1))
done
test "$(sha256sum "$archive" | awk '{{print $1}}')" = {context.archive_sha256}
tar -tzf "$archive" > "$staging/archive-files.txt"
if grep -E '(^|/)(data|logs|artifacts|backups)/|(^|/)\.env($|/)|\.sqlite3($|-)|\.sqlite3-(wal|shm)$|(^|/).*\.db$' "$staging/archive-files.txt"; then exit 41; fi
mkdir -m 755 "$candidate"
tar -xzf "$archive" -C "$candidate"
printf '%s\n' {shell_quote(context.commit)} > "$candidate/.deployed-commit"
chown -R masterstock:masterstock "$candidate"
install -d -o masterstock -g masterstock -m 700 /var/cache/masterstock/uv
sudo -u masterstock env UV_CACHE_DIR=/var/cache/masterstock/uv /usr/local/bin/uv --config-file /etc/uv/uv.toml venv --relocatable --python /usr/bin/python3.12 "$candidate/.venv"
sudo -u masterstock env UV_CACHE_DIR=/var/cache/masterstock/uv /usr/local/bin/uv --config-file /etc/uv/uv.toml pip install --python "$candidate/.venv/bin/python" -r "$candidate/requirements-runtime-lock.txt"
sudo -u masterstock env UV_CACHE_DIR=/var/cache/masterstock/uv /usr/local/bin/uv --config-file /etc/uv/uv.toml pip install --python "$candidate/.venv/bin/python" --no-deps "$candidate"
test "$(head -n 1 "$candidate/.venv/bin/masterstock")" = '#!/bin/sh'
test "$(cat "$candidate/.deployed-commit")" = {shell_quote(context.commit)}
pkg=$("$candidate/.venv/bin/python" -c 'import pathlib,master_stock_selector; print(pathlib.Path(master_stock_selector.__file__).resolve().parent)')
case "$pkg" in "$candidate"/*) ;; *) exit 42;; esac
test -f "$pkg/web/static/app.css"
test -f "$pkg/web/templates/base.html"
test -f "$pkg/web/templates/ashare/owner_activity.html"
test -f "$pkg/web/templates/ashare/shared_trading_plan.html"
test -f "$pkg/web/static/vendor/lightweight-charts-5.1.0.js"
test -f "$pkg/web/static/vendor/lucide-chevron-left.svg"
test -f "$pkg/web/templates/reading/archive.html"
test -f "$pkg/web/templates/reading/article.html"
test -f "$pkg/web/content/reading/adam-grimes-trading-templates/index.md"
test -f "$pkg/web/content/reading/adam-grimes-trading-templates/images/00103.jpg"
test "$(sha256sum "$pkg/web/static/app.css" | awk '{{print $1}}')" = {context.css_sha256}
install -d -o masterstock -g masterstock -m 700 "$state"
userdb="$state/users.sqlite3"
cookies="$state/cookies.txt"
sudo -u masterstock "$candidate/.venv/bin/python" -m master_stock_selector.web.users_cli --database "$userdb" schema-migrate --apply >/dev/null
sudo -u masterstock "$candidate/.venv/bin/python" - <<PY
from pathlib import Path
from master_stock_selector.web.users import USER_SCHEMA_VERSION, UserRepository
repo = UserRepository(Path("$userdb"))
repo.create_user("release-check", "ReleaseCheckOnly2026", "Release Check")
assert repo.schema_status()["schema_version"] == USER_SCHEMA_VERSION
PY
systemd-run --unit="$unit" --uid=masterstock --property=WorkingDirectory="$candidate" --setenv=MASTERSTOCK_SECURE_COOKIES=0 --setenv=MASTERSTOCK_ACCESS_LOGGING=0 "$candidate/.venv/bin/masterstock" web --host 127.0.0.1 --port 18000 --market-database /var/lib/masterstock/market.sqlite3 --watchlist-database /var/lib/masterstock/master_watchlist.sqlite3 --user-database "$userdb"
cleanup() {{ systemctl stop "$unit.service" >/dev/null 2>&1 || true; }}
trap cleanup EXIT INT TERM
http_code=000
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  http_code=$(curl -sS -o "$state/health.json" -w '%{{http_code}}' http://127.0.0.1:18000/healthz || true)
  if [ "$http_code" = 200 ]; then break; fi
  sleep 1
done
test "$http_code" = 200
grep -q '"market":true' "$state/health.json"
grep -q '"watchlist":true' "$state/health.json"
grep -q '"users":true' "$state/health.json"
test "$(curl -sS -o /dev/null -w '%{{http_code}}' http://127.0.0.1:18000/login)" = 200
test "$(curl -sS -o "$state/app.css" -w '%{{http_code}}' http://127.0.0.1:18000/static/app.css)" = 200
test "$(sha256sum "$state/app.css" | awk '{{print $1}}')" = {context.css_sha256}
test "$(curl -sS -c "$cookies" -o /dev/null -w '%{{http_code}}' -X POST --data 'username=release-check&password=ReleaseCheckOnly2026' http://127.0.0.1:18000/login)" = 303
{smoke_checks}
ss -ltn | grep -q '127.0.0.1:18000'
systemctl stop "$unit.service"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! ss -ltn | grep -q ':18000 '; then break; fi
  sleep 1
done
if ss -ltn | grep -q ':18000 '; then exit 43; fi
trap - EXIT INT TERM
rm -rf "$state"
echo CANDIDATE_OK
"""


def rehearsal_cleanup_script(context: ReleaseContext) -> str:
    return f"""set -eu
candidate={shell_quote(context.candidate_dir)}
staging={shell_quote(context.staging_dir)}
unit={shell_quote(context.unit_name)}
systemctl stop "$unit.service" >/dev/null 2>&1 || true
if ss -ltn | grep -q ':18000 '; then exit 51; fi
if [ -e "$candidate" ]; then
  test "$(cat "$candidate/.deployed-commit")" = {shell_quote(context.commit)}
  rm -rf "$candidate"
fi
if [ -e "$staging" ]; then rm -rf "$staging"; fi
test ! -e "$candidate"
test ! -e "$staging"
curl -fsS http://127.0.0.1:8000/healthz | grep -q '"status":"ok"'
echo REHEARSAL_CLEAN
"""


def apply_script(context: ReleaseContext) -> str:
    candidate = context.candidate_dir
    before = f"/opt/masterstock.before-{context.short_commit}"
    failed = f"/opt/masterstock.failed-{context.short_commit}"
    backup = (
        f"/var/backups/masterstock/users-v10-before-v11-{context.release_id}.sqlite3"
        if context.config.migrate_users
        else f"/var/backups/masterstock/users-release-{context.release_id}.sqlite3"
    )
    failed_user = f"/var/backups/masterstock/users-failed-{context.release_id}.sqlite3"
    migration_preflight = ""
    regular_preflight = ""
    if context.config.migrate_users:
        migration_preflight = """"$candidate/.venv/bin/python" - <<'PY'
from pathlib import Path
from master_stock_selector.web.users import USER_SCHEMA_VERSION, UserRepository

assert USER_SCHEMA_VERSION == 11
status = UserRepository(Path("/var/lib/masterstock/users.sqlite3")).schema_status()
assert status["quick_check"] == "ok"
assert status["schema_version"] == 10
assert status["required_version"] == 11
assert status["missing_tables"] == []
assert status["missing_columns"] == ["user_stock_review.c1_record_json"]
assert status["missing_indexes"] == []
assert not status["legacy_observation_states"]
assert not status["legacy_trade_setups"]
assert not status["legacy_chart_drawings"]
PY
"""
    else:
        regular_preflight = """sudo -u masterstock env MASTERSTOCK_USER_DATABASE=/var/lib/masterstock/users.sqlite3 "$candidate/scripts/manage_users.sh" schema-check >/dev/null
"$candidate/.venv/bin/python" - "$backup" <<'PY'
import sqlite3, sys
from master_stock_selector.web.users import USER_SCHEMA_VERSION
source = sqlite3.connect("file:/var/lib/masterstock/users.sqlite3?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[1])
try:
    assert source.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    source.backup(target)
    assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert target.execute("PRAGMA user_version").fetchone()[0] == USER_SCHEMA_VERSION
finally:
    target.close()
    source.close()
PY
chown masterstock:masterstock "$backup"
chmod 600 "$backup"
"""
    migration_step = ""
    if context.config.migrate_users:
        migration_step = f"""sudo -u masterstock env MASTERSTOCK_USER_DATABASE=/var/lib/masterstock/users.sqlite3 "$candidate/scripts/manage_users.sh" schema-migrate --apply --backup "$backup" > {shell_quote(context.staging_dir + '/user-migration-result.json')}
"$candidate/.venv/bin/python" - {shell_quote(context.staging_dir + '/user-migration-result.json')} "$backup" <<'PY'
import json, sqlite3, sys
from pathlib import Path
from master_stock_selector.web.users import USER_SCHEMA_VERSION, UserRepository

result = json.loads(Path(sys.argv[1]).read_text())
assert result["status"] == "migrated"
assert result["existing_row_counts_preserved"] is True
assert result["backup"]["path"] == sys.argv[2]
assert result["backup"]["quick_check"] == "ok"
with sqlite3.connect(f"file:{{sys.argv[2]}}?mode=ro", uri=True) as source:
    assert source.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert source.execute("PRAGMA user_version").fetchone()[0] == 10
status = UserRepository(Path("/var/lib/masterstock/users.sqlite3")).schema_status()
assert USER_SCHEMA_VERSION == 11
assert status["compatible"] is True
assert status["quick_check"] == "ok"
assert status["schema_version"] == 11
PY
migration_completed=1
"""
    user_fingerprint_check = (
        'test "$(stat -c \'%i|%d\' /var/lib/masterstock/users.sqlite3)" = "$users_identity_before"'
        if context.config.migrate_users
        else 'test "$(stat -c \'%s|%Y|%i|%d\' /var/lib/masterstock/users.sqlite3)" = "$users_before"'
    )
    return f"""set -eu
candidate={shell_quote(candidate)}
before={shell_quote(before)}
failed={shell_quote(failed)}
backup={shell_quote(backup)}
failed_user={shell_quote(failed_user)}
migrate_users={1 if context.config.migrate_users else 0}
migration_completed=0
test -d "$candidate"
test ! -e "$before"
test ! -e "$failed"
test ! -e "$backup"
test ! -e "$failed_user"
{migration_preflight}{regular_preflight}market_before=$(stat -c '%s|%Y|%i|%d' /var/lib/masterstock/market.sqlite3)
watchlist_before=$(stat -c '%s|%Y|%i|%d' /var/lib/masterstock/master_watchlist.sqlite3)
users_before=$(stat -c '%s|%Y|%i|%d' /var/lib/masterstock/users.sqlite3)
users_identity_before=$(stat -c '%i|%d' /var/lib/masterstock/users.sqlite3)
completed=0
rollback() {{
  exit_code=$?
  trap - EXIT INT TERM
  if [ "$completed" -ne 1 ]; then
    systemctl stop masterstock-web.service >/dev/null 2>&1 || true
    if [ -d "$before" ]; then
      if [ -e /opt/masterstock ]; then mv /opt/masterstock "$failed"; fi
      mv "$before" /opt/masterstock
    fi
    if [ "$migrate_users" -eq 1 ] && [ -f "$backup" ]; then
      /opt/masterstock/.venv/bin/python - "$backup" "$failed_user" <<'PY'
import sqlite3, sys

backup_path, failed_path = sys.argv[1:]
current = sqlite3.connect("/var/lib/masterstock/users.sqlite3")
failed = sqlite3.connect(failed_path)
try:
    current.backup(failed)
    assert failed.execute("PRAGMA quick_check").fetchone()[0] == "ok"
finally:
    failed.close()
    current.close()
source = sqlite3.connect(f"file:{{backup_path}}?mode=ro", uri=True)
target = sqlite3.connect("/var/lib/masterstock/users.sqlite3")
try:
    assert source.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert source.execute("PRAGMA user_version").fetchone()[0] == 10
    source.backup(target)
    assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert target.execute("PRAGMA user_version").fetchone()[0] == 10
finally:
    target.close()
    source.close()
PY
      chown masterstock:masterstock /var/lib/masterstock/users.sqlite3 "$failed_user"
      chmod 600 /var/lib/masterstock/users.sqlite3 "$failed_user"
    fi
    systemctl start masterstock-web.service
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then break; fi
      sleep 1
    done
  fi
  exit "$exit_code"
}}
trap rollback EXIT INT TERM
systemctl stop masterstock-web.service
test "$(systemctl is-active masterstock-web.service || true)" = inactive
{migration_step}mv /opt/masterstock "$before"
mv "$candidate" /opt/masterstock
systemctl start masterstock-web.service
http_code=000
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  http_code=$(curl -sS -o /tmp/masterstock-release-health-{context.release_id}.json -w '%{{http_code}}' http://127.0.0.1:8000/healthz || true)
  if [ "$http_code" = 200 ]; then break; fi
  sleep 1
done
test "$http_code" = 200
test "$(cat /opt/masterstock/.deployed-commit)" = {shell_quote(context.commit)}
test "$(stat -c '%s|%Y|%i|%d' /var/lib/masterstock/market.sqlite3)" = "$market_before"
test "$(stat -c '%s|%Y|%i|%d' /var/lib/masterstock/master_watchlist.sqlite3)" = "$watchlist_before"
{user_fingerprint_check}
completed=1
trap - EXIT INT TERM
rm -f /tmp/masterstock-release-health-{context.release_id}.json
rm -rf {shell_quote(context.staging_dir)}
echo "APPLY_OK backup=$backup rollback=$before"
"""


def final_fingerprint_script() -> str:
    return """set -eu
for unit in masterstock-web.service masterstock-daily.timer masterstock-backup.timer nginx.service certbot.timer; do
  test "$(systemctl is-active "$unit")" = active
done
curl -fsS http://127.0.0.1:8000/healthz | grep -q '"status":"ok"'
sudo -u masterstock env MASTERSTOCK_USER_DATABASE=/var/lib/masterstock/users.sqlite3 /opt/masterstock/scripts/manage_users.sh schema-check >/dev/null
if ss -ltn | grep -q ':18000 '; then exit 61; fi
for db in /var/lib/masterstock/market.sqlite3 /var/lib/masterstock/master_watchlist.sqlite3 /var/lib/masterstock/users.sqlite3; do
  stat -c 'DB|%n|%s|%Y|%i|%d' "$db"
done
echo FINAL_REMOTE_OK
"""


def public_acceptance(config: ReleaseConfig) -> None:
    canonical = config.canonical_url
    checks = (
        (f"{canonical}/healthz", "200"),
        (f"{canonical}/login", "200"),
        (f"{canonical}/a/daily", "200"),
    )
    for url, expected in checks:
        code = output_of(
            [
                "curl",
                "-sS",
                "--connect-timeout",
                "10",
                "--max-time",
                "30",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                url,
            ]
        )
        if code != expected:
            raise ReleaseError(f"公网验收失败：{url} expected={expected} actual={code}")


def run_release(config: ReleaseConfig) -> int:
    started = time.monotonic()
    context: ReleaseContext | None = None
    baseline_fingerprints: dict[str, str] | None = None
    cleanup_needed = False
    with tempfile.TemporaryDirectory(prefix="masterstock-release-") as temp_name:
        temp_root = Path(temp_name)
        local_candidate_started = time.monotonic()
        print("\n== 本地发布候选 ==", flush=True)
        context = build_local_candidate(config, temp_root)
        context.timings["本地发布候选"] = round(
            time.monotonic() - local_candidate_started,
            2,
        )
        print(
            f"== 本地发布候选: {context.timings['本地发布候选']:.1f}s ==",
            flush=True,
        )
        with timed(context, "本地验证"):
            run_local_tests(config)
        split_archive(context, temp_root)

        try:
            with timed(context, "ECS 基线"):
                check_instance_running(config)
                baseline_output = run_cloud_script(
                    config,
                    baseline_script(context),
                    label="ECS 基线",
                    timeout=120,
                )
                baseline_fingerprints = parse_db_fingerprints(baseline_output)
                live_commit = parse_live_commit(baseline_output)
                if config.apply and live_commit == context.commit:
                    raise ReleaseError(f"ECS 已运行目标提交，拒绝重复发布：{live_commit}")
            cleanup_needed = True
            with timed(context, "上传归档"):
                upload_archive(context)
            with timed(context, "远端候选"):
                run_cloud_script(
                    config,
                    candidate_script(context),
                    label="远端候选",
                    timeout=config.command_timeout,
                )
            if config.apply:
                with timed(context, "备份与原子切换"):
                    run_cloud_script(
                        config,
                        apply_script(context),
                        label="备份与原子切换",
                        timeout=180,
                    )
                cleanup_needed = False
            else:
                with timed(context, "演练清理"):
                    run_cloud_script(
                        config,
                        rehearsal_cleanup_script(context),
                        label="演练清理",
                        timeout=120,
                    )
                cleanup_needed = False

            with timed(context, "远端终验"):
                final_output = run_cloud_script(
                    config,
                    final_fingerprint_script(),
                    label="远端终验",
                    timeout=120,
                )
                final_fingerprints = parse_db_fingerprints(final_output)
                if not config.apply:
                    verify_rehearsal_fingerprints(
                        baseline_fingerprints,
                        final_fingerprints,
                    )
            if config.apply:
                with timed(context, "公网验收"):
                    public_acceptance(config)
        finally:
            if cleanup_needed and context is not None and not config.apply:
                print("演练异常，尝试清理本次精确候选与暂存目录", flush=True)
                try:
                    run_cloud_script(
                        config,
                        rehearsal_cleanup_script(context),
                        label="异常清理",
                        timeout=120,
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"异常清理失败：{error}", file=sys.stderr, flush=True)

    assert context is not None
    total = round(time.monotonic() - started, 2)
    context.timings["总耗时"] = total
    print(
        json.dumps(
            {
                "status": "applied" if config.apply else "rehearsed",
                "commit": context.commit,
                "release_id": context.release_id,
                "timings_seconds": context.timings,
                "total_seconds": total,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_release(parse_args(argv))
    except (ReleaseError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"发布失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
