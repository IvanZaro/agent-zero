#!/bin/bash
set -e

# install playwright - moved to install A0
# bash /ins/install_playwright.sh "$@"

# searxng - moved to base image
# bash /ins/install_searxng.sh "$@"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get unavailable; skipping LibreOffice install"
  exit 0
fi

XPRA_PACKAGES=(xpra xpra-x11 xpra-html5)

install_xpra_repo() {
  local os_id=""
  local codename=""
  local uri="https://xpra.org"
  local suite="trixie"
  local arch

  arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    os_id="${ID:-}"
    codename="${VERSION_CODENAME:-}"
  fi

  if [ "$os_id" = "kali" ]; then
    uri="https://xpra.org/beta"
    suite="sid"
  elif [ "$codename" = "sid" ] || [ "$codename" = "forky" ]; then
    uri="https://xpra.org/beta"
    suite="$codename"
  elif [ -n "$codename" ]; then
    suite="$codename"
  fi

  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates wget
  configure_xpra_repo "$uri" "$suite" "$arch"
  apt-get update

  if ! xpra_install_check; then
    if [ "$arch" != "amd64" ]; then
      echo "xpra packages are not installable from ${uri} ${suite} for ${arch}; falling back to https://xpra.org trixie"
      XPRA_PACKAGES=(xpra-server xpra-x11 xpra-html5)
      configure_xpra_repo "https://xpra.org" "trixie" "$arch"
      apt-get update
      if ! xpra_install_check; then
        cat /tmp/xpra-install-check.log
        exit 1
      fi
    else
      cat /tmp/xpra-install-check.log
      exit 1
    fi
  fi
}

xpra_install_check() {
  DEBIAN_FRONTEND=noninteractive apt-get install -s --no-install-recommends "${XPRA_PACKAGES[@]}" >/tmp/xpra-install-check.log 2>&1
}

configure_xpra_repo() {
  local uri="$1"
  local suite="$2"
  local arch="$3"

  wget -O /usr/share/keyrings/xpra.asc https://xpra.org/xpra.asc
  cat >/etc/apt/sources.list.d/xpra.sources <<EOF
Types: deb
URIs: ${uri}
Suites: ${suite}
Components: main
Signed-By: /usr/share/keyrings/xpra.asc
Architectures: ${arch}
EOF
}

install_xpra_repo
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libreoffice-core \
  libreoffice-writer \
  libreoffice-calc \
  libreoffice-impress \
  libreoffice-gtk3 \
  python3-uno \
  "${XPRA_PACKAGES[@]}" \
  xfce4-session \
  xfwm4 \
  xfce4-panel \
  xfdesktop4 \
  xfce4-settings \
  thunar \
  gvfs \
  libglib2.0-bin \
  xfce4-terminal \
  x11-xserver-utils \
  xdotool \
  xauth \
  dbus-x11 \
  fonts-dejavu \
  fonts-liberation \
  fonts-crosextra-caladea \
  fonts-crosextra-carlito \
  fonts-noto-core \
  fonts-noto-cjk \
  fonts-noto-color-emoji

rm -rf /var/lib/apt/lists/*

# === Claude Code MCP install (fork addition) =================================
# Installs the Anthropic Claude Code CLI globally and a pinned copy of the
# steipete claude-code-mcp server under /opt/mcp/claude-code-mcp so A0 can
# invoke Claude Code as an MCP tool (see plan §7).
#
# Pinned to avoid silent ABI breakage in the MCP args[] shape across upstream
# updates. Bump when intentionally upgrading; verify with:
#   npm view @anthropic-ai/claude-code version
#   npm view @steipete/claude-code-mcp version
CLAUDE_CODE_VERSION="2.1.131"
CLAUDE_CODE_MCP_VERSION="1.10.12"

# The base image (agent0ai/agent-zero-base) ships node_eval.js at
# /exe/node_eval.js, which strongly suggests Node is preinstalled. Detect
# defensively and install via NodeSource (LTS) only if missing, matching
# the apt-get style used above.
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Node.js/npm not present; installing NodeSource LTS"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg
  mkdir -p /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs
  rm -rf /var/lib/apt/lists/*
fi

npm i -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" || true

mkdir -p /opt/mcp/claude-code-mcp
cd /tmp
npm pack "@steipete/claude-code-mcp@${CLAUDE_CODE_MCP_VERSION}"
tar -xzf steipete-claude-code-mcp-*.tgz -C /opt/mcp/claude-code-mcp --strip-components=1
rm -f steipete-claude-code-mcp-*.tgz
cd /opt/mcp/claude-code-mcp
npm ci --omit=dev
# === end Claude Code MCP install =============================================
