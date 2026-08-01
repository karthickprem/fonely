"""Security behavior tests for scripts/pre-push-audit.sh.

All credentials are synthetic and assembled at runtime so this source file does
not itself contain a complete token. Tests use isolated temporary Git repos and
never contact external services.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "pre-push-audit.sh"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )


def create_repo(tmp_path: Path, *, initial_commit: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(AUDIT_SCRIPT, repo / "scripts" / "pre-push-audit.sh")
    os.chmod(repo / "scripts" / "pre-push-audit.sh", 0o755)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "audit@example.invalid")
    git(repo, "config", "user.name", "Audit Test")
    (repo / ".gitignore").write_text(".env\n")
    git(repo, "add", ".gitignore", "scripts/pre-push-audit.sh")
    if initial_commit:
        git(repo, "commit", "-qm", "initial")
    return repo


def run_audit(
    repo: Path,
    *args: str,
    path: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if path is not None:
        env["PATH"] = path
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / "scripts" / "pre-push-audit.sh"), *args],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def classic_pat(body: str = "A" * 36) -> str:
    return "ghp" + "_" + body


def fine_grained_pat(body: str = "A" * 42) -> str:
    return "github" + "_pat_" + body


def amd_value() -> str:
    return "amd" + "_fake_subscription_value_1234567890"


def assert_secret_not_printed(result: subprocess.CompletedProcess[str], secret: str) -> None:
    assert secret not in result.stdout
    assert secret not in result.stderr


def stage_file(repo: Path, path: str, content: str, *, force: bool = False) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    args = ["add"]
    if force:
        args.append("-f")
    args.append(path)
    git(repo, *args)


@pytest.mark.parametrize(
    ("category", "secret"),
    [
        ("github-classic-pat", classic_pat()),
        ("github-fine-grained-pat", fine_grained_pat()),
    ],
)
def test_staged_github_tokens_rejected_without_printing_value(
    tmp_path: Path,
    category: str,
    secret: str,
) -> None:
    repo = create_repo(tmp_path)
    stage_file(repo, "token.txt", secret)
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert category in result.stderr
    assert 'path="token.txt"' in result.stderr
    assert_secret_not_printed(result, secret)


def test_literal_fine_grained_prefix_without_body_is_allowed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    stage_file(repo, "docs.txt", "The prefix is " + "github" + "_pat_" + ".")
    result = run_audit(repo, "--staged")
    assert result.returncode == 0


@pytest.mark.parametrize("separator", ["=", ":"])
def test_staged_amd_subscription_header_forms_rejected(
    tmp_path: Path,
    separator: str,
) -> None:
    repo = create_repo(tmp_path)
    secret = amd_value()
    stage_file(repo, "config.txt", f"Ocp-Apim-Subscription-Key{separator} {secret}\n")
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "amd-gateway-key" in result.stderr
    assert_secret_not_printed(result, secret)


@pytest.mark.parametrize("embedded_word", ["example", "dummy"])
def test_placeholder_substring_inside_opaque_token_still_rejected(
    tmp_path: Path,
    embedded_word: str,
) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat(embedded_word + "A" * 32)
    stage_file(repo, "opaque.txt", secret)
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert_secret_not_printed(result, secret)


def test_exact_placeholder_is_allowed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    stage_file(repo, "example.env", "EXOTEL_API_TOKEN=YOUR_TOKEN_HERE\n")
    result = run_audit(repo, "--staged")
    assert result.returncode == 0


def test_exact_approved_local_test_database_fixture_is_allowed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    path = "backend/tests/unit/pending_actions/test_postgres_safety.py"
    approved = (
        "postgresql+asyncpg:/" + "/fonely_test_user:secret" + "@localhost:5432/fonely_test_run1"
    )
    stage_file(repo, path, f'VALID_URL = "{approved}"\n')
    result = run_audit(repo, "--staged")
    assert result.returncode == 0


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+asyncpg:/" + "/app_user:secret" + "@production.example.com:5432/customer_prod",
        "postgresql+asyncpg:/"
        + "/fonely_test_user:secret"
        + "@production.example.com:5432/fonely_test",
        "postgresql+asyncpg:/" + "/fonely_test_user:secret" + "@localhost:5432/customer_prod",
        "postgresql+asyncpg:/"
        + "/fonely_test_user:secret"
        + "@localhost:5432/fonely_test?ssl=require",
        "postgresql+asyncpg:/" + "/fonely_test_user:secret" + "@localhost:5432/fonely_test#unsafe",
        "postgresql+asyncpg:/" + "/fonely_test_user:not_approved" + "@localhost:5432/fonely_test",
    ],
)
def test_unsafe_database_fixture_variants_are_rejected(
    tmp_path: Path,
    value: str,
) -> None:
    repo = create_repo(tmp_path)
    stage_file(
        repo,
        "backend/tests/unit/pending_actions/test_postgres_safety.py",
        f'URL = "{value}"\n',
    )
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "credentialed-database-url" in result.stderr
    assert_secret_not_printed(result, value)


def test_approved_database_fixture_in_unapproved_path_is_rejected(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    value = "postgresql+asyncpg:/" + "/fonely_test_user:secret" + "@localhost:5432/fonely_test_run1"
    stage_file(repo, "other_test.py", f'URL = "{value}"\n')
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "credentialed-database-url" in result.stderr
    assert_secret_not_printed(result, value)


def test_ignored_env_is_not_a_working_tree_candidate_or_read(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    secret = classic_pat()
    env_file = repo / ".env"
    env_file.write_text(secret)
    os.chmod(env_file, 0)
    try:
        result = run_audit(repo, "--working-tree")
    finally:
        os.chmod(env_file, 0o600)
    assert result.returncode == 0
    assert_secret_not_printed(result, secret)


def test_forcibly_staged_env_is_rejected_from_index_blob(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat()
    stage_file(repo, ".env", secret, force=True)
    (repo / ".env").unlink()
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "forbidden-path" in result.stderr
    assert_secret_not_printed(result, secret)


def test_oversized_working_tree_file_is_rejected(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    oversized = repo / "large.txt"
    with oversized.open("wb") as file_obj:
        file_obj.truncate(10 * 1024 * 1024 + 1)
    result = run_audit(repo, "--working-tree")
    assert result.returncode == 1
    assert "file-over-10MiB" in result.stderr


@pytest.mark.parametrize("mode", ["staged", "range"])
def test_oversized_git_blob_is_rejected_without_retrieving_content(
    tmp_path: Path,
    mode: str,
) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    oversized = repo / "large.txt"
    with oversized.open("wb") as file_obj:
        file_obj.truncate(10 * 1024 * 1024 + 1)
    git(repo, "add", "large.txt")
    oid = git(repo, "rev-parse", ":large.txt").stdout.strip()
    args = ("--staged",)
    if mode == "range":
        git(repo, "commit", "-qm", "add oversized blob")
        args = ("--range", f"{base}..HEAD")

    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "wrapper-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GIT_CALL_LOG"\n'
        'if [[ "$3" == "cat-file" && "$4" == "blob" && "$5" == "$BLOCKED_OID" ]]; then\n'
        "  exit 97\n"
        "fi\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    wrapper.chmod(0o755)
    call_log = tmp_path / "git-calls.log"
    result = run_audit(
        repo,
        *args,
        path=f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}",
        extra_env={
            "BLOCKED_OID": oid,
            "GIT_CALL_LOG": str(call_log),
            "REAL_GIT": real_git,
        },
    )
    assert result.returncode == 1
    assert "file-over-10MiB" in result.stderr
    assert f"cat-file blob {oid}" not in call_log.read_text()


def test_binary_file_is_not_scanned_as_text(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat().encode()
    binary = b"\x00\xff" + secret
    target = repo / "binary.bin"
    target.write_bytes(binary)
    git(repo, "add", "binary.bin")
    result = run_audit(repo, "--staged")
    assert result.returncode == 0


def test_working_tree_symlink_is_not_followed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    outside = tmp_path / "outside-secret.txt"
    secret = classic_pat()
    outside.write_text(secret)
    (repo / "link.txt").symlink_to(outside)
    result = run_audit(repo, "--working-tree")
    assert result.returncode == 0
    assert_secret_not_printed(result, secret)


def test_working_tree_symlink_parent_is_not_followed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret = classic_pat()
    (outside_dir / "secret.txt").write_text(secret)
    (repo / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
    result = run_audit(repo, "--working-tree")
    assert result.returncode == 0
    assert_secret_not_printed(result, secret)


@pytest.mark.parametrize("filename", ["space name.txt", "tab\tname.txt", "line\nname.txt"])
def test_staged_mode_handles_special_filenames_safely(
    tmp_path: Path,
    filename: str,
) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat()
    stage_file(repo, filename, secret)
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert_secret_not_printed(result, secret)
    assert "github-classic-pat" in result.stderr


def test_range_blob_not_hidden_by_clean_working_tree_copy(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    secret = classic_pat()
    stage_file(repo, "historical.txt", secret)
    git(repo, "commit", "-qm", "commit secret")
    (repo / "historical.txt").write_text("clean replacement")
    result = run_audit(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 1
    assert_secret_not_printed(result, secret)
    assert "commit=" in result.stderr


def test_audit_runs_without_backend_virtualenv(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    assert not (repo / "backend" / ".venv").exists()
    result = run_audit(repo, "--working-tree")
    assert result.returncode == 0


def test_missing_python_fails_clearly(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    limited_bin = tmp_path / "limited-bin"
    limited_bin.mkdir()
    for command in ("bash", "dirname", "mktemp", "rm", "git"):
        executable = shutil.which(command)
        assert executable is not None
        (limited_bin / command).symlink_to(executable)
    result = run_audit(repo, "--working-tree", path=str(limited_bin))
    assert result.returncode == 2
    assert "Python 3 or Python is required" in result.stderr


def test_default_before_git_initialization_uses_safe_working_tree_mode(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scripts").mkdir()
    shutil.copy2(AUDIT_SCRIPT, project / "scripts" / "pre-push-audit.sh")
    os.chmod(project / "scripts" / "pre-push-audit.sh", 0o755)
    (project / ".gitignore").write_text(".env\n")
    secret = classic_pat()
    ignored = project / ".env"
    ignored.write_text(secret)
    os.chmod(ignored, 0)
    (project / "clean.txt").write_text("clean")
    try:
        result = run_audit(project)
    finally:
        os.chmod(ignored, 0o600)
    assert result.returncode == 0
    assert "mode=working-tree" in result.stdout
    assert_secret_not_printed(result, secret)


def test_staged_mode_reads_index_blob_not_working_tree(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat()
    stage_file(repo, "config.txt", secret)
    (repo / "config.txt").write_text("clean working-tree replacement")
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert_secret_not_printed(result, secret)


def test_intent_to_add_is_rejected_as_unscannable(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    (repo / "intent.txt").write_text("future content")
    git(repo, "add", "-N", "intent.txt")
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "unscannable-intent-to-add" in result.stderr


def test_staged_regular_file_to_secret_symlink_type_change_is_rejected(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    stage_file(repo, "type-change.txt", "clean")
    git(repo, "commit", "-qm", "add regular file")
    (repo / "type-change.txt").unlink()
    secret = classic_pat()
    (repo / "type-change.txt").symlink_to(secret)
    git(repo, "add", "type-change.txt")
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "github-classic-pat" in result.stderr
    assert_secret_not_printed(result, secret)


def test_range_regular_file_to_secret_symlink_type_change_is_rejected(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    stage_file(repo, "type-change.txt", "clean")
    git(repo, "commit", "-qm", "add regular file")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "type-change.txt").unlink()
    secret = classic_pat()
    (repo / "type-change.txt").symlink_to(secret)
    git(repo, "add", "type-change.txt")
    git(repo, "commit", "-qm", "change regular file to symlink")
    result = run_audit(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 1
    assert "github-classic-pat" in result.stderr
    assert_secret_not_printed(result, secret)


def test_staged_symlink_to_secret_regular_file_type_change_is_rejected(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    (repo / "type-change.txt").symlink_to("clean-target")
    git(repo, "add", "type-change.txt")
    git(repo, "commit", "-qm", "add symlink")
    (repo / "type-change.txt").unlink()
    secret = classic_pat()
    (repo / "type-change.txt").write_text(secret)
    git(repo, "add", "type-change.txt")
    result = run_audit(repo, "--staged")
    assert result.returncode == 1
    assert "github-classic-pat" in result.stderr
    assert_secret_not_printed(result, secret)


def test_clean_staged_type_change_is_allowed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    stage_file(repo, "type-change.txt", "clean")
    git(repo, "commit", "-qm", "add regular file")
    (repo / "type-change.txt").unlink()
    (repo / "type-change.txt").symlink_to("clean-target")
    git(repo, "add", "type-change.txt")
    result = run_audit(repo, "--staged")
    assert result.returncode == 0


def test_clean_range_type_change_is_allowed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    stage_file(repo, "type-change.txt", "clean")
    git(repo, "commit", "-qm", "add regular file")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "type-change.txt").unlink()
    (repo / "type-change.txt").symlink_to("clean-target")
    git(repo, "add", "type-change.txt")
    git(repo, "commit", "-qm", "change regular file to symlink")
    result = run_audit(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 0


def test_range_symlink_to_secret_regular_file_type_change_is_rejected(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    (repo / "type-change.txt").symlink_to("clean-target")
    git(repo, "add", "type-change.txt")
    git(repo, "commit", "-qm", "add symlink")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "type-change.txt").unlink()
    secret = classic_pat()
    (repo / "type-change.txt").write_text(secret)
    git(repo, "add", "type-change.txt")
    git(repo, "commit", "-qm", "change symlink to regular file")
    result = run_audit(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 1
    assert "github-classic-pat" in result.stderr
    assert_secret_not_printed(result, secret)


def test_range_detects_secret_in_root_commit(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = classic_pat()
    stage_file(repo, "root-secret.txt", secret)
    git(repo, "commit", "-qm", "root with secret")
    (repo / "root-secret.txt").write_text("clean replacement")
    result = run_audit(repo, "--range", "HEAD")
    assert result.returncode == 1
    assert "commit=" in result.stderr
    assert_secret_not_printed(result, secret)


def test_range_detects_secret_added_then_removed(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    secret = fine_grained_pat()
    stage_file(repo, "history.txt", secret)
    git(repo, "commit", "-qm", "add secret")
    (repo / "history.txt").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-qm", "remove secret")
    result = run_audit(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 1
    assert "commit=" in result.stderr
    assert_secret_not_printed(result, secret)


def test_invalid_range_fails_without_fallback(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    result = run_audit(repo, "--range", "missing-ref..HEAD")
    assert result.returncode == 2
    assert "Invalid or ambiguous revision range" in result.stderr


def test_default_requires_explicit_mode_when_repository_has_commits(tmp_path: Path) -> None:
    repo = create_repo(tmp_path, initial_commit=True)
    result = run_audit(repo)
    assert result.returncode == 2
    assert "Explicit --working-tree" in result.stderr
