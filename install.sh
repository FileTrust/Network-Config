#!/usr/bin/env bash
# BEGIN FILETRUST INSTALL BANNER — from root singature.txt
filetrust_install_banner() {
  local banner_dir banner_version='unknown' banner_line
  banner_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -r "$banner_dir/VERSION" ]]; then
    IFS= read -r banner_version < "$banner_dir/VERSION" || [[ -n $banner_version ]]
    banner_version=${banner_version%$'\r'}
  fi
  cat <<'FILETRUST_BANNER_TOP'
███████╗██╗██╗     ███████╗████████╗██████╗ ██╗   ██╗███████╗████████╗
██╔════╝██║██║     ██╔════╝╚══██╔══╝██╔══██╗██║   ██║██╔════╝╚══██╔══╝
█████╗  ██║██║     █████╗     ██║   ██████╔╝██║   ██║███████╗   ██║
██╔══╝  ██║██║     ██╔══╝     ██║   ██╔══██╗██║   ██║╚════██║   ██║
██║     ██║███████╗███████╗   ██║   ██║  ██║╚██████╔╝███████║   ██║
╚═╝     ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝


Author: Ali Farahpoor
Company: Electra Sphere Innovations (ElectraSI.es)
Location: Barcelona, Spain
Contact: support@filetrust.io

FILETRUST_BANNER_TOP
  printf 'Version: %s %s\n' 'NetworkConfig' "$banner_version"
  cat <<'FILETRUST_BANNER_BOTTOM'
Release Date: 25 October 2026

Documentation: https://docs.filetrust.io
Website: https://www.filetrust.io
FILETRUST_BANNER_BOTTOM
  printf '\n'
}
filetrust_install_banner
unset -f filetrust_install_banner
# END FILETRUST INSTALL BANNER
#
# One-command setup for the FileTrust network config service.
#
#   ./install.sh             set up and keep it running: a service that starts
#                            with the machine and comes back if it ever stops
#   ./install.sh --no-service  set up only; you start it yourself
#   ./install.sh --user      install the service for this user rather than the
#                            machine (no root needed)
#   ./install.sh --rpc URLS  the chain nodes, comma separated, best first:
#                            --rpc http://node1:8545,http://node2:8545
#   ./install.sh --bundle F  the file the deploy wrote. Defaults to
#                            network_config_bundle.json beside this script.
#   ./install.sh --port N    what it listens on (default 8989)
#   ./install.sh --host H    what it binds to (default 0.0.0.0)
#   ./install.sh --public-url URL
#                            the address it tells callers to use, e.g.
#                            https://config.filetrust.io
#   ./install.sh --discovery-urls URLS
#   ./install.sh --price-url URL
#                            what it publishes for the other services
#   ./install.sh --dry-run   show what would happen, change nothing
#   ./install.sh --verbose   show every check, not only what needs doing
#
# This service hands out the network's settings: the deployed contract
# addresses, their ABIs, and where the other services answer. Every Data
# Owner, Vault Keeper and Guardian reads it once at startup.
#
# It needs no chain on this machine and no Python packages at all -- only the
# one file your deploy wrote. Copy that file here and it serves it, and when a
# later deploy replaces it, it notices and re-reads it without a restart.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="filetrust-networkconfig"
BUNDLE_NAME="network_config_bundle.json"
WANT_SERVICE=1
USER_UNIT=0
DRY_RUN=0
VERBOSE=0
RPC_LIST=""
BUNDLE_FILE=""
PORT=""
HOST=""
PUBLIC_URL=""
DISCOVERY_URLS=""
PRICE_URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --service) WANT_SERVICE=1 ;;
    --no-service) WANT_SERVICE=0 ;;
    --user) USER_UNIT=1 ;;
    --rpc|--rpc-urls) shift; RPC_LIST="${1:-}" ;;
    --bundle|--bundle-file) shift; BUNDLE_FILE="${1:-}" ;;
    --port) shift; PORT="${1:-}" ;;
    --host) shift; HOST="${1:-}" ;;
    --public-url) shift; PUBLIC_URL="${1:-}" ;;
    --discovery-urls) shift; DISCOVERY_URLS="${1:-}" ;;
    --price-url|--ftu-price-url) shift; PRICE_URL="${1:-}" ;;
    --dry-run) DRY_RUN=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    -h|--help) awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' \
                 "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
step() { [ "$VERBOSE" = 1 ] && say "$*"; return 0; }
note() { [ "$VERBOSE" = 1 ] && printf '%s\n' "$*"; return 0; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── Python ───────────────────────────────────────────────────────────────────
# Nothing to install: this service uses only what Python ships with. No
# virtual environment either, because there would be nothing in it.
step "Checking Python"
command -v python3 >/dev/null 2>&1 || die \
  "python3 is not installed. Install it with your package manager and run this again."
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')"
[ "$PY_OK" = 1 ] || die "Python 3.9 or newer is needed. This machine has $(python3 -V 2>&1)."
note "  nothing to install: the standard library is enough"

# ── Your settings file ───────────────────────────────────────────────────────
step "Checking your settings file"
DEFAULT_CONFIG="$APP_DIR/config.default.json"
CONFIG="$APP_DIR/config.json"
if [ "$DRY_RUN" = 1 ]; then
  note "  would create or update config.json"
elif [ ! -f "$DEFAULT_CONFIG" ]; then
  note "  this copy ships config.json directly; nothing to merge"
elif [ ! -f "$CONFIG" ]; then
  cp "$DEFAULT_CONFIG" "$CONFIG"
  say "Your settings file"
  echo "  created config.json from the shipped defaults"
else
  FT_CONFIG="$CONFIG" FT_DEFAULT="$DEFAULT_CONFIG" python3 <<'SETTINGS_MERGE'
import json, os
from pathlib import Path

mine = Path(os.environ["FT_CONFIG"])
shipped = Path(os.environ["FT_DEFAULT"])
try:
    current = json.loads(mine.read_text(encoding="utf-8"))
    defaults = json.loads(shipped.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"  could not read the settings files ({exc}); leaving yours alone")
    raise SystemExit(0)

def is_heading(key):
    # Section titles, not settings.
    return key.startswith("##")

added = [k for k in defaults if k not in current and not is_heading(k)]
removed = [k for k in current if k not in defaults and not is_heading(k)]
rewritten = [k for k in current if is_heading(k) and k not in defaults]
if not added and not removed and not rewritten:
    raise SystemExit(0)
merged = {key: current.get(key, value) for key, value in defaults.items()}
mine.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
if added:
    print(f"  added {len(added)} new setting(s): " + ", ".join(added[:4]))
if removed:
    print(f"  removed {len(removed)} setting(s) the service no longer reads")
SETTINGS_MERGE
fi

# ── What you asked for on the command line ───────────────────────────────────
if [ "$DRY_RUN" = 0 ]; then
  FT_CONFIG="$CONFIG" \
  FT_RPC="$RPC_LIST" FT_BUNDLE="$BUNDLE_FILE" FT_PORT="$PORT" FT_HOST="$HOST" \
  FT_PUBLIC="$PUBLIC_URL" FT_DISCOVERY="$DISCOVERY_URLS" FT_PRICE="$PRICE_URL" \
  python3 - <<'APPLY'
import json, os
from pathlib import Path

path = Path(os.environ["FT_CONFIG"])
config = json.loads(path.read_text(encoding="utf-8"))
changed = []

def put(key, value):
    value = str(value or "").strip()
    if value and config.get(key) != value:
        config[key] = value
        changed.append(key)

put("rpc_urls", os.environ.get("FT_RPC"))
put("bundle_file", os.environ.get("FT_BUNDLE"))
put("host", os.environ.get("FT_HOST"))
put("public_ip", os.environ.get("FT_PUBLIC"))
put("discovery_urls", os.environ.get("FT_DISCOVERY"))
put("ftu_price_url", os.environ.get("FT_PRICE"))

port = str(os.environ.get("FT_PORT") or "").strip()
if port.isdigit() and config.get("port") != int(port):
    config["port"] = int(port)
    changed.append("port")

if changed:
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print("  set " + ", ".join(changed))
APPLY
fi

# ── The file your deploy wrote ───────────────────────────────────────────────
# Everything this service publishes comes from that one file. Without it there
# is nothing to serve, and saying so now is better than a service that starts
# and answers 503 to every caller.
if [ "$DRY_RUN" = 0 ]; then
  step "Checking the deploy file"
  FT_CONFIG="$CONFIG" FT_DIR="$APP_DIR" FT_NAME="$BUNDLE_NAME" python3 - <<'BUNDLE'
import json, os
from pathlib import Path

config = json.loads(Path(os.environ["FT_CONFIG"]).read_text(encoding="utf-8"))
given = str(config.get("bundle_file") or "").strip()
path = Path(given) if given else Path(os.environ["FT_DIR"]) / os.environ["FT_NAME"]

if not path.is_file():
    print(f"missing {path}")
    raise SystemExit(0)

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"unreadable {path}: {exc}")
    raise SystemExit(0)

files = data.get("artifact_files") or {}
required = data.get("required_artifact_files") or list(files)
missing = [name for name in required if name not in files]
contracts = (data.get("deployed_addresses") or {}).get("contracts") or {}
if missing:
    print(f"short {len(files)}/{len(required)} " + ",".join(missing[:4]))
else:
    print(f"ok {len(files)} ABIs, {len(contracts)} contracts, "
          f"chain {(data.get('deployed_addresses') or {}).get('chainId', '?')}")
BUNDLE
fi > "$APP_DIR/.bundle-check" 2>&1 || true

if [ "$DRY_RUN" = 0 ]; then
  BUNDLE_STATE="$(cat "$APP_DIR/.bundle-check" 2>/dev/null || true)"
  rm -f "$APP_DIR/.bundle-check"
  case "$BUNDLE_STATE" in
    *ok*)
      say "The deploy file"
      echo "  ${BUNDLE_STATE#*ok }" ;;
    *missing*)
      say "The deploy file"
      warn "  not here yet: ${BUNDLE_STATE#*missing }"
      warn "  Your deploy writes $BUNDLE_NAME. Copy it next to this script."
      warn "  The service cannot answer anything until you do."
      WANT_SERVICE=0 ;;
    *short*)
      say "The deploy file"
      warn "  it is incomplete: ${BUNDLE_STATE#*short }"
      warn "  Run the deploy again; the service refuses to start on a short one."
      WANT_SERVICE=0 ;;
    *)
      [ -n "$BUNDLE_STATE" ] && warn "  $BUNDLE_STATE" ;;
  esac
fi

# ── Keeping it running ───────────────────────────────────────────────────────
if [ "$WANT_SERVICE" = 1 ] && ! command -v systemctl >/dev/null 2>&1; then
  warn "  this machine has no systemd, so nothing can start it for you."
  warn "  start it yourself with: python3 $APP_DIR/network_config_server.py"
  WANT_SERVICE=0
fi

if [ "$WANT_SERVICE" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  if [ "$USER_UNIT" = 1 ]; then UNIT_TARGET="default.target"; else UNIT_TARGET="multi-user.target"; fi
  RUN_AS=""
  if [ "$USER_UNIT" = 0 ]; then
    # A machine-wide unit installed by root would also run as root, in a
    # directory somebody else owns.
    DIR_OWNER="$(stat -c '%U' "$APP_DIR" 2>/dev/null || true)"
    DIR_GROUP="$(stat -c '%G' "$APP_DIR" 2>/dev/null || true)"
    if [ -n "${DIR_OWNER:-}" ] && [ "$DIR_OWNER" != "root" ]; then
      RUN_AS="User=$DIR_OWNER
Group=$DIR_GROUP"
    fi
  fi
  UNIT_BODY="[Unit]
Description=FileTrust network config service
Documentation=https://github.com/FileTrust/Network-Config
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
${RUN_AS:+$RUN_AS
}WorkingDirectory=$APP_DIR
ExecStart=$(command -v python3) -u $APP_DIR/network_config_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=$UNIT_TARGET
"
  say "Service"
  if [ "$USER_UNIT" = 1 ]; then
    UNIT_PATH="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    mkdir -p "$(dirname "$UNIT_PATH")"
    printf '%s' "$UNIT_BODY" > "$UNIT_PATH"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME" >/dev/null 2>&1 \
      || warn "  could not enable it; see: systemctl --user status $SERVICE_NAME"
    # Without this it stops when you log out and does not come back at boot.
    command -v loginctl >/dev/null 2>&1 && {
      loginctl enable-linger "$USER" >/dev/null 2>&1 \
        || sudo loginctl enable-linger "$USER" >/dev/null 2>&1 \
        || warn "  could not enable lingering; it will stop when you log out."
    }
    echo "  installed for $USER, starts at boot, restarts if it ever stops"
  else
    UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
    if [ "$(id -u)" = 0 ]; then
      printf '%s' "$UNIT_BODY" > "$UNIT_PATH"
      systemctl daemon-reload
      systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1 \
        || warn "  could not enable it; see: systemctl status $SERVICE_NAME"
      echo "  installed, starts at boot, restarts if it ever stops"
    elif command -v sudo >/dev/null 2>&1; then
      # -n: never stop at a password prompt. Under a launcher there is nobody
      # to type one, and a prompt on a closed stdin reads as a hang.
      printf '%s' "$UNIT_BODY" | sudo -n tee "$UNIT_PATH" >/dev/null 2>&1 \
        && sudo -n systemctl daemon-reload \
        && sudo -n systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1 \
        || true
    # Say it is installed only if it is. This branch used to print success
    # whatever sudo did, so an install that never wrote the unit still
    # reported "installed, starts at boot" -- and nothing started at boot.
      if [ -f "$UNIT_PATH" ]; then
        echo "  installed, starts at boot, restarts if it ever stops"
      else
        warn "  NOT installed: writing $UNIT_PATH needs root, and this account"
        warn "  could not get it. Run this again as root, or use --user."
        WANT_SERVICE=0
      fi
    else
      warn "  installing a machine-wide service needs root. Run this again as"
      warn "  root, or with --user to install it for $USER only."
      WANT_SERVICE=0
    fi
  fi
fi

# ── Does it answer? ──────────────────────────────────────────────────────────
if [ "$WANT_SERVICE" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  SERVICE_PORT="$(FT_CONFIG="$CONFIG" python3 -c "
import json, os
print(int(json.load(open(os.environ['FT_CONFIG'])).get('port', 8989)))")"
  say "Checking it answers"
  ANSWERED=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if OUT="$(python3 - "$SERVICE_PORT" <<'HEALTH'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2) as r:
        data = json.loads(r.read().decode("utf-8"))
except Exception as exc:
    print(f"no {exc}")
    raise SystemExit(1)
up, total = data.get("nodes_up"), data.get("nodes_total")
print(f"yes chain nodes answering: {up}/{total}" if total else "yes")
HEALTH
)"; then ANSWERED=1; break; fi
    sleep 1
  done
  if [ "$ANSWERED" = 1 ]; then
    echo "  ${OUT#yes}"
    echo "  http://127.0.0.1:$SERVICE_PORT/v1/network-config"
  else
    warn "  it is not answering yet. The usual reason is that no chain node"
    warn "  replied, which this service reports as unhealthy. Check with:"
    if [ "$USER_UNIT" = 1 ]; then
      warn "      journalctl --user -u $SERVICE_NAME -n 30"
    else
      warn "      journalctl -u $SERVICE_NAME -n 30"
    fi
  fi
fi

say "Done"
if [ "$WANT_SERVICE" = 1 ] && [ "$USER_UNIT" = 1 ]; then
  echo "      systemctl --user status $SERVICE_NAME"
elif [ "$WANT_SERVICE" = 1 ]; then
  echo "      systemctl status $SERVICE_NAME"
else
  echo "      python3 $APP_DIR/network_config_server.py"
fi
