"""Centralized path and architecture constants for Zamal AI Operator.

All user-specific and architecture-specific values are derived here using:
  - os.path.expanduser("~") for home-relative paths
  - platform.machine() for architecture string
  - Environment variable overrides with safe defaults

Import from here; never hardcode /Users/book or x86_64-apple-darwin elsewhere.

CPC path is included here for consistency but remains READ-ONLY from Operator.
Do not add CPC to any writeable path set.
"""
import os
import platform
from pathlib import Path

# ── Home directory ────────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))

# ── Operator root ─────────────────────────────────────────────────────────────
# Override with ZAMAL_OPERATOR_ROOT for non-standard installs.

OPERATOR_ROOT = Path(
    os.environ.get("ZAMAL_OPERATOR_ROOT", str(HOME / "zamal-ai-operator"))
).resolve()

# ── CPC root (read-only from Operator) ───────────────────────────────────────
# Override with ZAMAL_CPC_ROOT if the CPC project lives elsewhere.

CPC_ROOT = Path(
    os.environ.get("ZAMAL_CPC_ROOT", str(HOME / "Desktop" / "AI-GROCERY-DECISION-PLATFORM"))
).resolve()

# ── Node / NVM ────────────────────────────────────────────────────────────────
# Override with ZAMAL_NVM_ROOT or ZAMAL_NODE_BIN for custom installs.
# NVM node version can be overridden with ZAMAL_NODE_VERSION.

_NVM_ROOT = Path(os.environ.get("ZAMAL_NVM_ROOT", str(HOME / ".nvm")))
_NODE_VERSION = os.environ.get("ZAMAL_NODE_VERSION", "v22.23.2")

NODE_BIN: Path
if "ZAMAL_NODE_BIN" in os.environ:
    NODE_BIN = Path(os.environ["ZAMAL_NODE_BIN"])
else:
    NODE_BIN = _NVM_ROOT / "versions" / "node" / _NODE_VERSION / "bin" / "node"

# ── Codex architecture string ─────────────────────────────────────────────────
# Override with ZAMAL_CODEX_ARCH for cross-architecture installs.
# platform.machine() returns e.g. "x86_64", "arm64" on macOS.
# The standalone release directory uses "{version}-{arch}-{os}" naming.

def _default_codex_arch() -> str:
    machine = platform.machine()
    system = platform.system().lower()
    # Map to the naming convention used by Codex standalone releases.
    if system == "darwin":
        return f"{machine}-apple-darwin"
    if system == "linux":
        return f"{machine}-unknown-linux-gnu"
    return machine

CODEX_ARCH: str = os.environ.get("ZAMAL_CODEX_ARCH", _default_codex_arch())

# ── Codex standalone binary ───────────────────────────────────────────────────

CODEX_STANDALONE_149 = (
    HOME / ".codex" / "packages" / "standalone" / "releases"
    / f"0.149.0-{CODEX_ARCH}" / "bin" / "codex"
)
