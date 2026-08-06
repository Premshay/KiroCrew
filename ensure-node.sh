#!/bin/bash
# Ensure Node.js is available for the build. Installs via mise (preferred),
# nvm, or the nodejs "unofficial-builds" glibc-217 tarball on old-glibc hosts.
# Called by: setup.sh, kirocrew update, kirocrew gateway, Makefile (frontend)
# Platforms: macOS, Amazon Linux 2 (glibc 2.26), Amazon Linux 2023, Linux
#
# On success this records the resolved node bin directory in
# "<data-home>/node-bin-dir" so non-interactive callers (e.g. make) can put
# the right node on PATH without re-running a version manager. The data home is
# "$KIROCREW_HOME" when set, else "$HOME/.kiro/crew" (the current default) —
# NOT the pre-move "$HOME/.kirocrew", which the one-time data-home migration
# deletes, so writing there would resurrect the legacy dir on every build.

# The frontend build (vite 8 / rolldown) requires Node >= 20.12: rolldown's
# runtime imports `styleText` from `node:util`, an export added in Node 20.12.0.
# Anything older fails at ESM instantiation with
#   "The requested module 'node:util' does not provide an export named 'styleText'".
# The floor is per release-line, not a bare major: styleText was added in Node
# 20.12.0 and, on the (non-LTS) 21.x line, only in 21.7.0; every 22+ release has
# it. So 20.0–20.11 and 21.0–21.6 predate styleText and crash the same way.
MIN_VERSION="20.12"   # human-readable primary floor, for messages
TARGET_VERSION=20

# Amazon Linux 2 ships glibc 2.26, but the OFFICIAL Node >= 18 binaries are
# linked against glibc >= 2.28 and fail to load ("GLIBC_2.28 not found"). The
# official Node 16 build (glibc-2.17 baseline) runs on AL2 but is below the
# >= 20.12 floor the frontend now needs. The nodejs "unofficial-builds" project
# publishes a `glibc-217` variant compiled against glibc 2.17 that satisfies
# both constraints on AL2 — x64 only (upstream ships no arm64 glibc-217 build).
GLIBC217_VERSION=20.20.2

_node_major() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f1
}

_node_minor() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f2
}

# node is "usable" only if it is on PATH AND actually executes. A binary built
# for a newer glibc is present on PATH but exits non-zero with a loader error,
# so a plain "command -v node" is not enough.
_node_usable() {
    command -v node >/dev/null 2>&1 && node -v >/dev/null 2>&1
}

# True (0) iff the current node runs AND has util.styleText. That export landed
# in 20.12.0 and (on the 21.x line) 21.7.0, and is in every 22+ release, so the
# cutoff is per release-line. This is the single predicate for "is node good
# enough?" — used to decide install, to pick the old-glibc fallback, and for
# final validation, so a runnable-but-too-old node (Node 16/20.11/21.6 already
# on PATH) never masquerades as sufficient.
_node_version_ok() {
    _node_usable || return 1
    local maj min
    maj=$(_node_major); min=$(_node_minor)
    [ -n "$maj" ] && [ -n "$min" ] || return 1
    if [ "$maj" -ge 22 ]; then return 0; fi
    if [ "$maj" -eq 21 ]; then [ "$min" -ge 7 ]; return; fi
    if [ "$maj" -eq 20 ]; then [ "$min" -ge 12 ]; return; fi
    return 1
}

_needs_install() {
    ! _node_version_ok
}

_get_platform() {
    if [[ "$(uname)" == "Darwin" ]]; then echo "mac"
    # AL2023 must be checked before AL2: "Amazon Linux release 2" is a
    # substring of "Amazon Linux release 2023".
    elif grep -q 'Amazon Linux release 2023' /etc/system-release 2>/dev/null; then echo "al2023"
    elif grep -q 'Amazon Linux release 2' /etc/system-release 2>/dev/null; then echo "al2"
    else echo "linux"; fi
}

_source_mise() {
    local mise_bin=""
    if [ -x "$HOME/.local/bin/mise" ]; then
        mise_bin="$HOME/.local/bin/mise"
    elif command -v mise &>/dev/null; then
        mise_bin="mise"
    fi
    [ -z "$mise_bin" ] && return 0
    eval "$("$mise_bin" activate bash 2>/dev/null)" 2>/dev/null || true
    # `mise activate` sets up a prompt hook and does not reliably inject tools
    # onto PATH in a non-interactive script (e.g. when invoked from make), so
    # also add the resolved node bin dir directly.
    local nb
    nb="$("$mise_bin" which node 2>/dev/null)"
    [ -n "$nb" ] && export PATH="$(dirname "$nb"):$PATH"
}

_source_nvm() {
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        . "$NVM_DIR/nvm.sh"
    fi
}

# Directory holding the unofficial glibc-217 Node install, under the data home.
_glibc217_node_dir() {
    echo "${KIROCREW_HOME:-$HOME/.kiro/crew}/node-glibc217"
}

# Put a previously-installed glibc-217 node on PATH (if present). Called LAST in
# the source order so it takes precedence over any stale mise/nvm node.
_source_glibc217_node() {
    local bin
    bin="$(_glibc217_node_dir)/bin"
    [ -x "$bin/node" ] && export PATH="$bin:$PATH"
}

# Install the nodejs "unofficial-builds" glibc-217 variant (glibc 2.17 baseline)
# so a Node >= 20.12 runs on old-glibc hosts (AL2 = glibc 2.26). x64 only.
# On success exports PATH and returns 0; otherwise prints guidance and returns 1.
_install_glibc217_node() {
    local ver="$GLIBC217_VERSION"
    local arch
    arch="$(uname -m)"
    if [ "$arch" != "x86_64" ]; then
        echo "  ⚠️  $arch: upstream publishes no glibc-217 Node build."
        echo "     The frontend build needs Node >= 20.12 (vite 8 / rolldown), and the"
        echo "     official arm64 Node >= 18 needs glibc 2.28 (this host has less)."
        echo "     Build Node from source, or run 'make build' on an x86_64 host / in CI."
        return 1
    fi

    local dir tarball url tmp
    dir="$(_glibc217_node_dir)"
    tarball="node-v${ver}-linux-x64-glibc-217"
    url="https://unofficial-builds.nodejs.org/download/release/v${ver}/${tarball}.tar.gz"

    # Reuse an existing good install.
    if [ -x "$dir/bin/node" ] && "$dir/bin/node" -v >/dev/null 2>&1; then
        export PATH="$dir/bin:$PATH"
        return 0
    fi

    echo "  → Installing Node v${ver} (glibc-217, old-glibc compatible) from unofficial-builds…"
    tmp="$(mktemp -d)" || return 1
    if ! curl -fsSL "$url" -o "$tmp/node.tar.gz"; then
        echo "  ⚠️  Download failed: $url"
        rm -rf "$tmp"; return 1
    fi

    # Verify integrity against the published checksum (best-effort: only enforced
    # when both the manifest and a sha256 tool are available).
    if curl -fsSL "https://unofficial-builds.nodejs.org/download/release/v${ver}/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"; then
        local want got sha_cmd
        if command -v sha256sum >/dev/null 2>&1; then sha_cmd="sha256sum";
        elif command -v shasum >/dev/null 2>&1; then sha_cmd="shasum -a 256"; fi
        if [ -n "$sha_cmd" ]; then
            want="$(awk -v f="${tarball}.tar.gz" '$2==f {print $1}' "$tmp/SHASUMS256.txt")"
            got="$($sha_cmd "$tmp/node.tar.gz" | awk '{print $1}')"
            # Empty want = the expected filename was absent from the manifest
            # (wrong version / bad download); fail closed rather than skip.
            if [ -z "$want" ] || [ "$want" != "$got" ]; then
                echo "  ⚠️  Checksum verification failed for ${tarball}.tar.gz (want '$want' got '$got')"
                rm -rf "$tmp"; return 1
            fi
        fi
    fi

    rm -rf "$dir"
    mkdir -p "$dir"
    if ! tar -xzf "$tmp/node.tar.gz" -C "$dir" --strip-components=1; then
        echo "  ⚠️  Extraction failed"
        # Remove the half-populated dir too: a partial tar can leave a runnable
        # bin/node that later validation / the reuse check would wrongly accept.
        rm -rf "$dir" "$tmp"; return 1
    fi
    rm -rf "$tmp"

    export PATH="$dir/bin:$PATH"
    "$dir/bin/node" -v >/dev/null 2>&1
}

# Record where node ended up so make / other callers can find it. Writes into
# the data home ($KIROCREW_HOME, else ~/.kiro/crew) — never the legacy
# ~/.kirocrew (see header).
_record_node_bin() {
    if command -v node >/dev/null 2>&1; then
        local home="${KIROCREW_HOME:-$HOME/.kiro/crew}"
        mkdir -p "$home"
        dirname "$(command -v node)" > "$home/node-bin-dir" 2>/dev/null || true
    fi
}

PLATFORM=$(_get_platform)

# Source existing managers first — node may already be installed but not in PATH.
# glibc-217 last so it wins over any stale mise/nvm node still on PATH.
_source_mise
_source_nvm
_source_glibc217_node

if ! _needs_install; then
    echo "  ✅ node v$(_node_major) ($(which node))"
    _record_node_bin
    exit 0
fi

echo "  → Node.js missing or < $MIN_VERSION, installing node@$TARGET_VERSION on $PLATFORM…"

_ensure_mise() {
    _source_mise
    if ! command -v mise &>/dev/null; then
        echo "  → Installing mise…"
        curl -fsSL https://mise.run | sh
        _source_mise
    fi
}

case $PLATFORM in
    al2)
        # AL2 (glibc 2.26): the official Node >= 18 binaries won't load, so use
        # the glibc-217 unofficial build (>= 20.12 for vite 8 / rolldown).
        _install_glibc217_node
        ;;
    mac|al2023)
        # macOS + AL2023 (glibc 2.34) run the official mise build for Node 20.
        _ensure_mise
        mise use -g "node@$TARGET_VERSION" 2>/dev/null
        ;;
    *)
        # Generic Linux — try mise, fall back to nvm.
        _ensure_mise
        if command -v mise &>/dev/null; then
            mise use -g "node@$TARGET_VERSION" 2>/dev/null
        else
            echo "  → Falling back to nvm…"
            if [ ! -d "$HOME/.nvm" ]; then
                curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash 2>/dev/null
            fi
            _source_nvm
            nvm install "$TARGET_VERSION" 2>/dev/null
            nvm alias default "$TARGET_VERSION" 2>/dev/null
        fi
        # If the resulting node can't load OR is below the >= 20.12 floor (an
        # old glibc leaves official Node 20 unloadable, or a pre-existing
        # nvm/mise Node 16/18 is runnable but too old), fall back to the
        # glibc-217 unofficial build (x64), which runs on glibc 2.17+ and is
        # >= 20.12 as the frontend build requires.
        _source_mise
        _source_nvm
        if ! _node_version_ok; then
            echo "  → node@$TARGET_VERSION is unusable or < $MIN_VERSION here; trying glibc-217 build…"
            _install_glibc217_node
        fi
        ;;
esac

# Re-source to pick up newly installed node. glibc-217 last so it wins.
_source_mise
_source_nvm
_source_glibc217_node

if _node_version_ok; then
    echo "  ✅ node $(node -v) installed ($(which node))"
    _record_node_bin
else
    echo "  ⚠️  Node install failed — frontend build needs Node >= $MIN_VERSION"
    echo "     macOS / AL2023 / modern Linux: curl https://mise.run | sh && mise use -g node@$TARGET_VERSION"
    echo "     Amazon Linux 2 (x86_64): re-run 'bash ensure-node.sh' (installs the glibc-217 Node build)"
    exit 1
fi
