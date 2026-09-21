#!/bin/bash
#
# Metasphere Agents - persistent multi-agent orchestration
# One-line installer: curl -fsSL https://raw.githubusercontent.com/julianfleck/metasphere-agents/main/install.sh | bash
#
# Options:
#   -y                     Non-interactive mode (use defaults/env vars)
#   -v                     Verbose output
#   --no-migrate-<name>    Skip the matching subdir under migrate/.
#                          E.g. --no-migrate-openclaw to skip the
#                          OpenClaw precursor import even when it's
#                          detected on disk. See migrate/README.md.
#
# Environment variables (for non-interactive):
#   TELEGRAM_BOT_TOKEN    - Telegram bot token
#   METASPHERE_DIR        - Installation directory (default: ~/.metasphere)
#   METASPHERE_AGENT_RUNTIME - Agent REPL: claude (default) or codex
#
set -e

REPO="julianfleck/metasphere-agents"
METASPHERE_DIR="${METASPHERE_DIR:-$HOME/.metasphere}"
AGENT_RUNTIME="${METASPHERE_AGENT_RUNTIME:-claude}"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo ".")"
INTERACTIVE=true
VERBOSE=false

# Parse arguments — manual loop so we can mix short flags (-y, -v) and
# the generic ``--no-migrate-<name>`` family (one flag per migration
# subdir under ``migrate/``). Each ``--no-migrate-<name>`` sets the
# env var ``MIGRATE_<NAME_UPPERCASED>=false``, which run_migrations()
# checks before invoking the corresponding migrate script.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y) INTERACTIVE=false; shift ;;
        -v) VERBOSE=true; shift ;;
        --no-migrate-*)
            _name="${1#--no-migrate-}"
            _upper=$(printf '%s' "$_name" | tr '[:lower:]-' '[:upper:]_')
            printf -v "MIGRATE_${_upper}" '%s' "false"
            export "MIGRATE_${_upper}"
            shift
            ;;
        --) shift; break ;;
        *) shift ;;
    esac
done
unset _name _upper

# Detect if stdin is terminal
[[ ! -t 0 ]] && INTERACTIVE=false

# Colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    DIM='\033[2m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' CYAN='' DIM='' NC=''
fi

ok() { echo -e "${GREEN}[ok]${NC} $*"; }
info() { echo -e "${CYAN}[..]${NC} $*"; }
warn() { echo -e "${YELLOW}[!!]${NC} $*"; }
err() { echo -e "${RED}[error]${NC} $*"; exit 1; }

# seed_or_drift_check SRC DEST LABEL
# Idempotent template seeder with drift handling:
#   - dest missing  → cp src to dest, log "Seeded LABEL"
#   - sha256 match  → silent skip (operator's copy = shipped)
#   - sha256 differ + non-interactive (-y or no tty)
#                   → WARN "TEMPLATE DRIFT: ..." and skip
#   - sha256 differ + interactive
#                   → prompt (k)eep / (o)verwrite / (d)iff?
#                       k → silent skip with "Kept local LABEL"
#                       o → backup dest to dest.bak-<unix>, then cp src
#                       d → diff -u local shipped (paged), re-prompt
seed_or_drift_check() {
    local src="$1" dest="$2" label="$3"
    [[ -f "$src" ]] || return 0
    if [[ ! -f "$dest" ]]; then
        cp "$src" "$dest"
        ok "Seeded $label"
        return 0
    fi
    local src_hash dest_hash
    src_hash=$(sha256sum "$src" | cut -d' ' -f1)
    dest_hash=$(sha256sum "$dest" | cut -d' ' -f1)
    if [[ "$src_hash" == "$dest_hash" ]]; then
        return 0
    fi
    if [[ "$INTERACTIVE" != "true" ]]; then
        warn "TEMPLATE DRIFT: $label differs from shipped (run 'metasphere update --templates' to opt in)"
        return 0
    fi
    while true; do
        local src_lines dest_lines
        src_lines=$(wc -l < "$src")
        dest_lines=$(wc -l < "$dest")
        echo
        warn "TEMPLATE DRIFT: $label"
        echo "  shipped:  $src ($src_lines lines)"
        echo "  local:    $dest ($dest_lines lines)"
        local choice
        read -rp "  shipped template differs. (k)eep mine [default], (o)verwrite, (d)iff? " choice
        case "${choice:-k}" in
            k|K)
                ok "Kept local $label"
                return 0
                ;;
            o|O)
                local backup="$dest.bak-$(date +%s)"
                cp "$dest" "$backup"
                cp "$src" "$dest"
                ok "Overwrote $label (backup at $backup)"
                return 0
                ;;
            d|D)
                diff -u "$dest" "$src" | ${PAGER:-less -R}
                ;;
            *)
                echo "  invalid choice; expected k, o, or d"
                ;;
        esac
    done
}

echo "Metasphere Agents"
echo "================="
echo "Multi-agent orchestration for $AGENT_RUNTIME"
echo

# =============================================================================
# Dependency checks
# =============================================================================

check_dependencies() {
    info "Checking dependencies..."

    # Git
    if command -v git &>/dev/null; then
        ok "git"
    else
        err "git required - install from https://git-scm.com"
    fi

    # jq
    if command -v jq &>/dev/null; then
        ok "jq"
    else
        warn "jq not found - installing..."
        if [[ "$(uname)" == "Darwin" ]]; then
            brew install jq || err "Failed to install jq"
        elif command -v apt-get &>/dev/null; then
            sudo apt-get install -y jq || err "Failed to install jq"
        else
            err "Please install jq manually"
        fi
        ok "jq installed"
    fi

    # curl
    if command -v curl &>/dev/null; then
        ok "curl"
    else
        err "curl required"
    fi

    # python3 + python3-venv (for $METASPHERE_DIR/venv). On Debian/
    # Ubuntu, the `ensurepip` module ships separately via python3-venv,
    # so `python3 -m venv` fails on minimal installs even though
    # python3 itself is present.
    if ! command -v python3 &>/dev/null; then
        err "python3 required"
    fi
    if ! python3 -c "import ensurepip" &>/dev/null; then
        err_no_exit "python3-venv not available — metasphere needs it for its dedicated venv"
        local py_minor
        py_minor=$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo "")
        echo "    On Debian/Ubuntu:"
        if [[ -n "$py_minor" ]]; then
            echo "      sudo apt install python3-venv python3.${py_minor}-venv"
        else
            echo "      sudo apt install python3-venv"
        fi
        echo "    On Alpine: apk add py3-virtualenv"
        echo "    Then re-run this installer."
        exit 1
    fi
    ok "python3 + python3-venv"

    # Agent runtime CLI. Keep provider probes local and non-mutating: Codex
    # exposes an auth-status command, while Claude requires a short headless
    # request to distinguish an installed-but-logged-out CLI.
    case "$AGENT_RUNTIME" in
    claude)
      if command -v claude &>/dev/null; then
        local claude_version=$(claude --version 2>/dev/null | head -1 || echo "unknown")
        ok "claude CLI ($claude_version)"

        # Test if Claude is authenticated with a quick probe
        info "Testing Claude Code authentication..."
        local test_response=$(echo "reply with just 'ok'" | timeout 30 claude -p 2>&1 || echo "FAILED")
        if [[ "$test_response" == *"ok"* ]] || [[ "$test_response" == *"Ok"* ]] || [[ "$test_response" == *"OK"* ]]; then
            ok "Claude Code authenticated"
        elif [[ "$test_response" == *"FAILED"* ]] || [[ "$test_response" == *"error"* ]] || [[ "$test_response" == *"login"* ]]; then
            warn "Claude Code may not be authenticated"
            echo "    Run: claude /login"
            echo "    Then re-run this installer"
            if $INTERACTIVE; then
                read -p "Continue anyway? [y/N] " -n 1 -r
                echo
                [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
            fi
        else
            ok "Claude Code responding"
        fi
      else
        warn "claude CLI not found"
        echo "    Install from: https://claude.ai/code"
        echo "    Metasphere requires Claude Code for agent execution"
        if $INTERACTIVE; then
            read -p "Continue without Claude? [y/N] " -n 1 -r
            echo
            [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
        fi
      fi
      ;;
    codex)
      if command -v codex &>/dev/null; then
        local codex_version
        codex_version=$(codex --version 2>/dev/null | head -1 || echo "unknown")
        ok "codex CLI ($codex_version)"
        if codex login status &>/dev/null; then
            ok "Codex authenticated"
        else
            warn "Codex may not be authenticated"
            echo "    Run: codex login"
            if $INTERACTIVE; then
                read -p "Continue anyway? [y/N] " -n 1 -r
                echo
                [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
            fi
        fi
      else
        warn "codex CLI not found"
        echo "    Install Codex CLI, then run: codex login"
        if $INTERACTIVE; then
            read -p "Continue without Codex? [y/N] " -n 1 -r
            echo
            [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
        fi
      fi
      ;;
    *)
      err "Unsupported METASPHERE_AGENT_RUNTIME '$AGENT_RUNTIME' (expected claude or codex)"
      ;;
    esac

    # CAM (Collective Agent Memory)
    if command -v cam &>/dev/null; then
        local cam_version=$(cam --version 2>/dev/null || echo "unknown")
        ok "CAM ($cam_version)"
    else
        warn "CAM not found - installing..."
        if command -v uv &>/dev/null; then
            uv tool install git+https://github.com/julianfleck/collective-agent-memory.git 2>&1 | tail -3
        elif command -v pipx &>/dev/null; then
            pipx install git+https://github.com/julianfleck/collective-agent-memory.git 2>&1 | tail -3
        elif command -v pip3 &>/dev/null; then
            pip3 install --user git+https://github.com/julianfleck/collective-agent-memory.git 2>&1 | tail -3
        else
            err "pip/pipx/uv required to install CAM"
        fi
        ok "CAM installed"
    fi

    # systemd (user instance) — preferred for the gateway/heartbeat/schedule
    # daemons since it gives them auto-restart and boot persistence. When
    # unavailable (Docker minimal image, Alpine, WSL without systemd, or
    # an install run as root with no user session), we fall back to
    # nohup+disown launch. Daemons still work but don't auto-restart on
    # reboot and die when the launching shell exits.
    #
    # ``METASPHERE_NO_SYSTEMD`` is exported for ``setup_daemon_linux`` so
    # it picks the right branch.
    if [[ -n "${METASPHERE_SKIP_SYSTEMD:-}" ]]; then
        warn "systemd check skipped (METASPHERE_SKIP_SYSTEMD set)"
        export METASPHERE_NO_SYSTEMD=1
    elif ! command -v systemctl &>/dev/null; then
        warn "systemctl not found — using nohup fallback for daemons"
        echo "    Daemons won't auto-start on reboot. On a host with systemd"
        echo "    (most Linux VMs), install.sh will pick it up automatically."
        export METASPHERE_NO_SYSTEMD=1
    elif ! systemctl --user list-units &>/dev/null; then
        warn "systemctl --user isn't responsive — using nohup fallback"
        echo "    If you want auto-restart: enable user lingering as root —"
        echo "         sudo loginctl enable-linger \$(whoami)"
        echo "    then log out, log back in, and re-run this installer."
        export METASPHERE_NO_SYSTEMD=1
    else
        ok "systemd (user instance available)"
    fi

    echo
}

# Like err() but doesn't exit — caller prints more context and exits itself.
err_no_exit() {
    echo -e "${RED}✗${NC} $1"
}

# =============================================================================
# Directory setup
# =============================================================================

setup_directories() {
    info "Setting up directories..."

    mkdir -p "$METASPHERE_DIR"/{config,agents,telegram/stream,logs}
    mkdir -p "$METASPHERE_DIR/agents/@orchestrator"

    # Set permissions
    chmod 700 "$METASPHERE_DIR/config"

    # Seed default auto-update.env on FRESH installs only — preserves any
    # operator-tuned setting on re-runs. Default: enabled, daily at 4am.
    if [[ ! -f "$METASPHERE_DIR/config/auto-update.env" ]]; then
        cat > "$METASPHERE_DIR/config/auto-update.env" <<'EOF'
# metasphere auto-update configuration
# Managed by `metasphere update --enable|--disable`.
AUTO_UPDATE_ENABLED=true
AUTO_UPDATE_INTERVAL=daily
AUTO_UPDATE_BRANCH=main
AUTO_UPDATE_RESTART_DAEMONS=true
AUTO_UPDATE_NOTIFY=true
EOF
        chmod 600 "$METASPHERE_DIR/config/auto-update.env"
        ok "Seeded default auto-update.env (daily, enabled)"
    fi

    # Seed ~/.metasphere/CLAUDE.md from the shipped user template.
    # Auto-loaded by Claude Code at orchestrator session start when CWD
    # is ~/.metasphere/. Drift-aware: skips on match, prompts on
    # divergence (interactive), or warns + skips (non-interactive).
    seed_or_drift_check \
        "$SCRIPT_DIR/templates/install/CLAUDE.md" \
        "$METASPHERE_DIR/CLAUDE.md" \
        "~/.metasphere/CLAUDE.md"

    # Seed ~/.metasphere/ADDRESSBOOK.yaml.
    #
    # Three branches:
    #   1. ADDRESSBOOK.yaml already exists → no-op (operator's edits
    #      preserved across re-runs).
    #   2. Legacy ~/.metasphere/config/telegram_contacts.json present
    #      → migrate it: each {name: chat_id} entry becomes
    #      contacts.<name>.telegram: <chat_id> in the YAML form. Old
    #      file kept in place; metasphere/contacts.py prefers the
    #      YAML at lookup time but falls back with a deprecation WARN
    #      if YAML is missing.
    #   3. Neither present + template ships → seed the empty stub.
    local addressbook="$METASPHERE_DIR/ADDRESSBOOK.yaml"
    local legacy_contacts="$METASPHERE_DIR/config/telegram_contacts.json"
    if [[ ! -f "$addressbook" ]]; then
        if [[ -f "$legacy_contacts" ]] && command -v python3 &>/dev/null; then
            python3 - "$legacy_contacts" "$addressbook" <<'PYEOF'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
try:
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError) as e:
    print(f"could not read {src}: {e}", file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict):
    print(f"unexpected shape in {src} (not a JSON object)", file=sys.stderr)
    sys.exit(1)
lines = ["# Migrated from legacy ~/.metasphere/config/telegram_contacts.json",
         "# at install time. Edit freely."]
# No default-recipient is written here — the operator sets one
# explicitly after install by adding `default-recipient: <name>`
# at the top of this file.
lines.append("contacts:")
for name, chat_id in data.items():
    lines.append(f"  {name}:")
    lines.append(f"    telegram: {chat_id}")
with open(dst, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PYEOF
            chmod 600 "$addressbook"
            ok "Migrated ~/.metasphere/config/telegram_contacts.json -> ~/.metasphere/ADDRESSBOOK.yaml"
        elif [[ -f "$SCRIPT_DIR/templates/install/ADDRESSBOOK.yaml.template" ]]; then
            cp "$SCRIPT_DIR/templates/install/ADDRESSBOOK.yaml.template" "$addressbook"
            chmod 600 "$addressbook"
            ok "Seeded ~/.metasphere/ADDRESSBOOK.yaml from template"
        fi
    fi

    # Seed ~/.metasphere/teams.yaml — the central agent→projects
    # roster consumed by the project-capsule resolver (B7). No-op
    # when the file already exists so operator edits are preserved
    # across re-runs.
    local teams_config="$METASPHERE_DIR/teams.yaml"
    if [[ ! -f "$teams_config" && -f "$SCRIPT_DIR/templates/install/teams.yaml" ]]; then
        cp "$SCRIPT_DIR/templates/install/teams.yaml" "$teams_config"
        chmod 644 "$teams_config"
        ok "Seeded ~/.metasphere/teams.yaml from template"
    fi

    ok "Created $METASPHERE_DIR"
}

# =============================================================================
# Install scripts
# =============================================================================

install_scripts() {
    info "Installing scripts..."

    local BIN_DIR="$METASPHERE_DIR/bin"
    local VENV_DIR="$METASPHERE_DIR/venv"
    mkdir -p "$BIN_DIR"

    # Create / reuse a dedicated venv under $METASPHERE_DIR/venv.
    # Avoids PEP 668 errors on Debian 12+ / Python 3.12+ hosts and
    # keeps metasphere isolated from the system Python. The venv
    # location is stable across reinstalls, so the pip install is
    # editable against the source tree and subsequent git-pulls
    # (metasphere update) pick up changes without re-venv-ing.
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        info "Creating metasphere venv at $VENV_DIR..."
        if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
            err "Failed to create venv. On Debian/Ubuntu: apt install python3-venv"
        fi
        ok "Created venv"
    else
        ok "Reusing existing venv at $VENV_DIR"
    fi

    # Install the unified Python CLI entry point INTO the venv.
    # The single `metasphere` binary dispatches all subcommands via
    # metasphere.cli.main. Individual metasphere-* console scripts
    # (declared in pyproject.toml [project.scripts]) are installed
    # alongside it by pip; only `metasphere` itself is symlinked
    # into BIN_DIR below. The bash era left one script in scripts/
    # (`metasphere-reaper`, run by systemd timer for npm-root-g
    # cleanup); everything else routes through the Python CLI.
    #
    # --no-warn-script-location: pip whines that the venv's bin isn't
    # on PATH globally, but we symlink the single `metasphere` entry
    # into BIN_DIR below, which IS on PATH (via setup_path). Silence
    # the noise.
    if [[ -d "$SCRIPT_DIR" ]]; then
        "$VENV_DIR/bin/pip" install -e "$SCRIPT_DIR" -q \
            --no-warn-script-location 2>&1 | tail -3 || true
    fi

    # Surface the optional voice-transcription extra. We intentionally do
    # NOT auto-install it — the faster-whisper wheel is heavyweight (CUDA
    # runtime + model download on first use) and many operators don't
    # care about voice notes. Just nudge.
    if ! "$VENV_DIR/bin/pip" show faster-whisper &>/dev/null; then
        info "Voice transcription optional. Install with: pip install metasphere-agents[voice]"
    fi

    # Ensure the unified binary exists in BIN_DIR. Prefer the venv
    # entry point; fall back to legacy locations for already-set-up
    # hosts before we gain the venv.
    local pip_bin=""
    for candidate in \
        "$VENV_DIR/bin/metasphere" \
        "${VIRTUAL_ENV:-/nonexistent}/bin/metasphere" \
        "$HOME/.local/bin/metasphere"; do
        if [[ -x "$candidate" ]]; then
            pip_bin="$candidate"
            break
        fi
    done
    if [[ -n "$pip_bin" ]]; then
        ln -sfn "$pip_bin" "$BIN_DIR/metasphere"
    else
        cat > "$BIN_DIR/metasphere" << 'SHIM'
#!/bin/bash
exec python3 -m metasphere.cli.main "$@"
SHIM
        chmod +x "$BIN_DIR/metasphere"
    fi
    ok "Installed unified metasphere CLI → $BIN_DIR/metasphere"

    # Clean up legacy standalone shims (messages, tasks) — everything
    # routes through `metasphere msg` / `metasphere task` now.
    rm -f "$BIN_DIR/messages" "$BIN_DIR/tasks"

    # Remove stale individual metasphere-* symlinks from previous installs.
    for f in "$BIN_DIR"/metasphere-*; do
        local name
        name=$(basename "$f")
        case "$name" in
            metasphere-fts) ;; # keep standalone FTS tool
            *) rm -f "$f" ;;
        esac
    done
    # Remove stale .bak files.
    rm -f "$BIN_DIR"/*.bak "$BIN_DIR"/README.md 2>/dev/null || true

    # Configure PATH in shell profile
    setup_path "$BIN_DIR"
}

setup_path() {
    local BIN_DIR="$1"

    # Already in PATH?
    if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
        ok "PATH already configured"
        return
    fi

    # Detect shell and profile
    local shell_name=$(basename "$SHELL")
    local profile=""

    case "$shell_name" in
        zsh)
            profile="$HOME/.zshrc"
            ;;
        bash)
            if [[ -f "$HOME/.bash_profile" ]]; then
                profile="$HOME/.bash_profile"
            else
                profile="$HOME/.bashrc"
            fi
            ;;
        fish)
            profile="$HOME/.config/fish/config.fish"
            ;;
        *)
            profile="$HOME/.profile"
            ;;
    esac

    # Check if already added to profile
    if [[ -f "$profile" ]] && grep -q "metasphere/bin" "$profile" 2>/dev/null; then
        ok "PATH entry exists in $profile"
        return
    fi

    # Add to profile
    local path_line=""
    if [[ "$shell_name" == "fish" ]]; then
        path_line="set -gx PATH $BIN_DIR \$PATH"
    else
        path_line="export PATH=\"$BIN_DIR:\$PATH\""
    fi

    if $INTERACTIVE; then
        read -p "Add metasphere to PATH in $profile? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            echo "" >> "$profile"
            echo "# Metasphere Agents" >> "$profile"
            echo "$path_line" >> "$profile"
            ok "Added to $profile"
            echo "    Run: source $profile (or restart shell)"
        else
            warn "Skipped PATH setup"
            echo "    Add manually: $path_line"
        fi
    else
        # Non-interactive: add automatically
        echo "" >> "$profile"
        echo "# Metasphere Agents" >> "$profile"
        echo "$path_line" >> "$profile"
        ok "Added to $profile"
    fi

    # Also export for current session
    export PATH="$BIN_DIR:$PATH"
}

# =============================================================================
# Generic migrations dispatcher
# =============================================================================
#
# Walks ``$SCRIPT_DIR/migrate/*/``. Each subdir is a self-contained
# migration source with two executables:
#
#   detect.sh   — exit 0 if the source is present on this host.
#   migrate.sh  — perform the import. Inherits METASPHERE_DIR,
#                 INTERACTIVE, VERBOSE from this script.
#
# Detection is silent. On a hit, prompt ``Migrate from <name>? [Y/n]``
# in interactive mode (default Yes); proceed by default in -y mode.
# Pass ``--no-migrate-<name>`` to skip a specific source regardless of
# detection (the arg parser at the top sets MIGRATE_<NAME>=false).
#
# Adding a migration: drop a sibling subdir under migrate/. See
# migrate/README.md for the contract.

run_migrations() {
    local migrate_dir="$SCRIPT_DIR/migrate"
    [[ -d "$migrate_dir" ]] || return 0

    local source_dir source_name detect migrate skip_var
    shopt -s nullglob
    for source_dir in "$migrate_dir"/*/; do
        source_name=$(basename "$source_dir")
        detect="${source_dir}detect.sh"
        migrate="${source_dir}migrate.sh"

        # Both scripts must exist + be executable for the dispatcher
        # to consider this a valid migration source.
        [[ -x "$detect" && -x "$migrate" ]] || continue

        # --no-migrate-<name> short-circuit, regardless of detection.
        skip_var="MIGRATE_$(printf '%s' "$source_name" | tr '[:lower:]-' '[:upper:]_')"
        if [[ "${!skip_var:-true}" == "false" ]]; then
            continue
        fi

        # Run detection in a subshell so its env / set -e flags don't
        # bleed into install.sh. Stdout/stderr suppressed by contract.
        if ! ( bash "$detect" ) >/dev/null 2>&1; then
            continue
        fi

        echo
        info "Migration source detected: $source_name"
        local do_migrate=true
        if $INTERACTIVE; then
            read -p "Migrate from $source_name? [Y/n] " -n 1 -r
            echo
            [[ $REPLY =~ ^[Nn]$ ]] && do_migrate=false
        fi

        if $do_migrate; then
            if METASPHERE_DIR="$METASPHERE_DIR" \
                    INTERACTIVE="$INTERACTIVE" \
                    VERBOSE="$VERBOSE" \
                    bash "$migrate"; then
                ok "Migration from $source_name complete"
            else
                warn "Migration from $source_name failed (continuing)"
            fi
        else
            info "Skipped $source_name migration"
        fi
    done
    shopt -u nullglob
}

# =============================================================================
# CAM (Collective Agent Memory)
# =============================================================================
#
# Two responsibilities:
#   1. Ensure the `cam` binary is installed and on PATH (idempotent).
#   2. Make the user's existing CAM data dir (~/.cam) reachable so we don't
#      re-index.

CAM_BIN=""

find_cam_bin() {
    # Standard PATH lookup first
    if command -v cam &>/dev/null; then
        CAM_BIN=$(command -v cam)
        return 0
    fi
    # Common pip/pipx install locations not always on PATH
    for candidate in "$HOME/.local/bin/cam" /usr/local/bin/cam /opt/homebrew/bin/cam; do
        if [[ -x "$candidate" ]]; then
            CAM_BIN="$candidate"
            return 0
        fi
    done
    return 1
}

install_cam() {
    if find_cam_bin; then
        ok "CAM already installed: $CAM_BIN"
        return 0
    fi

    info "CAM not found - installing collective-agent-memory..."

    # Prefer pipx (isolated env), fall back to pip --user
    if command -v pipx &>/dev/null; then
        if pipx install collective-agent-memory 2>&1 | tail -5; then
            find_cam_bin && ok "CAM installed via pipx ($CAM_BIN)" || warn "pipx install reported success but cam not found"
        else
            warn "pipx install failed - try manually: pipx install collective-agent-memory"
        fi
    elif command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
        local pip_cmd
        pip_cmd=$(command -v pip3 || command -v pip)
        if "$pip_cmd" install --user collective-agent-memory 2>&1 | tail -5; then
            find_cam_bin && ok "CAM installed via pip --user ($CAM_BIN)" || warn "pip install reported success but cam not found"
        else
            warn "pip install failed - try manually: $pip_cmd install --user collective-agent-memory"
        fi
    else
        warn "Neither pipx nor pip available - install Python first, then: pipx install collective-agent-memory"
    fi
}

migrate_cam_data() {
    # If ~/.cam already exists, no re-index is needed — CAM will pick
    # up the existing index in place.
    if [[ -d "$HOME/.cam" ]]; then
        local size
        size=$(du -sh "$HOME/.cam" 2>/dev/null | cut -f1)
        ok "CAM data dir present: $HOME/.cam ($size) — no re-index needed"
    fi
    return 0
}

# =============================================================================
# Telegram configuration
# =============================================================================

setup_telegram() {
    echo
    echo "Telegram Bot Setup"
    echo "------------------"

    local token_file="$METASPHERE_DIR/config/telegram.env"
    local existing_token=""
    local verified_bot=""

    # Check if token already set (possibly from migration)
    if [[ -f "$token_file" ]] && grep -q "TELEGRAM_BOT_TOKEN=" "$token_file"; then
        source "$token_file"
        existing_token="$TELEGRAM_BOT_TOKEN"
    fi

    # Also check environment variable
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -z "$existing_token" ]]; then
        existing_token="$TELEGRAM_BOT_TOKEN"
    fi

    # If we have a token, verify it
    if [[ -n "$existing_token" ]]; then
        echo "  Found token: ${existing_token:0:10}...${existing_token: -4}"
        verified_bot=$(curl -s "https://api.telegram.org/bot$existing_token/getMe" 2>/dev/null | jq -r '.result.username // empty')
        if [[ -n "$verified_bot" ]]; then
            ok "Token valid (bot: @$verified_bot)"
            echo "TELEGRAM_BOT_TOKEN=$existing_token" > "$token_file"
            chmod 600 "$token_file"

            if $INTERACTIVE; then
                read -p "  Keep this token? [Y/n] " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Nn]$ ]]; then
                    existing_token=""
                    verified_bot=""
                else
                    return
                fi
            else
                return
            fi
        else
            warn "Token found but invalid (API verification failed)"
            existing_token=""
        fi
    fi

    # No valid token — prompt user
    if $INTERACTIVE; then
        echo
        echo "  Metasphere uses a Telegram bot for the human interface."
        echo "  To set one up:"
        echo "    1. Message @BotFather on Telegram"
        echo "    2. Send /newbot and follow instructions"
        echo "    3. Copy the token (format: 123456789:ABCdefGHI...)"
        echo
        read -p "  Enter bot token (or press Enter to skip): " token

        if [[ -n "$token" ]]; then
            # Validate format before saving
            if [[ ! "$token" =~ ^[0-9]+: ]]; then
                warn "Token format looks wrong (expected 123456789:ABC...). Saving anyway."
            fi
            echo "TELEGRAM_BOT_TOKEN=$token" > "$token_file"
            chmod 600 "$token_file"
            ok "Telegram token saved"

            # Verify token
            local bot_info
            bot_info=$(curl -s "https://api.telegram.org/bot$token/getMe" 2>/dev/null | jq -r '.result.username // empty')
            if [[ -n "$bot_info" ]]; then
                ok "Bot verified: @$bot_info"
                echo "  Send a message to @$bot_info to complete setup"
            else
                warn "Could not verify token — check if correct"
            fi
        else
            warn "Skipped Telegram setup"
            echo "  Run later: metasphere config telegram <token>"
        fi
    else
        warn "No TELEGRAM_BOT_TOKEN — set via environment or run installer interactively"
    fi
}

# =============================================================================
# Agent setup
# =============================================================================

setup_orchestrator() {
    info "Setting up @orchestrator agent..."

    local agent_dir="$METASPHERE_DIR/agents/@orchestrator"
    mkdir -p "$agent_dir"

    # SOUL.md — starter voice. Opinionated default; edit it to make it yours.
    # The per-turn context hook injects this file's head as a "voice capsule"
    # so the agent stays in character between turns. If it reads like bland
    # corporate boilerplate, the agent's replies will too. Don't leave it bland.
    if [[ ! -f "$agent_dir/SOUL.md" ]]; then
        cat > "$agent_dir/SOUL.md" << 'EOF'
# @orchestrator

You are the orchestrator: the persistent agent at the root of this
metasphere install. New work arrives here first — from the human
operator or from scheduled jobs — and it's your call what happens
next: handle it yourself, delegate to a child agent, or escalate back.

## How you sound

Dry and direct. Not robotic, not chirpy. You have opinions and you
share them — especially when you think the user is about to make a
bad call. "That'll work, but have you considered..." is more useful
than "Great idea!".

You're comfortable being wrong. When a diagnosis turns out to be off,
say so plainly, adjust, move on. Don't backfill justifications for a
bad guess.

You keep your own counsel. Internal monologue, tool output, and
routine process detail don't belong in user-facing replies unless
something unexpected happened. The user is often reading this on a
phone — respect the screen.

## What you care about

- **Getting the thing actually working, not just looking like it
  worked.** Smoke-test your own fixes. Never ship a "should work" —
  try it.
- **Honest error reporting.** If something is broken, name it. If a
  fix is a workaround rather than a root-cause fix, flag it. The user
  can't make good decisions on polished lies.
- **Keeping state coherent.** Memory, tasks, messages, commits —
  these are the load-bearing structures. When they drift from
  reality, fix the drift before doing anything else.
- **Not being boring.** If a reply could have been written by any
  generic assistant in any repo, it's wrong. Have a point of view.

## What you are NOT

- Not a yes-man. When the user proposes something you think is
  wrong, push back with your reasoning, then do what they say if
  they confirm.
- Not a narrator of your own tool calls. "I ran X, I ran Y, I ran Z"
  is noise. Report the outcome, not the transcript.
- Not a summarizer of what you just said two paragraphs ago. Move
  forward.

---

*This file is your default personality. Edit it freely — this is
where you become yourself. The voice capsule in the per-turn context
pulls the top of this file, so the most load-bearing lines are the
first 30 or so. Put your sharpest opinions there.*
EOF
    fi

    # USER.md — scaffolding for the user to describe themselves. Without a
    # USER.md the agent has no idea who it's talking to, which flattens voice
    # into generic-assistant mode.
    if [[ ! -f "$agent_dir/USER.md" ]]; then
        cat > "$agent_dir/USER.md" << 'EOF'
# USER.md — who the orchestrator is talking to

_Fill this in. The agent reads it to calibrate how to speak with
you. Without it, you'll get generic-assistant replies._

## Name and handle

- Name:
- Preferred handle:
- Pronouns:
- Timezone:

## What you do

_One or two paragraphs. What's your role? What kind of work brings
you to this repo? What does a normal working day look like?_

## How you prefer to work with agents

_Examples:_
- _"Lead with the bottom line; I'll ask for details if I want them."_
- _"Push back when you disagree. I'd rather argue than get rubber-stamped."_
- _"Don't summarize what you just did — I can read the diff."_
- _"When something's broken, say so plainly. No softening."_

## What you don't want from agents

_Examples:_
- _"Don't open replies with 'I'll' or 'Let me' — just do the thing."_
- _"No emoji unless I use them first."_
- _"Don't recap the conversation back to me."_

## Current focus

_What are you actively working on? Update this when your focus shifts.
It gives the agent context for why you might be asking about X today._
EOF
    fi

    # AGENTS.md — runtime guidelines for @orchestrator. Per-type
    # template at templates/agents/orchestrator/AGENTS.md. Read by the
    # agent at session start (per persona-index). Drift-aware: skips on
    # match, prompts on divergence (interactive), or warns + skips
    # (non-interactive).
    seed_or_drift_check \
        "$SCRIPT_DIR/templates/agents/orchestrator/AGENTS.md" \
        "$agent_dir/AGENTS.md" \
        "@orchestrator/AGENTS.md"

    # Status
    echo "active: ready" > "$agent_dir/status"

    ok "Orchestrator initialized"
}

# =============================================================================
# Daemon setup
# =============================================================================

setup_daemon() {
    info "Setting up daemon..."

    if [[ "$(uname)" == "Darwin" ]]; then
        setup_daemon_macos
    elif [[ "$(uname)" == "Linux" ]]; then
        setup_daemon_linux
    else
        warn "Unsupported platform for daemon"
    fi
}

setup_daemon_macos() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local template_dir="$SCRIPT_DIR/launchd/user"
    local venv_bin="$METASPHERE_DIR/venv/bin/metasphere"
    local runtime="$AGENT_RUNTIME"
    local service_path="$METASPHERE_DIR/venv/bin:$METASPHERE_DIR/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"

    mkdir -p "$plist_dir" "$METASPHERE_DIR/logs"

    local obsolete="$plist_dir/com.metasphere.plist"
    if [[ -f "$obsolete" ]]; then
        launchctl bootout "gui/$(id -u)/com.metasphere" 2>/dev/null || true
        rm -f "$obsolete"
        ok "Removed obsolete com.metasphere launchd job"
    fi

    local rendered_any=false
    local daemon
    for daemon in gateway heartbeat schedule; do
        local tmpl="$template_dir/com.metasphere.$daemon.plist"
        local out="$plist_dir/com.metasphere.$daemon.plist"
        if [[ ! -f "$tmpl" ]]; then
            warn "Missing template $tmpl — skipping $daemon"
            continue
        fi
        local tmp
        tmp=$(mktemp)
        sed \
            -e "s|@@METASPHERE_DIR@@|$METASPHERE_DIR|g" \
            -e "s|@@METASPHERE_PROJECT_ROOT@@|$SCRIPT_DIR|g" \
            -e "s|@@METASPHERE_VENV_BIN@@|$venv_bin|g" \
            -e "s|@@METASPHERE_AGENT_RUNTIME@@|$runtime|g" \
            -e "s|@@METASPHERE_PATH@@|$service_path|g" \
            "$tmpl" > "$tmp"
        if ! plutil -lint "$tmp" >/dev/null; then
            rm -f "$tmp"
            warn "Invalid launchd template for $daemon — skipping"
            continue
        fi
        if [[ ! -f "$out" ]] || ! cmp -s "$tmp" "$out"; then
            mv "$tmp" "$out"
            chmod 644 "$out"
            rendered_any=true
            ok "Rendered com.metasphere.$daemon.plist"
        else
            rm -f "$tmp"
        fi
    done

    local start_now=true
    if $INTERACTIVE; then
        read -p "Start metasphere daemons now? [Y/n] " -n 1 -r
        echo
        [[ $REPLY =~ ^[Nn]$ ]] && start_now=false
    fi

    if $start_now; then
        local restart_pending=()
        for daemon in gateway heartbeat schedule; do
            local label="com.metasphere.$daemon"
            local target="gui/$(id -u)/$label"
            local plist="$plist_dir/$label.plist"
            [[ -f "$plist" ]] || continue
            if launchctl print "$target" &>/dev/null; then
                $rendered_any && restart_pending+=("$label")
            else
                launchctl bootstrap "gui/$(id -u)" "$plist"
            fi
        done
        ok "Daemons loaded (gateway, heartbeat, schedule)"
        if (( ${#restart_pending[@]} > 0 )); then
            warn "Updated launchd jobs remain on their previous definitions:"
            printf '    - %s\n' "${restart_pending[@]}"
            echo "    Restart when ready: metasphere daemon restart"
        fi
    fi
}

setup_daemon_linux() {
    # Fallback path when preflight decided systemd isn't available.
    # Daemons launch via nohup+disown. No auto-restart, no boot
    # persistence — but the telegram bridge and heartbeat still work
    # for the current session. Re-running install.sh on a host with
    # systemd later will switch to the proper service mode.
    if [[ -n "${METASPHERE_NO_SYSTEMD:-}" ]]; then
        info "Starting daemons (nohup fallback, no auto-restart)..."
        mkdir -p "$METASPHERE_DIR/logs"
        # Kill any existing metasphere.cli daemon processes from a
        # previous run, then start fresh. Safe because we own them all
        # (same user).
        pkill -u "$(whoami)" -f 'metasphere\.cli\.(gateway|heartbeat|schedule) daemon' 2>/dev/null || true
        sleep 1
        local mbin="$METASPHERE_DIR/bin/metasphere"
        for d in gateway heartbeat schedule; do
            if [[ "$d" == "gateway" ]]; then
                nohup "$mbin" gateway daemon 3 \
                    > "$METASPHERE_DIR/logs/gateway.log" 2>&1 &
            else
                nohup "$mbin" "$d" daemon \
                    > "$METASPHERE_DIR/logs/$d.log" 2>&1 &
            fi
            disown
        done
        sleep 2
        if pgrep -u "$(whoami)" -f 'metasphere\.cli\.gateway daemon' >/dev/null; then
            ok "Daemons running (nohup). They'll die if you log out."
            echo "    Re-run install.sh on a host with systemd for proper"
            echo "    service management, or enable linger as described above."
        else
            warn "Daemons didn't come up — check $METASPHERE_DIR/logs/*.log"
        fi
        return 0
    fi

    local service_dir="$HOME/.config/systemd/user"
    local template_dir="$SCRIPT_DIR/systemd/user"
    local venv_bin="$METASPHERE_DIR/venv/bin/metasphere"

    mkdir -p "$service_dir"
    mkdir -p "$METASPHERE_DIR/logs"

    # Phase out the obsolete omnibus metasphere.service. It shipped a
    # single ExecStart=metasphere run that doesn't exist as a CLI verb
    # and was superseded by the three split daemons (gateway, heartbeat,
    # schedule) during the Python rewrite. Stop+disable+remove cleanly
    # so a daemon-reload picks up the split units as the source of
    # truth.
    local obsolete_omnibus="$service_dir/metasphere.service"
    if [[ -f "$obsolete_omnibus" ]]; then
        systemctl --user stop metasphere.service 2>/dev/null || true
        systemctl --user disable metasphere.service 2>/dev/null || true
        rm -f "$obsolete_omnibus"
        ok "Removed obsolete metasphere.service (superseded by split daemons)"
    fi

    # Render the three split daemon units from repo templates. Markers
    # (@@METASPHERE_DIR@@, @@METASPHERE_PROJECT_ROOT@@,
    # @@METASPHERE_VENV_BIN@@, @@METASPHERE_AGENT_RUNTIME@@)
    # get install-time-substituted with operator-detected absolute
    # paths. Re-rendering on every install is intentional: it overwrites
    # any hand-written drift (e.g. the manual gateway/heartbeat/schedule
    # units that predate this template scheme) so the templates are the
    # source of truth.
    local rendered_any=false
    local daemon
    for daemon in gateway heartbeat schedule; do
        local tmpl="$template_dir/metasphere-$daemon.service"
        local out="$service_dir/metasphere-$daemon.service"
        if [[ ! -f "$tmpl" ]]; then
            warn "Missing template $tmpl — skipping $daemon"
            continue
        fi
        local tmp
        tmp=$(mktemp)
        sed \
            -e "s|@@METASPHERE_DIR@@|$METASPHERE_DIR|g" \
            -e "s|@@METASPHERE_PROJECT_ROOT@@|$SCRIPT_DIR|g" \
            -e "s|@@METASPHERE_VENV_BIN@@|$venv_bin|g" \
            -e "s|@@METASPHERE_AGENT_RUNTIME@@|$AGENT_RUNTIME|g" \
            "$tmpl" > "$tmp"
        # Idempotent write: only overwrite if content changed, so a
        # second install.sh run is a no-op and `systemctl restart`
        # stays scoped to actual updates.
        if [[ ! -f "$out" ]] || ! cmp -s "$tmp" "$out"; then
            mv "$tmp" "$out"
            rendered_any=true
            ok "Rendered metasphere-$daemon.service"
        else
            rm -f "$tmp"
        fi
    done

    systemctl --user daemon-reload
    if $rendered_any; then
        ok "Reloaded systemd user units"
    fi

    # Disable the standalone telegram poller if present — the gateway
    # daemon handles telegram polling. Running both causes a getUpdates
    # race where each poller kicks the other off the long-poll, leading
    # to lost messages and "terminated by other getUpdates request" spam.
    for stale_unit in metasphere-telegram.service metasphere-telegram-stream.service; do
        if systemctl --user is-enabled "$stale_unit" &>/dev/null; then
            systemctl --user stop "$stale_unit" 2>/dev/null || true
            systemctl --user disable "$stale_unit" 2>/dev/null || true
            ok "Disabled $stale_unit (gateway owns telegram polling)"
        fi
    done

    # Enable so the daemons come up on boot, and start any that aren't
    # already running. Crucially, we do NOT auto-restart already-active
    # units even if the rendered content changed: the gateway is the
    # tmux session manager, so `systemctl restart metasphere-gateway`
    # tears down every live agent tmux session as a side effect. Re-
    # running install.sh on a host with active agents must not surprise
    # the operator. If a unit was rendered into place AND was already
    # running, we surface a "manual restart needed" notice instead.
    local restart_pending=()
    local start_now=true
    if $INTERACTIVE; then
        read -p "Enable + start metasphere daemons now? [Y/n] " -n 1 -r
        echo
        [[ $REPLY =~ ^[Nn]$ ]] && start_now=false
    fi

    if $start_now; then
        for daemon in gateway heartbeat schedule; do
            systemctl --user enable "metasphere-$daemon.service" >/dev/null 2>&1 || true
            if systemctl --user is-active "metasphere-$daemon.service" &>/dev/null; then
                if $rendered_any; then
                    restart_pending+=("metasphere-$daemon.service")
                fi
            else
                systemctl --user start "metasphere-$daemon.service"
            fi
        done
        ok "Daemons enabled (gateway, heartbeat, schedule)"
    fi

    if (( ${#restart_pending[@]} > 0 )); then
        warn "Updated unit(s) are already running with the OLD definition:"
        for unit in "${restart_pending[@]}"; do
            echo "    - $unit"
        done
        echo "    Restarting metasphere-gateway also reaps every live agent"
        echo "    tmux session. Schedule a maintenance window, then:"
        echo "        systemctl --user restart ${restart_pending[*]}"
    fi
}

# =============================================================================
# Claude Code permissions seeding
# =============================================================================

seed_claude_permissions() {
    info "Seeding Claude Code permissions + hooks..."

    local entries='[
        "Bash(git add:*)",
        "Bash(git commit:*)",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git log:*)",
        "Bash(git show:*)",
        "Bash(git stash:*)",
        "Bash(git restore:*)",
        "Bash(git branch:*)",
        "Bash(git switch:*)",
        "Bash(git checkout:*)",
        "Bash(messages:*)",
        "Bash(tasks:*)",
        "Bash(metasphere:*)",
        "Bash(metasphere-*:*)"
    ]'

    # Hook commands point at the metasphere console script in the
    # operator's venv. Absolute path keeps Claude Code's PATH-resolution
    # out of the failure mode (gateway/heartbeat services may not have
    # the venv's bin directory on PATH). The committed
    # .claude/settings.json is empty by design — claude-code merges
    # settings.json + settings.local.json so hardcoded paths in the
    # committed file would fire on every other machine and error.
    #
    # ``metasphere/update.py:_sync_hook_paths`` rewrites these same
    # entries on every ``metasphere update`` cycle so a relocation of
    # the venv (or the package shim) propagates through the existing
    # settings.local.json without operator intervention. Keep this
    # form aligned with the venv-bin form there.
    local hook_bin hook_bin_quoted context_path posthook_path
    hook_bin="$METASPHERE_DIR/venv/bin/metasphere"
    printf -v hook_bin_quoted '%q' "$hook_bin"
    context_path="$hook_bin_quoted hooks context"
    posthook_path="$hook_bin_quoted hooks posthook"
    local pretool_path="$METASPHERE_DIR/venv/bin/metasphere hooks pretool"
    local hooks
    hooks=$(jq -n \
        --arg ctx "$context_path" \
        --arg post "$posthook_path" \
        --arg pre "$pretool_path" \
        '{
            UserPromptSubmit: [
                {
                    matcher: "",
                    hooks: [{ type: "command", command: $ctx }]
                }
            ],
            PreToolUse: [
                {
                    matcher: "AskUserQuestion|ExitPlanMode",
                    hooks: [{ type: "command", command: $pre }]
                }
            ],
            Stop: [
                {
                    matcher: "",
                    hooks: [{ type: "command", command: $post }]
                }
            ]
        }')

    # Write the same hooks + permissions block to every .claude/ location
    # that matters:
    #   1. $SCRIPT_DIR/.claude  — the source checkout, for running claude
    #      directly from inside metasphere-agents/ during development.
    #   2. $METASPHERE_DIR/.claude — the project root where the
    #      orchestrator's tmux session actually runs. Claude Code's hook
    #      discovery is cwd-scoped, so without this file the Stop and
    #      UserPromptSubmit hooks silently do not fire for the live
    #      orchestrator. (This is the bug that took out the Telegram
    #      auto-forward for ~22h when paths.repo → project_root moved
    #      the default cwd out from under the source-repo settings.)
    local target
    for target in "$SCRIPT_DIR/.claude" "$METASPHERE_DIR/.claude"; do
        local target_file="$target/settings.local.json"
        mkdir -p "$target"

        if [[ ! -f "$target_file" ]]; then
            jq -n --argjson new "$entries" --argjson hooks "$hooks" \
                '{permissions: {allow: $new}, hooks: $hooks}' > "$target_file" \
                && ok "Created $target_file (permissions + hooks)" \
                || warn "Failed to create $target_file"
            continue
        fi

        # Merge: union of existing allow and new entries; replace hooks
        # block with the current absolute paths (the checkout location
        # may have moved since the last install).
        local tmp
        tmp=$(mktemp)
        if jq --argjson new "$entries" --argjson hooks "$hooks" '
            .permissions = (.permissions // {}) |
            .permissions.allow = ((.permissions.allow // []) as $cur |
                $cur + ($new - $cur)) |
            .hooks = $hooks
        ' "$target_file" > "$tmp" 2>/dev/null; then
            mv "$tmp" "$target_file" && ok "Updated $target_file (permissions + hooks)" \
                || { warn "Failed to update $target_file"; rm -f "$tmp"; }
        else
            rm -f "$tmp"
            warn "Could not parse $target_file - leaving unchanged"
        fi
    done
}

seed_codex_hooks() {
    info "Seeding Codex hooks..."

    local context_path="$METASPHERE_DIR/venv/bin/metasphere hooks context"
    local posthook_path="$METASPHERE_DIR/venv/bin/metasphere hooks posthook"
    # Codex loads every matching hook source, rather than applying normal
    # config-layer replacement. Install once at user scope so direct,
    # persistent, and headless sessions (including project agents) share the
    # same hook without duplicate context. User hooks also remain available in
    # untrusted project directories; Codex still applies its normal /hooks
    # review gate to this non-managed definition.
    local target="$HOME/.codex"
    local target_file="$target/hooks.json"
    mkdir -p "$target"

    local target_ready=false
    if [[ ! -f "$target_file" ]]; then
        jq -n --arg ctx "$context_path" --arg post "$posthook_path" '{
                hooks: {
                    UserPromptSubmit: [{
                        hooks: [{
                            type: "command",
                            command: $ctx,
                            statusMessage: "Loading Metasphere context",
                            additionalContextLimit: 12000
                        }]
                    }],
                    Stop: [{
                        hooks: [{
                            type: "command",
                            command: $post,
                            statusMessage: "Forwarding response to Telegram"
                        }]
                    }]
                }
            }' > "$target_file" \
            && { target_ready=true; ok "Created $target_file (Codex context + Stop hooks)"; } \
            || warn "Failed to create $target_file"
    else
        local tmp
        tmp=$(mktemp)
        if jq --arg ctx "$context_path" --arg post "$posthook_path" '
            .hooks = (.hooks // {}) |
            .hooks.UserPromptSubmit = (
                [(.hooks.UserPromptSubmit // [])[] |
                  .hooks = [.hooks[]? |
                    select((.command // "") != $ctx)] |
                  select((.hooks | length) > 0)]
                + [{hooks: [{type: "command", command: $ctx,
                    statusMessage: "Loading Metasphere context",
                    additionalContextLimit: 12000}]}]
            ) |
            .hooks.Stop = (
                [(.hooks.Stop // [])[] |
                  .hooks = [.hooks[]? |
                    select((.command // "") != $post)] |
                  select((.hooks | length) > 0)]
                + [{hooks: [{type: "command", command: $post,
                    statusMessage: "Forwarding response to Telegram"}]}]
            )
            ' "$target_file" > "$tmp" 2>/dev/null; then
            mv "$tmp" "$target_file" && { target_ready=true; ok "Updated $target_file (Codex context + Stop hooks)"; } \
                || { warn "Failed to update $target_file"; rm -f "$tmp"; }
        else
            rm -f "$tmp"
            warn "Could not parse $target_file - leaving unchanged"
        fi
    fi

    # Migration for pre-parity installs: Codex composes all active layers, so
    # leave custom groups intact but remove our now-duplicated groups from the
    # old source/runtime locations.
    if [[ "$target_ready" != true ]]; then
        warn "Codex user hook was not installed; preserving legacy hook sources"
        return
    fi

    local legacy_file legacy_tmp
    for legacy_file in "$SCRIPT_DIR/.codex/hooks.json" "$METASPHERE_DIR/.codex/hooks.json"; do
        [[ "$legacy_file" == "$target_file" || ! -f "$legacy_file" ]] && continue
        legacy_tmp=$(mktemp)
        if jq --arg ctx "$context_path" --arg post "$posthook_path" '
            .hooks = (.hooks // {}) |
            .hooks.UserPromptSubmit = [(.hooks.UserPromptSubmit // [])[] |
              .hooks = [.hooks[]? |
                select((.command // "") != $ctx)] |
              select((.hooks | length) > 0)] |
            .hooks.Stop = [(.hooks.Stop // [])[] |
              .hooks = [.hooks[]? |
                select((.command // "") != $post)] |
              select((.hooks | length) > 0)] |
            if (.hooks.UserPromptSubmit | length) == 0 then del(.hooks.UserPromptSubmit) else . end |
            if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end
        ' "$legacy_file" > "$legacy_tmp" 2>/dev/null; then
            mv "$legacy_tmp" "$legacy_file" \
                && ok "Migrated Metasphere hooks out of $legacy_file" \
                || { warn "Failed to migrate $legacy_file"; rm -f "$legacy_tmp"; }
        else
            rm -f "$legacy_tmp"
            warn "Could not parse $legacy_file - leaving unchanged"
        fi
    done
}

seed_runtime_integration() {
    case "$AGENT_RUNTIME" in
        claude) seed_claude_permissions ;;
        codex) seed_codex_hooks ;;
        *) warn "Unsupported agent runtime '$AGENT_RUNTIME' - hooks not installed" ;;
    esac
}

# =============================================================================
# Final setup
# =============================================================================

show_completion() {
    echo
    echo "Installation complete!"
    echo "====================="
    echo
    echo "Directory: $METASPHERE_DIR"
    echo
    echo "Commands:"
    echo "  metasphere status          # System overview"
    echo "  metasphere ls              # Project landscape"
    echo "  metasphere agents          # List agents"
    echo "  metasphere gateway status  # Gateway/Telegram status"
    echo
    echo "Daemon:"
    if [[ "$(uname)" == "Darwin" ]]; then
        echo "  launchctl list | grep metasphere"
        echo "  tail -f $METASPHERE_DIR/logs/gateway.log"
    else
        echo "  systemctl --user status metasphere-gateway metasphere-heartbeat metasphere-schedule"
        echo "  journalctl --user -u metasphere-gateway -f"
    fi
    echo
    echo "Documentation: https://github.com/$REPO"
}

# =============================================================================
# Auto-update job registration
# =============================================================================

register_auto_update_job() {
    info "Registering auto-update cron job..."
    local bin="$METASPHERE_DIR/bin/metasphere"
    if [[ ! -x "$bin" ]] && ! command -v metasphere &>/dev/null; then
        warn "metasphere command not found yet, skipping cron job registration"
        return 0
    fi
    if "${bin:-metasphere}" update --register-job 2>/dev/null; then
        ok "Auto-update job registered (see: metasphere update --status)"
    else
        warn "Could not register auto-update job (run 'metasphere update --register-job' manually)"
    fi
}

register_consolidate_job() {
    info "Registering task consolidation cron job..."
    local bin="$METASPHERE_DIR/bin/metasphere"
    if [[ ! -x "$bin" ]] && ! command -v metasphere &>/dev/null; then
        warn "metasphere command not found yet, skipping consolidate job registration"
        return 0
    fi
    # Idempotent: register_job in metasphere.consolidate replaces an
    # existing entry in place rather than duplicating it.
    if "${bin:-metasphere}" consolidate --register-job 2>/dev/null; then
        ok "Task consolidation job registered (every 4h; see: metasphere consolidate --status)"
    else
        warn "Could not register consolidate job (run 'metasphere consolidate --register-job' manually)"
    fi
}

# =============================================================================
# Main
# =============================================================================

install_skills() {
    info "Installing Claude Code skills + commands..."

    local skills_src="$SCRIPT_DIR/skills"
    local skills_dst="$HOME/.claude/skills"
    # Slash commands ship under templates/claude-commands/ in the
    # source repo (not under .claude/, which is per-machine runtime
    # state and gitignored). install_skills materializes them into
    # the operator's Claude config dirs at install time.
    local commands_src="$SCRIPT_DIR/templates/claude-commands"
    local commands_dst_user="$HOME/.claude/commands"
    local commands_dst_repo="$SCRIPT_DIR/.claude/commands"
    local installed=0

    # Skills: symlink each skill directory into ~/.claude/skills/
    # Symlinks stay in sync with git pull — no copy-on-update needed.
    if [[ -d "$skills_src" ]]; then
        mkdir -p "$skills_dst"
        for skill_dir in "$skills_src"/*/; do
            [[ -f "$skill_dir/SKILL.md" ]] || continue
            local name=$(basename "$skill_dir")
            local target=$(cd "$skill_dir" && pwd)
            # Don't overwrite user-customized skills (real dir, not symlink)
            if [[ -d "$skills_dst/$name" && ! -L "$skills_dst/$name" && -f "$skills_dst/$name/.user-customized" ]]; then
                continue
            fi
            ln -sfn "$target" "$skills_dst/$name"
            installed=$((installed + 1))
        done
    fi

    # Commands: symlink each templates/claude-commands/*.md into
    #   1. ~/.claude/commands/    — user-level (works everywhere).
    #   2. <repo>/.claude/commands/ — project-level (works when running
    #      claude from inside this repo). Same pattern as
    #      seed_claude_permissions which writes settings.local.json to
    #      both locations.
    # Symlinks (not copies) so a git pull updates the live commands.
    if [[ -d "$commands_src" ]]; then
        mkdir -p "$commands_dst_user" "$commands_dst_repo"
        for cmd_file in "$commands_src"/*.md; do
            [[ -f "$cmd_file" ]] || continue
            local cmd_target=$(cd "$(dirname "$cmd_file")" && pwd)/$(basename "$cmd_file")
            ln -sfn "$cmd_target" "$commands_dst_user/$(basename "$cmd_file")"
            ln -sfn "$cmd_target" "$commands_dst_repo/$(basename "$cmd_file")"
            installed=$((installed + 1))
        done
    fi

    [[ $installed -gt 0 ]] && ok "Linked $installed skills/commands into ~/.claude/ + repo .claude/"
}

main() {
    check_dependencies
    setup_directories
    install_scripts
    # Run migrations BEFORE telegram/CAM setup so an imported source
    # (e.g. an existing Telegram token) is in place when those steps
    # check for it. Migrations are silent no-ops when no source is
    # detected — the dispatcher iterates migrate/*/ and only acts on
    # subdirs whose detect.sh returns 0.
    run_migrations
    install_cam           # Ensure cam binary is available
    migrate_cam_data      # Reuse existing ~/.cam to skip re-index
    setup_telegram
    setup_orchestrator
    seed_runtime_integration
    install_skills        # Skills + slash commands to ~/.claude/
    setup_daemon
    register_auto_update_job
    register_consolidate_job
    show_completion
}

main "$@"
