from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts/release_ecs.py"
SPEC = importlib.util.spec_from_file_location("release_ecs", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_ecs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_ecs
SPEC.loader.exec_module(release_ecs)


def test_default_mode_is_safe_rehearsal() -> None:
    config = release_ecs.parse_args([])

    assert config.apply is False
    assert config.migrate_users is False
    assert config.smoke_paths == ("/a/daily",)
    assert config.chunk_bytes == 24_000


def test_apply_requires_commit_confirmation() -> None:
    with pytest.raises(SystemExit):
        release_ecs.parse_args(["--apply"])


def test_user_migration_requires_apply() -> None:
    with pytest.raises(SystemExit):
        release_ecs.parse_args(["--migrate-users"])

    config = release_ecs.parse_args(
        ["--apply", "--migrate-users", "--confirm-commit", "abcdef0"]
    )
    assert config.apply is True
    assert config.migrate_users is True


def test_apply_refuses_test_and_worktree_bypasses() -> None:
    with pytest.raises(SystemExit):
        release_ecs.parse_args(
            ["--apply", "--confirm-commit", "abcdef0", "--skip-tests"]
        )
    with pytest.raises(SystemExit):
        release_ecs.parse_args(
            ["--apply", "--confirm-commit", "abcdef0", "--allow-dirty-worktree"]
        )


def test_smoke_paths_are_deduplicated_and_validated() -> None:
    config = release_ecs.parse_args(
        ["--smoke-path", "/a/daily", "--smoke-path", "/a/owner"]
    )
    assert config.smoke_paths == ("/a/daily", "/a/owner")

    with pytest.raises(SystemExit):
        release_ecs.parse_args(["--smoke-path", "not-absolute"])


def test_archive_policy_rejects_runtime_data_and_secrets() -> None:
    release_ecs.validate_archive_names(["README.md", "src/package/app.py"])

    for forbidden in (
        "data/market.sqlite3",
        ".env",
        "logs/web.log",
        "backups/users.sqlite3",
        "nested/users.sqlite3-wal",
    ):
        with pytest.raises(release_ecs.ReleaseError):
            release_ecs.validate_archive_names([forbidden])


def test_required_wheel_resources_include_nested_reading_assets() -> None:
    assert (
        "master_stock_selector/web/content/reading/mark-minervini-breakout-trading/"
        "images/workbook-figures/mtp-v2-p453-fwlt-breakout.jpeg"
        in release_ecs.REQUIRED_WHEEL_RESOURCES
    )


def test_baseline_and_apply_scripts_keep_safety_contract() -> None:
    config = release_ecs.ReleaseConfig(
        apply=True,
        migrate_users=False,
        confirm_commit="a" * 40,
        profile="codex-aliyun",
        region="cn-hangzhou",
        instance_id="i-example",
        canonical_url="https://example.com",
        smoke_paths=("/a/daily",),
        skip_tests=False,
        allow_dirty_worktree=False,
        upload_workers=8,
        chunk_bytes=24_000,
        command_timeout=600,
    )
    context = release_ecs.ReleaseContext(
        config=config,
        commit="a" * 40,
        short_commit="aaaaaaa",
        release_id="aaaaaaa-20260820t000000z",
        archive=Path("/tmp/archive.tar.gz"),
        archive_sha256="b" * 64,
        css_sha256="c" * 64,
        candidate_dir="/opt/masterstock.next-aaaaaaa",
        staging_dir="/tmp/masterstock-release-aaaaaaa-20260820t000000z",
        unit_name="masterstock-candidate-aaaaaaa-20260820t000000z",
    )

    baseline = release_ecs.baseline_script(context)
    apply = release_ecs.apply_script(context)

    assert "schema-check" in baseline
    assert ":18000 " in baseline
    assert "systemctl stop masterstock-web.service" in apply
    assert "source.backup(target)" in apply
    assert "PRAGMA quick_check" in apply
    assert "mv /opt/masterstock \"$before\"" in apply
    assert "mv \"$before\" /opt/masterstock" in apply
    assert "market_before" in apply
    assert "users_before" in apply


def test_migration_apply_script_freezes_v11_to_v12_contract(tmp_path: Path) -> None:
    config = release_ecs.parse_args(
        ["--apply", "--migrate-users", "--confirm-commit", "a" * 40]
    )
    context = release_ecs.ReleaseContext(
        config=config,
        commit="a" * 40,
        short_commit="aaaaaaa",
        release_id="aaaaaaa-20260827t000000z",
        archive=tmp_path / "archive.tar.gz",
        archive_sha256="b" * 64,
        css_sha256="c" * 64,
        candidate_dir="/opt/masterstock.next-aaaaaaa",
        staging_dir="/tmp/masterstock-release-aaaaaaa-20260827t000000z",
        unit_name="masterstock-candidate-aaaaaaa-20260827t000000z",
    )

    script = release_ecs.apply_script(context)

    assert "users-v11-before-v12-" in script
    assert "assert USER_SCHEMA_VERSION == 12" in script
    assert 'assert status["schema_version"] == 11' in script
    assert 'schema-migrate --apply --backup "$backup"' in script
    assert 'assert source.execute("PRAGMA user_version").fetchone()[0] == 11' in script
    assert 'assert source.execute("PRAGMA user_version").fetchone()[0] == 10' not in script
    assert 'assert target.execute("PRAGMA user_version").fetchone()[0] == 11' in script
    assert 'assert target.execute("PRAGMA user_version").fetchone()[0] == 10' not in script
    assert 'assert status["schema_version"] == 12' in script
    assert "source.backup(target)" in script
    assert "systemctl stop masterstock-web.service" in script
    assert "systemctl start masterstock-web.service" in script
    syntax = subprocess.run(
        ["sh", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr


def test_rehearsal_cleanup_is_exact_and_checks_live_health() -> None:
    config = release_ecs.parse_args([])
    context = release_ecs.ReleaseContext(
        config=config,
        commit="d" * 40,
        short_commit="ddddddd",
        release_id="ddddddd-20260820t000000z",
        archive=Path("/tmp/archive.tar.gz"),
        archive_sha256="e" * 64,
        css_sha256="f" * 64,
        candidate_dir="/opt/masterstock.rehearsal-ddddddd-20260820t000000z",
        staging_dir="/tmp/masterstock-release-ddddddd-20260820t000000z",
        unit_name="masterstock-candidate-ddddddd-20260820t000000z",
    )

    script = release_ecs.rehearsal_cleanup_script(context)

    assert "rm -rf \"$candidate\"" in script
    assert "rm -rf \"$staging\"" in script
    assert "curl -fsS http://127.0.0.1:8000/healthz" in script
    assert "masterstock.before" not in script


def test_cloud_payloads_are_redacted_from_command_logging() -> None:
    command = [
        "aliyun",
        "ecs",
        "RunCommand",
        "--CommandContent",
        "secret-command",
        "--Content",
        "secret-file",
    ]

    redacted = release_ecs._redact_content(command)

    assert "secret-command" not in redacted
    assert "secret-file" not in redacted
    assert redacted.count("<base64-content>") == 2


def test_installed_import_check_resolves_macos_temp_path_alias(tmp_path: Path) -> None:
    code = release_ecs.installed_import_check(tmp_path / "wheel-install")

    assert "target=Path(" in code
    assert ".resolve()" in code
    assert "p.is_relative_to(target)" in code


def test_rehearsal_fingerprints_allow_user_log_growth_only() -> None:
    before = {
        "/var/lib/masterstock/market.sqlite3": "100|10|1|7",
        "/var/lib/masterstock/master_watchlist.sqlite3": "200|20|2|7",
        "/var/lib/masterstock/users.sqlite3": "300|30|3|7",
    }
    after = {**before, "/var/lib/masterstock/users.sqlite3": "400|40|3|7"}

    release_ecs.verify_rehearsal_fingerprints(before, after)

    changed_market = {**after, "/var/lib/masterstock/market.sqlite3": "101|10|1|7"}
    with pytest.raises(release_ecs.ReleaseError):
        release_ecs.verify_rehearsal_fingerprints(before, changed_market)

    replaced_users = {**after, "/var/lib/masterstock/users.sqlite3": "400|40|9|7"}
    with pytest.raises(release_ecs.ReleaseError):
        release_ecs.verify_rehearsal_fingerprints(before, replaced_users)


def test_runtime_lock_is_a_version_consistent_production_subset() -> None:
    development = (PROJECT_ROOT / "requirements-lock.txt").read_text()
    runtime = (PROJECT_ROOT / "requirements-runtime-lock.txt").read_text()

    for package in ("fastapi", "httpx2", "starlette", "uvicorn"):
        development_line = next(
            line for line in development.splitlines() if line.startswith(f"{package}==")
        )
        assert development_line in runtime.splitlines()
    for development_only in ("pytest==", "mypy==", "ruff==", "wheel=="):
        assert development_only not in runtime


def test_generated_remote_scripts_are_posix_shell_syntax(tmp_path: Path) -> None:
    config = release_ecs.parse_args(
        ["--smoke-path", "/a/reading/al-brooks-pullback-trading"]
    )
    part = tmp_path / "part-000"
    part.write_bytes(b"release")
    context = release_ecs.ReleaseContext(
        config=config,
        commit="a" * 40,
        short_commit="aaaaaaa",
        release_id="aaaaaaa-20260820t000000z",
        archive=tmp_path / "archive.tar.gz",
        archive_sha256="b" * 64,
        css_sha256="c" * 64,
        candidate_dir="/opt/masterstock.rehearsal-aaaaaaa-20260820t000000z",
        staging_dir="/tmp/masterstock-release-aaaaaaa-20260820t000000z",
        unit_name="masterstock-candidate-aaaaaaa-20260820t000000z",
        parts=[part],
    )

    scripts = (
        release_ecs.baseline_script(context),
        release_ecs.candidate_script(context),
        release_ecs.rehearsal_cleanup_script(context),
        release_ecs.apply_script(context),
        release_ecs.final_fingerprint_script(),
    )
    for script in scripts:
        result = subprocess.run(
            ["sh", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_candidate_schema_check_uses_the_packaged_schema_version(tmp_path: Path) -> None:
    config = release_ecs.parse_args([])
    part = tmp_path / "part-000"
    part.write_bytes(b"release")
    context = release_ecs.ReleaseContext(
        config=config,
        commit="a" * 40,
        short_commit="aaaaaaa",
        release_id="aaaaaaa-20260823t000000z",
        archive=tmp_path / "archive.tar.gz",
        archive_sha256="b" * 64,
        css_sha256="c" * 64,
        candidate_dir="/opt/masterstock.rehearsal-aaaaaaa",
        staging_dir="/tmp/masterstock-release-aaaaaaa-20260823t000000z",
        unit_name="masterstock-candidate-aaaaaaa-20260823t000000z",
        parts=[part],
    )

    script = release_ecs.candidate_script(context)

    assert "USER_SCHEMA_VERSION, UserRepository" in script
    assert '== USER_SCHEMA_VERSION' in script


def test_apply_backup_schema_check_uses_the_packaged_schema_version(tmp_path: Path) -> None:
    config = release_ecs.parse_args(["--apply", "--confirm-commit", "a" * 40])
    context = release_ecs.ReleaseContext(
        config=config,
        commit="a" * 40,
        short_commit="aaaaaaa",
        release_id="aaaaaaa-20260823t000000z",
        archive=tmp_path / "archive.tar.gz",
        archive_sha256="b" * 64,
        css_sha256="c" * 64,
        candidate_dir="/opt/masterstock.rehearsal-aaaaaaa",
        staging_dir="/tmp/masterstock-release-aaaaaaa-20260823t000000z",
        unit_name="masterstock-candidate-aaaaaaa-20260823t000000z",
    )

    script = release_ecs.apply_script(context)

    assert "from master_stock_selector.web.users import USER_SCHEMA_VERSION" in script
    assert '== USER_SCHEMA_VERSION' in script
