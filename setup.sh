#!/usr/bin/env sh
# OpenAgent installer for macOS and Linux (spec §1–§5).
#
# What it does, and just as importantly what it does NOT do:
#   • Uses uv (Astral's standalone installer) as the engine. uv needs no pre-existing Python.
#   • Installs a *managed* Python 3.12 that belongs to uv — your system Python is never touched,
#     and nothing is installed into it.
#   • Installs OpenAgent from THIS repository into an isolated uv tool environment (runtime deps
#     only — no pytest/ruff/mypy) and links the `openagent` executable onto your PATH.
#   • Never creates a .venv inside the repo, never runs `sudo pip`, never asks you to install
#     Python/pip/pipx yourself, and never deletes your OpenAgent data, agents, providers, or runs.
#   • Re-running it UPDATES an existing install (idempotent).
#
# CI / automation: set OPENAGENT_SETUP_NO_LAUNCH=1 to verify the install without opening the TUI.
set -eu

# A verified, pinned uv version for reproducible bootstraps. An already-installed uv on PATH is
# preferred over installing this one.
UV_VERSION="0.11.28"

WORK="$(mktemp -d 2>/dev/null || mktemp -d -t openagent-setup)"
# Clean up scratch files on any exit; never leaves temp behind.
trap 'rm -rf "$WORK"' EXIT INT TERM

# --------------------------------------------------------------------------- output helpers
say()  { printf '[openagent-setup] %s\n' "$*"; }
warn() { printf '[openagent-setup] warning: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

# die <stage> <what-happened> [how-to-fix]
die() {
    stage="$1"; what="$2"; fix="${3:-See the Troubleshooting section in README.md.}"
    printf '\n[openagent-setup] ERROR\n' >&2
    printf '[openagent-setup]   stage : %s\n' "$stage" >&2
    printf '[openagent-setup]   what  : %s\n' "$what" >&2
    printf '[openagent-setup]   fix   : %s\n' "$fix" >&2
    exit 1
}

# --------------------------------------------------------------------------- 0. repository root
# Resolve the repo root from the script's own location, so it works no matter where it is called
# from — including paths with spaces or non-ASCII characters.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)" \
    || die "locate-repo" "could not determine the script directory"
REPO_ROOT="$SCRIPT_DIR"

[ -f "$REPO_ROOT/pyproject.toml" ] \
    || die "locate-repo" "no pyproject.toml in $REPO_ROOT" \
           "Run setup.sh from inside a cloned OpenAgent repository."
[ -d "$REPO_ROOT/src/openagent" ] \
    || die "locate-repo" "no src/openagent in $REPO_ROOT" \
           "This does not look like the OpenAgent repository."
grep -q '^name = "openagent"' "$REPO_ROOT/pyproject.toml" 2>/dev/null \
    || die "locate-repo" "pyproject.toml is not the 'openagent' package" \
           "Make sure you cloned https://github.com/yasirkaramandev/openagent"

say "Installing OpenAgent from: $REPO_ROOT"

# Note (not warn) any pre-existing `openagent` so a shadowed command is never a silent surprise (§4).
PRE_EXISTING="$(command -v openagent 2>/dev/null || true)"
[ -n "$PRE_EXISTING" ] && say "note: an 'openagent' command is already on PATH at $PRE_EXISTING"
have git || warn "Git is not installed; git-worktree runs will fall back to isolated copies"
if ! have docker && ! have podman; then
    say "note: Docker/Podman is absent; optional container-sandbox runs will be unavailable"
fi

# --------------------------------------------------------------------------- 1. uv
locate_uv() {
    if have uv; then command -v uv; return 0; fi
    for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    done
    return 1
}

if UV="$(locate_uv)"; then
    say "[1/6] Using existing uv: $UV ($("$UV" --version 2>/dev/null || echo unknown))"
else
    say "[1/6] Installing uv $UV_VERSION (Astral standalone installer over HTTPS)"
    url="https://astral.sh/uv/${UV_VERSION}/install.sh"
    installer="$WORK/uv-install.sh"
    if have curl; then
        curl -LsSf "$url" -o "$installer" || die "install-uv" "curl could not download uv from $url" \
            "Check your network / proxy, or install uv manually: https://docs.astral.sh/uv/"
    elif have wget; then
        wget -qO "$installer" "$url" || die "install-uv" "wget could not download uv from $url" \
            "Check your network / proxy, or install uv manually: https://docs.astral.sh/uv/"
    else
        die "install-uv" "neither curl nor wget is available" \
            "Install curl or wget, or install uv manually: https://docs.astral.sh/uv/"
    fi
    sh "$installer" || die "install-uv" "the uv installer failed"
    UV="$(locate_uv)" || die "install-uv" \
        "uv was installed but could not be found on PATH, \$HOME/.local/bin, or \$HOME/.cargo/bin" \
        "Open a new terminal and re-run setup.sh, or add uv's bin dir to PATH."
    say "      uv ready: $UV ($("$UV" --version 2>/dev/null || echo unknown))"
fi

# --------------------------------------------------------------------------- 2. managed Python
say "[2/6] Installing a managed Python 3.12 (isolated; your system Python is untouched)"
"$UV" python install 3.12 \
    || die "install-python" "uv could not install a managed Python 3.12" \
           "Check your network / proxy. This does not modify your system Python."

# --------------------------------------------------------------------------- 3. OpenAgent tool
say "[3/6] Installing OpenAgent (runtime deps only — no dev tools; your data is preserved)"
"$UV" tool install --force --python 3.12 "$REPO_ROOT" \
    || die "install-openagent" "uv tool install failed for $REPO_ROOT" \
           "Re-run setup.sh; if it persists, check the output above for the failing dependency."

TOOL_BIN="$("$UV" tool dir --bin 2>/dev/null)" \
    || die "install-openagent" "could not read uv's tool bin directory"
OPENAGENT_BIN="$TOOL_BIN/openagent"
[ -x "$OPENAGENT_BIN" ] \
    || die "install-openagent" "openagent executable missing at $OPENAGENT_BIN after install" \
           "Re-run setup.sh; the tool install may have been interrupted."

# --------------------------------------------------------------------------- 4. PATH
add_path_to_profile() {
    prof="$1"; dir="$2"
    [ -e "$prof" ] || return 0  # only edit profiles that already exist
    marker="# added by OpenAgent setup"
    if grep -Fq "$marker" "$prof" 2>/dev/null && grep -Fq "$dir" "$prof" 2>/dev/null; then
        return 0  # already present — idempotent, never appended twice
    fi
    {
        printf '\n%s\n' "$marker"
        # Runtime-idempotent: only prepend when the dir is not already on PATH.
        printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$dir" "$dir"
    } >> "$prof" && say "      added $dir to PATH in $prof"
}

say "[4/6] Making the 'openagent' command available in new terminals"
# uv's own mechanism for future shells…
"$UV" tool update-shell >/dev/null 2>&1 || warn "uv tool update-shell reported an issue"
# …plus an explicit, idempotent fallback across the common shell profiles.
for prof in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    add_path_to_profile "$prof" "$TOOL_BIN"
done
# If none of those exist yet, create ~/.profile so a fresh login shell still finds openagent.
if [ ! -e "$HOME/.zshrc" ] && [ ! -e "$HOME/.bashrc" ] && \
   [ ! -e "$HOME/.bash_profile" ] && [ ! -e "$HOME/.profile" ]; then
    : > "$HOME/.profile"
    add_path_to_profile "$HOME/.profile" "$TOOL_BIN"
fi

# A launcher in an already-on-PATH dir works even in shells that don't source a profile. Best-effort,
# and sudo is used only interactively and only with an explicit heads-up (§4 — never hidden).
LINK="/usr/local/bin/openagent"
if [ -L "$LINK" ] && [ "$(readlink "$LINK" 2>/dev/null)" = "$OPENAGENT_BIN" ]; then
    say "      launcher already present: $LINK"
elif [ -w "/usr/local/bin" ]; then
    ln -sf "$OPENAGENT_BIN" "$LINK" 2>/dev/null && say "      linked $LINK -> $OPENAGENT_BIN"
elif [ -t 0 ] && have sudo; then
    say "      NOTE: creating $LINK needs admin rights — sudo may prompt for your password."
    if sudo ln -sf "$OPENAGENT_BIN" "$LINK" 2>/dev/null; then
        say "      linked $LINK -> $OPENAGENT_BIN (via sudo)"
    else
        warn "could not create $LINK; relying on the PATH entry above (open a new terminal)"
    fi
fi

# Make it work in THIS process too.
PATH="$TOOL_BIN:$PATH"
export PATH

# --------------------------------------------------------------------------- 5. verify
say "[5/6] Verifying installation"
"$OPENAGENT_BIN" version \
    || die "verify" "openagent could not run — the entrypoint or an import is broken" \
           "This is a real install failure; please re-run setup.sh and report the output."
# doctor exiting non-zero only because optional CLIs (Codex/Claude/agy) are missing is NOT a failure.
if "$OPENAGENT_BIN" doctor --json >/dev/null 2>&1; then
    say "      doctor: ok"
else
    say "      doctor reported warnings (e.g. optional Codex/Claude/agy CLIs not installed) — not an install failure"
fi

RESOLVED="$(command -v openagent 2>/dev/null || true)"
if [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$OPENAGENT_BIN" ] && \
   [ "$(readlink "$RESOLVED" 2>/dev/null || echo "$RESOLVED")" != "$OPENAGENT_BIN" ]; then
    warn "another 'openagent' resolves first on PATH ($RESOLVED). The one just installed is: $OPENAGENT_BIN"
fi

# --------------------------------------------------------------------------- 6. launch
if [ "${OPENAGENT_SETUP_NO_LAUNCH:-0}" = "1" ]; then
    say "[6/6] OPENAGENT_SETUP_NO_LAUNCH=1 set — skipping TUI launch. Install verified."
    say "Done. Open a new terminal and run: openagent"
    exit 0
fi

say "[6/6] Starting OpenAgent… (a new terminal will let you run 'openagent' directly)"
exec "$OPENAGENT_BIN"
