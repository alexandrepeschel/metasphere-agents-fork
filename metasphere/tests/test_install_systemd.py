"""Tests for the three split daemon systemd unit templates and the
``install.sh`` rendering logic that materializes them under
``~/.config/systemd/user/``.

The templates live at ``systemd/user/metasphere-<daemon>.service`` in
the repo and use ``@@PLACEHOLDER@@`` markers that ``install.sh``
substitutes at install time. Tests here cover:

- Template content invariants (Restart=always, no operator paths,
  expected markers present).
- ``install.sh`` source-level greps that confirm the render block
  exists and phases out the obsolete omnibus ``metasphere.service``.
- A subprocess-driven render against a sandbox ``METASPHERE_DIR`` that
  exercises the actual sed substitution path and verifies idempotency
  + obsolete-unit removal. Sandboxed via ``HOME`` override so the test
  cannot touch the operator's real ``~/.config/systemd/user/``.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "systemd" / "user"
INSTALL_SH = REPO_ROOT / "install.sh"

DAEMONS = ("gateway", "heartbeat", "schedule")


# ---------------------------------------------------------------------------
# Template content invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("daemon", DAEMONS)
def test_template_exists(daemon):
    tmpl = TEMPLATE_DIR / f"metasphere-{daemon}.service"
    assert tmpl.is_file(), f"missing template {tmpl}"


@pytest.mark.parametrize("daemon", DAEMONS)
def test_template_has_restart_always(daemon):
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert "Restart=always" in tmpl, (
        f"{daemon}.service missing Restart=always — clean exits won't respawn"
    )
    assert "Restart=on-failure" not in tmpl, (
        f"{daemon}.service still has Restart=on-failure — clean exits won't respawn"
    )


@pytest.mark.parametrize("daemon", DAEMONS)
def test_template_has_wantedby_default(daemon):
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert "WantedBy=default.target" in tmpl


@pytest.mark.parametrize("daemon", DAEMONS)
def test_template_no_operator_paths(daemon):
    """Templates must not bake operator-specific paths.

    Heuristic from CLAUDE.md: "would this string be wrong on a
    stranger's install?" Any operator-specific `/home/<user>` or
    project-checkout name would.
    """
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert "rage-substrate" not in tmpl
    assert "/home/" not in tmpl, (
        f"{daemon}.service has a /home/ path — use %h or @@METASPHERE_DIR@@"
    )


@pytest.mark.parametrize("daemon", DAEMONS)
def test_template_uses_placeholders(daemon):
    """Templates must use install-time placeholders, not absolute
    paths, for METASPHERE_DIR + venv binary location."""
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert "@@METASPHERE_DIR@@" in tmpl
    assert "@@METASPHERE_VENV_BIN@@" in tmpl
    assert "@@METASPHERE_AGENT_RUNTIME@@" in tmpl


def test_gateway_conflicts_with_telegram_unit():
    """Gateway phase-out of the standalone telegram poller is a
    structural invariant — running both causes a getUpdates race."""
    tmpl = (TEMPLATE_DIR / "metasphere-gateway.service").read_text()
    assert "Conflicts=metasphere-telegram.service" in tmpl


@pytest.mark.parametrize(
    "daemon,expected",
    [
        ("gateway", "RestartSec=5"),
        ("heartbeat", "RestartSec=10"),
        ("schedule", "RestartSec=10"),
    ],
)
def test_template_restartsec_matches_daemon(daemon, expected):
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert expected in tmpl


@pytest.mark.parametrize(
    "daemon,expected_exec_tail",
    [
        ("gateway", "gateway daemon"),
        ("heartbeat", "heartbeat daemon 300"),
        ("schedule", "schedule daemon"),
    ],
)
def test_template_execstart_shape(daemon, expected_exec_tail):
    tmpl = (TEMPLATE_DIR / f"metasphere-{daemon}.service").read_text()
    assert f"ExecStart=@@METASPHERE_VENV_BIN@@ {expected_exec_tail}" in tmpl


# ---------------------------------------------------------------------------
# install.sh source-level greps
# ---------------------------------------------------------------------------


def test_install_sh_renders_three_split_daemons():
    src = INSTALL_SH.read_text()
    assert 'for daemon in gateway heartbeat schedule' in src, (
        "install.sh should iterate over the three split daemons"
    )
    assert "@@METASPHERE_DIR@@" in src
    assert "@@METASPHERE_VENV_BIN@@" in src


def test_install_sh_codex_hook_migration_is_user_scoped_and_handler_granular():
    """Installer must not delete custom siblings from a mixed hook group."""
    src = INSTALL_SH.read_text()
    start = src.index("seed_codex_hooks()")
    end = src.index("seed_runtime_integration()", start)
    block = src[start:end]
    assert 'local target="$HOME/.codex"' in block
    assert "UserPromptSubmit" in block
    assert "additionalContextLimit: 12000" in block
    # Filter handlers inside each group, then discard only empty groups. The
    # former whole-group `select(any(...))` form erased custom sibling hooks.
    assert 'def strip($sub; $exact): groups | map(' in block
    assert '.hooks |= map(select((is_ms($sub; $exact)) | not))' in block
    assert "select((.hooks | length) > 0)" in block
    assert "select(any(.hooks[]?" not in block


def test_install_sh_codex_hook_commands_quote_space_paths():
    """Fresh installs must produce one shell word for a spaced venv path."""
    src = INSTALL_SH.read_text()
    start = src.index("seed_codex_hooks()")
    end = src.index("seed_runtime_integration()", start)
    block = src[start:end]
    assert "printf -v hook_bin_quoted '%q' \"$hook_bin\"" in block
    assert 'context_path="$hook_bin_quoted hooks context"' in block
    assert 'posthook_path="$hook_bin_quoted hooks posthook"' in block

    spaced = "/tmp/metasphere install/venv/bin/metasphere"
    rendered = subprocess.run(
        ["bash", "-c", "printf -v q '%q' \"$1\"; printf '%s' \"$q hooks context\"", "_", spaced],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    argv = subprocess.run(
        ["bash", "-c", "eval 'set -- ' \"$1\"; printf '%s\\n' \"$#\" \"$1\" \"$2\" \"$3\"", "_", rendered],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert argv == ["3", spaced, "hooks", "context"]


def test_install_sh_phases_out_obsolete_omnibus():
    src = INSTALL_SH.read_text()
    # The omnibus metasphere.service shipped a non-existent CLI verb
    # and is superseded by the three split daemons. install.sh must
    # detect + remove it.
    assert "obsolete_omnibus" in src or "metasphere.service" in src
    assert 'systemctl --user stop metasphere.service' in src
    assert 'systemctl --user disable metasphere.service' in src


def test_install_sh_does_not_emit_omnibus_unit():
    """Regression: install.sh used to write an omnibus
    metasphere.service with ExecStart=metasphere run. The split
    rewrite should not leave that heredoc behind."""
    src = INSTALL_SH.read_text()
    assert "ExecStart=$METASPHERE_DIR/bin/metasphere run" not in src


def test_install_sh_does_not_auto_restart_active_units():
    """Cascade safety: restarting metasphere-gateway tears down all
    agent tmux sessions. Re-running install.sh on a host with active
    agents must surface a maintenance-window notice rather than
    silently restart."""
    src = INSTALL_SH.read_text()
    assert "restart_pending" in src
    assert "maintenance window" in src.lower()


# ---------------------------------------------------------------------------
# Subprocess render: idempotency + obsolete removal
# ---------------------------------------------------------------------------


def _run_render(
    tmp_home: Path,
    metasphere_dir: Path,
    *,
    premake_omnibus: bool = False,
    runtime: str = "claude",
):
    """Drive the install.sh render block in isolation against a
    sandbox HOME + METASPHERE_DIR. Stubs systemctl so no real systemd
    interaction occurs.

    Returns (stdout, stderr, returncode, rendered_unit_dir).
    """
    service_dir = tmp_home / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    metasphere_dir.mkdir(parents=True, exist_ok=True)

    if premake_omnibus:
        (service_dir / "metasphere.service").write_text(
            "[Unit]\nDescription=stale omnibus\n[Service]\nExecStart=/bin/true\n"
        )

    bin_dir = tmp_home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").chmod(0o755)

    # Driver script: source the relevant variable assignments install.sh
    # expects, then inline the render block. We avoid sourcing
    # install.sh directly because it runs a full setup pipeline.
    driver = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        export PATH="{bin_dir}:$PATH"
        export HOME="{tmp_home}"
        METASPHERE_DIR="{metasphere_dir}"
        AGENT_RUNTIME="{runtime}"
        SCRIPT_DIR="{REPO_ROOT}"

        ok()   {{ echo "[ok] $*"; }}
        warn() {{ echo "[warn] $*"; }}
        info() {{ echo "[info] $*"; }}
        err()  {{ echo "[err] $*"; exit 1; }}

        INTERACTIVE=false

        # Extract the systemd-mode body from setup_daemon_linux. We
        # replicate the relevant block here rather than invoking the
        # full install.sh pipeline; the block is grep-tested for
        # structural invariants in test_install_sh_*.
        service_dir="$HOME/.config/systemd/user"
        template_dir="$SCRIPT_DIR/systemd/user"
        venv_bin="$METASPHERE_DIR/venv/bin/metasphere"
        mkdir -p "$service_dir" "$METASPHERE_DIR/logs"

        obsolete_omnibus="$service_dir/metasphere.service"
        if [[ -f "$obsolete_omnibus" ]]; then
            systemctl --user stop metasphere.service 2>/dev/null || true
            systemctl --user disable metasphere.service 2>/dev/null || true
            rm -f "$obsolete_omnibus"
            ok "Removed obsolete metasphere.service"
        fi

        rendered_any=false
        for daemon in gateway heartbeat schedule; do
            tmpl="$template_dir/metasphere-$daemon.service"
            out="$service_dir/metasphere-$daemon.service"
            tmp=$(mktemp)
            sed \\
                -e "s|@@METASPHERE_DIR@@|$METASPHERE_DIR|g" \\
                -e "s|@@METASPHERE_PROJECT_ROOT@@|$SCRIPT_DIR|g" \\
                -e "s|@@METASPHERE_VENV_BIN@@|$venv_bin|g" \\
                -e "s|@@METASPHERE_AGENT_RUNTIME@@|$AGENT_RUNTIME|g" \\
                "$tmpl" > "$tmp"
            if [[ ! -f "$out" ]] || ! cmp -s "$tmp" "$out"; then
                mv "$tmp" "$out"
                rendered_any=true
                ok "Rendered metasphere-$daemon.service"
            else
                rm -f "$tmp"
            fi
        done

        echo "RENDERED_ANY=$rendered_any"
    """)
    driver_path = tmp_home / "drive.sh"
    driver_path.write_text(driver)
    driver_path.chmod(0o755)

    result = subprocess.run(
        ["bash", str(driver_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_home)},
    )
    return result.stdout, result.stderr, result.returncode, service_dir


def test_render_writes_three_units(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"
    stdout, stderr, rc, service_dir = _run_render(home, metasphere_dir)
    assert rc == 0, f"render failed: {stderr}"
    for daemon in DAEMONS:
        unit = service_dir / f"metasphere-{daemon}.service"
        assert unit.is_file(), f"{unit} not rendered"
        body = unit.read_text()
        # Substituted: no placeholders survive.
        assert "@@" not in body
        assert f"{metasphere_dir}/venv/bin/metasphere" in body
        assert f"METASPHERE_DIR={metasphere_dir}" in body
        assert "METASPHERE_AGENT_RUNTIME=claude" in body
        assert "Restart=always" in body
        # Operator paths from the host running tests must not bleed in.
        assert "rage-substrate" not in body


def test_render_threads_codex_runtime_to_all_units(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"
    _, stderr, rc, service_dir = _run_render(
        home, metasphere_dir, runtime="codex"
    )
    assert rc == 0, stderr
    for daemon in DAEMONS:
        body = (service_dir / f"metasphere-{daemon}.service").read_text()
        assert "METASPHERE_AGENT_RUNTIME=codex" in body


def test_render_is_idempotent(tmp_path):
    """Re-rendering with identical inputs produces no file content
    changes, so a second install.sh run is a no-op (no spurious
    daemon-reload / restart prompts)."""
    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"

    _, _, rc1, service_dir = _run_render(home, metasphere_dir)
    assert rc1 == 0
    snapshot = {
        d: (service_dir / f"metasphere-{d}.service").read_bytes() for d in DAEMONS
    }

    stdout2, stderr2, rc2, _ = _run_render(home, metasphere_dir)
    assert rc2 == 0, stderr2
    for d in DAEMONS:
        assert (service_dir / f"metasphere-{d}.service").read_bytes() == snapshot[d]
    # Second run reports rendered_any=false (no overwrites).
    assert "RENDERED_ANY=false" in stdout2


def test_render_phases_out_obsolete_omnibus(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"
    stdout, stderr, rc, service_dir = _run_render(
        home, metasphere_dir, premake_omnibus=True
    )
    assert rc == 0, stderr
    assert not (service_dir / "metasphere.service").exists(), (
        "obsolete metasphere.service should be removed"
    )
    assert "Removed obsolete metasphere.service" in stdout


def test_render_overwrites_drifted_unit(tmp_path):
    """Hand-written split units (e.g., spot's pre-template state)
    must be overwritten cleanly — templates are the source of truth."""
    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"
    service_dir = home / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)
    drifted = service_dir / "metasphere-gateway.service"
    drifted.write_text(
        "[Unit]\nDescription=hand-written drift\n[Service]\n"
        "ExecStart=/home/someone/.venv/bin/metasphere gateway daemon 5\n"
        "Restart=on-failure\n"
    )

    stdout, stderr, rc, _ = _run_render(home, metasphere_dir)
    assert rc == 0, stderr
    body = drifted.read_text()
    assert "Restart=always" in body
    assert "Restart=on-failure" not in body
    assert "/home/someone" not in body


# ---------------------------------------------------------------------------
# Optional: systemd-analyze verify (skipped if unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("daemon", DAEMONS)
def test_rendered_unit_passes_systemd_analyze_verify(tmp_path, daemon):
    """Smoke test that rendered units parse cleanly under
    systemd-analyze verify. Skipped where systemd-analyze isn't on
    PATH (CI minimal images, macOS)."""
    if subprocess.run(
        ["which", "systemd-analyze"], capture_output=True
    ).returncode != 0:
        pytest.skip("systemd-analyze not available")

    home = tmp_path / "home"
    home.mkdir()
    metasphere_dir = home / ".metasphere"
    _, stderr, rc, service_dir = _run_render(home, metasphere_dir)
    assert rc == 0, stderr

    unit = service_dir / f"metasphere-{daemon}.service"
    result = subprocess.run(
        ["systemd-analyze", "verify", "--user", str(unit)],
        capture_output=True,
        text=True,
    )
    # systemd-analyze emits warnings for things like unreachable
    # ExecStart paths in the sandbox; we only fail on hard parse
    # errors (non-zero exit + "Failed" in stderr).
    if result.returncode != 0 and "Failed" in result.stderr:
        pytest.fail(
            f"systemd-analyze verify failed for {unit}:\n{result.stderr}"
        )
