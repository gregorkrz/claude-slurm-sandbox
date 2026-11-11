#!/usr/bin/env bash
# One-time install. Copies the code to PREFIX (outside any project), builds a
# venv with the MCP SDK, and links claude-hpc / claude-slurm into BINDIR.
#
#   ./install.sh [--prefix DIR] [--bindir DIR] [--python PY]
#
# Re-run to upgrade. Per-project setup is `claude-slurm init` (see README).
set -euo pipefail

PREFIX="$HOME/.local/share/claude-slurm"
BINDIR="$HOME/.local/bin"
PYTHON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --bindir) BINDIR="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PREFIX="$(realpath -m "$PREFIX")"
BINDIR="$(realpath -m "$BINDIR")"
[[ "$SRC" == "$PREFIX" ]] && { echo "run install.sh from a checkout, not from PREFIX" >&2; exit 1; }

if [[ -z "$PYTHON" ]]; then
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
      PYTHON="$(command -v "$c")"; break
    fi
  done
fi
[[ -n "$PYTHON" ]] || { echo "need Python >= 3.10 (try: module load python; or --python)" >&2; exit 1; }
echo "python: $PYTHON ($("$PYTHON" -V))"

for t in bwrap socat; do
  command -v "$t" >/dev/null || echo "WARNING: '$t' not in PATH. Claude Code's sandbox needs it (see README)." >&2
done

mkdir -p "$PREFIX" "$BINDIR"
[[ -d "$PREFIX/claude_slurm" ]] && chmod -R u+w "$PREFIX/claude_slurm" "$PREFIX/bin"
[[ -d "$PREFIX/client" ]] && chmod -R u+w "$PREFIX/client"
rm -rf "$PREFIX/claude_slurm" "$PREFIX/bin" "$PREFIX/client"
cp -r "$SRC/claude_slurm" "$SRC/bin" "$SRC/client" "$SRC/requirements.txt" "$PREFIX/"
chmod +x "$PREFIX/client/"*
find "$PREFIX/claude_slurm" -name '__pycache__' -prune -exec rm -rf {} +

if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  "$PYTHON" -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/python" -m pip install -q --upgrade pip
"$PREFIX/venv/bin/python" -m pip install -q -r "$PREFIX/requirements.txt"
"$PREFIX/venv/bin/python" -c 'import mcp.server.fastmcp' \
  || { echo "MCP SDK import failed" >&2; exit 1; }
# Byte-compile now, then make the code read-only (cosmetic against accidents;
# the real protection is the sandbox denyWrite + Edit deny rules).
"$PREFIX/venv/bin/python" -m compileall -q "$PREFIX/claude_slurm"

ln -sfn "$PREFIX/bin/claude-hpc" "$BINDIR/claude-hpc"
ln -sfn "$PREFIX/bin/claude-slurm" "$BINDIR/claude-slurm"
cat > "$PREFIX/install.json" <<JSON
{"prefix": "$PREFIX", "bindir": "$BINDIR",
 "links": ["$BINDIR/claude-hpc", "$BINDIR/claude-slurm"],
 "installed": $(date +%s)}
JSON
chmod -R a-w "$PREFIX/claude_slurm" "$PREFIX/bin" "$PREFIX/client"

echo "installed to $PREFIX"
case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "add $BINDIR to PATH" ;; esac
echo "next: claude-slurm init <project-dir> --image /path/env.sif --partition <p>"
