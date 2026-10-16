#!/usr/bin/env bash
# One-command setup, run and test for the ERPNext Activity Assistant on Debian/Ubuntu (no Docker).
#
#   ./run.sh              install ERPNext if needed, start it, prepare it, seed demo data, run all tests, smoke-test the API
#   ./run.sh install      ERPNext only: system packages, MariaDB, Node, bench, site, ERPNext app (skips what exists)
#   ./run.sh start        start ERPNext (web, scheduler, worker) in the background
#   ./run.sh prepare      ERPNext setup wizard if not done, then Sales Activity DocType, users and API keys -> .env
#   ./run.sh seed         load demo data (idempotent)
#   ./run.sh test         offline tests, then live tests against ERPNext
#   ./run.sh smoke        start the API, call real endpoints, send one chat prompt if an LLM key is configured
#   ./run.sh serve        run the API in the foreground (http://localhost:8100/docs)
#   ./run.sh bot          run the Telegram bot in the foreground
#   ./run.sh stop         stop the processes started by this script
#   ./run.sh status       show what is running
#
# Safe to re-run: finished steps are skipped. Override with environment variables, for example:
#   SITE_NAME=erp.local ADMIN_PASSWORD=secret ERP_PORT=8000 ./run.sh
#   SKIP_INSTALL=1 ./run.sh        (ERPNext is already installed and you only want the project steps)
set -euo pipefail

# ---------- configuration ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench}"
SITE_NAME="${SITE_NAME:-mysite.local}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"
ERPNEXT_BRANCH="${ERPNEXT_BRANCH:-version-15}"
NODE_VERSION="${NODE_VERSION:-18}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-frappe_root_pw}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
ERP_PORT="${ERP_PORT:-8000}"
API_PORT="${API_PORT:-8100}"
# First-run company for the ERPNext setup wizard (demo values)
COMPANY_NAME="${COMPANY_NAME:-Al Mashani Trading Demo}"
COMPANY_ABBR="${COMPANY_ABBR:-AMT}"
COUNTRY="${COUNTRY:-Oman}"
CURRENCY="${CURRENCY:-OMR}"
TIMEZONE_NAME="${TIMEZONE_NAME:-Asia/Muscat}"
FISCAL_YEAR="${FISCAL_YEAR:-$(date +%Y)}"
VENV="$SCRIPT_DIR/venv"
ERP_URL="http://127.0.0.1:$ERP_PORT"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }

mkdir -p "$RUN_DIR"
export PATH="$HOME/.local/bin:$PATH"

load_node() {  # make nvm's node and yarn visible, if nvm is installed
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [[ -s $NVM_DIR/nvm.sh ]]; then set +u; . "$NVM_DIR/nvm.sh"; set -u; fi
}

erp_up() { curl -fsS -m 3 "$ERP_URL/api/method/ping" 2>/dev/null | grep -q pong; }
api_up() { curl -fsS -m 3 "http://127.0.0.1:$API_PORT/health" 2>/dev/null | grep -q ok; }

wait_for() {  # wait_for <description> <seconds> <command...>
  local what=$1 secs=$2; shift 2
  for ((i = 0; i < secs; i++)); do "$@" && return 0; sleep 1; done
  die "Timed out waiting for $what (see logs in $RUN_DIR)"
}

# Start a command detached in its own process group; its pid is the group id so `stop` can kill the whole tree.
spawn() {  # spawn <name> <cwd> <command...>
  local name=$1 cwd=$2; shift 2
  [[ -f $RUN_DIR/$name.pid ]] && kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null && return 0
  # exec inside the background subshell: no wrapper process lingers and the child never inherits our stdout
  ( cd "$cwd" || exit 1; exec setsid "$@" >"$RUN_DIR/$name.log" 2>&1 < /dev/null ) &
  echo $! >"$RUN_DIR/$name.pid"
}

stop_proc() {
  local name=$1 f="$RUN_DIR/$1.pid"
  [[ -f $f ]] || return 0
  local pid; pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; fi
  rm -f "$f"
}

# ---------- preflight ----------
preflight() {
  [[ $EUID -ne 0 ]] || die "Run as a normal user with sudo, not as root."
  command -v apt-get >/dev/null || die "This script supports Debian/Ubuntu only."
  command -v python3 >/dev/null || die "python3 is required."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10 or newer is required."
}

# ---------- 1. install ERPNext ----------
APT_PACKAGES=(git curl cron xvfb libfontconfig pkg-config python3-dev python3-pip python3-venv python3-setuptools pipx
              redis-server mariadb-server mariadb-client libmariadb-dev)

install_system_packages() {
  local missing=()
  for p in "${APT_PACKAGES[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if ((${#missing[@]})); then
    log "Installing system packages: ${missing[*]} (sudo password may be requested)"
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
  else
    log "System packages already installed"
  fi
  if ! command -v wkhtmltopdf >/dev/null; then
    sudo apt-get install -y wkhtmltopdf || warn "wkhtmltopdf not installed; PDF printing will not work (not needed for tests)."
  fi
  systemctl is-active --quiet mariadb || sudo systemctl enable --now mariadb
}

configure_mariadb() {
  local cnf=/etc/mysql/mariadb.conf.d/99-frappe.cnf
  if [[ ! -f $cnf ]]; then
    log "Writing MariaDB charset config"
    sudo tee "$cnf" >/dev/null <<'EOF'
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci

[mysql]
default-character-set = utf8mb4
EOF
    sudo systemctl restart mariadb
  fi
  if ! mysql -uroot -p"$DB_ROOT_PASSWORD" -e 'SELECT 1' >/dev/null 2>&1; then
    log "Setting the MariaDB root password"
    sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('${DB_ROOT_PASSWORD}'); FLUSH PRIVILEGES;"
  fi
}

install_node_and_bench() {
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [[ ! -s $NVM_DIR/nvm.sh ]]; then
    log "Installing nvm"
    curl -fsSo- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  fi
  set +u; . "$NVM_DIR/nvm.sh"
  log "Using Node $NODE_VERSION"
  nvm install "$NODE_VERSION"; nvm use "$NODE_VERSION"
  set -u
  command -v yarn >/dev/null || npm install -g yarn
  command -v uv >/dev/null || { log "Installing uv"; pipx install uv; }
  if ! command -v bench >/dev/null; then
    log "Installing frappe-bench"
    pipx install frappe-bench
    pipx ensurepath >/dev/null || true
  fi
}

install_erpnext() {
  preflight
  if [[ -d $BENCH_DIR/apps/erpnext && -d $BENCH_DIR/sites/$SITE_NAME ]] && command -v bench >/dev/null; then
    log "ERPNext already installed in $BENCH_DIR (site $SITE_NAME)"
    return
  fi
  install_system_packages
  configure_mariadb
  install_node_and_bench
  load_node

  if [[ -d $BENCH_DIR && ( ! -d $BENCH_DIR/apps/frappe || ! -x $BENCH_DIR/env/bin/python ) ]]; then
    log "Removing an incomplete bench at $BENCH_DIR from a previous failed run"
    rm -rf "$BENCH_DIR"
  fi
  if [[ ! -d $BENCH_DIR/apps/frappe ]]; then
    log "Initialising bench ($FRAPPE_BRANCH): this takes several minutes"
    bench init --frappe-branch "$FRAPPE_BRANCH" "$BENCH_DIR" </dev/null
  fi
  cd "$BENCH_DIR"
  [[ -d apps/erpnext ]] || { log "Fetching ERPNext ($ERPNEXT_BRANCH)"; bench get-app --branch "$ERPNEXT_BRANCH" erpnext; }

  # bench new-site / install-app need Frappe's own redis instances (bench start normally launches them)
  start_redis
  if [[ -d sites/$SITE_NAME ]] && ! bench --site "$SITE_NAME" list-apps 2>/dev/null | grep -qx erpnext; then
    log "Dropping a half-installed site $SITE_NAME from a previous failed run"
    bench drop-site "$SITE_NAME" --force --root-password "$DB_ROOT_PASSWORD" || rm -rf "sites/$SITE_NAME"
  fi
  if [[ ! -d sites/$SITE_NAME ]]; then
    log "Creating site $SITE_NAME"
    bench new-site "$SITE_NAME" --mariadb-root-password "$DB_ROOT_PASSWORD" --admin-password "$ADMIN_PASSWORD"
  fi
  bench --site "$SITE_NAME" list-apps | grep -qx erpnext || { log "Installing ERPNext on $SITE_NAME"; bench --site "$SITE_NAME" install-app erpnext; }
  bench use "$SITE_NAME"
  bench --site "$SITE_NAME" set-config developer_mode 0 >/dev/null || true
}

# ---------- 2. start / stop ERPNext ----------
redis_port() {  # redis_port <cache|queue>: read the port from the bench config, falling back to bench defaults
  python3 - "$BENCH_DIR" "$1" <<'EOF'
import json, sys, re
bench, kind = sys.argv[1], sys.argv[2]
default = {"cache": 13000, "queue": 11000}[kind]
try:
    cfg = json.load(open(f"{bench}/sites/common_site_config.json"))
    print(re.search(r":(\d+)", cfg[f"redis_{kind}"]).group(1))
except Exception:
    print(default)
EOF
}

start_redis() {
  cd "$BENCH_DIR"
  for kind in cache queue; do
    local port; port=$(redis_port "$kind")
    redis-cli -p "$port" ping >/dev/null 2>&1 || redis-server "config/redis_$kind.conf" --daemonize yes >/dev/null
  done
  sleep 1
}

start_erpnext() {
  [[ -d $BENCH_DIR/sites/$SITE_NAME ]] || die "ERPNext is not installed in $BENCH_DIR. Run: ./run.sh install"
  if erp_up; then log "ERPNext already running at $ERP_URL"; return; fi
  command -v bench >/dev/null || die "bench not found on PATH"
  load_node
  log "Starting ERPNext (web, scheduler, worker) at $ERP_URL"
  systemctl is-active --quiet mariadb || sudo systemctl start mariadb
  start_redis
  # The asset watcher that `bench start` also launches needs yarn and is not needed here, so run the three services directly.
  spawn erp-web "$BENCH_DIR" bench serve --port "$ERP_PORT"
  spawn erp-schedule "$BENCH_DIR" bench schedule
  spawn erp-worker "$BENCH_DIR" bench worker
  wait_for "ERPNext to answer on $ERP_URL" 120 erp_up
  log "ERPNext is up (login: Administrator / $ADMIN_PASSWORD)"
}

stop_all() {
  log "Stopping processes started by this script"
  for n in api bot erp-web erp-schedule erp-worker; do stop_proc "$n"; done
  warn "Redis instances and MariaDB were left running (they may be shared). Stop bench redis with: redis-cli -p <port> shutdown nosave"
}

# ---------- 3. prepare ERPNext and the project ----------
setup_wizard_if_needed() {
  cd "$BENCH_DIR"; load_node
  local done_flag
  done_flag=$(bench --site "$SITE_NAME" execute frappe.db.get_single_value --args '["System Settings", "setup_complete"]' 2>/dev/null | tail -1 || true)
  if [[ $done_flag == 1 ]]; then log "ERPNext setup wizard already completed"; return; fi
  log "Running the ERPNext setup wizard ($COMPANY_NAME, $COUNTRY, $CURRENCY): this takes a minute or two"
  bench --site "$SITE_NAME" execute frappe.desk.page.setup_wizard.setup_wizard.setup_complete --kwargs "{'args': {
    'language': 'English', 'country': '$COUNTRY', 'timezone': '$TIMEZONE_NAME', 'currency': '$CURRENCY',
    'company_name': '$COMPANY_NAME', 'company_abbr': '$COMPANY_ABBR', 'chart_of_accounts': 'Standard',
    'fy_start_date': '$FISCAL_YEAR-01-01', 'fy_end_date': '$FISCAL_YEAR-12-31', 'full_name': 'Admin User',
    'email': 'admin@example.com', 'password': '$ADMIN_PASSWORD'}}" | tail -3
}

project_env() {  # what the project scripts need to find and log in to ERPNext
  export ERP_URL ERP_ADMIN_USERNAME="${ERP_ADMIN_USERNAME:-Administrator}" ERP_ADMIN_PASSWORD="$ADMIN_PASSWORD"
}

install_python_deps() {
  if [[ ! -x $VENV/bin/python ]]; then log "Creating the Python virtualenv"; python3 -m venv "$VENV"; fi
  log "Installing Python dependencies"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
}

prepare() {
  erp_up || start_erpnext
  setup_wizard_if_needed
  install_python_deps
  project_env
  log "Preparing ERPNext for the assistant (Sales Activity DocType, users, API keys, .env)"
  (cd "$SCRIPT_DIR" && "$VENV/bin/python" scripts/setup_erpnext.py)
}

seed() {
  erp_up || start_erpnext
  [[ -x $VENV/bin/python ]] || install_python_deps
  project_env
  log "Loading demo data"
  (cd "$SCRIPT_DIR" && "$VENV/bin/python" scripts/seed_demo.py)
}

# ---------- 4. tests ----------
run_tests() {
  erp_up || start_erpnext
  [[ -x $VENV/bin/python ]] || install_python_deps
  project_env
  cd "$SCRIPT_DIR"
  log "Offline tests (no network, stubbed ERPNext, scripted LLM)"
  "$VENV/bin/pytest" tests --ignore=tests/live -q
  log "Live tests against ERPNext at $ERP_URL (they remove the records they create)"
  "$VENV/bin/pytest" tests/live -q
}

env_value() { grep -E "^$1=" "$SCRIPT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }

start_api() {
  [[ -f $SCRIPT_DIR/.env ]] || die ".env is missing. Run: ./run.sh prepare"
  if api_up; then return; fi
  project_env
  spawn api "$SCRIPT_DIR" "$VENV/bin/uvicorn" app.main:app_from_env --factory --port "$API_PORT"
  wait_for "the API on port $API_PORT" 30 api_up
}

smoke() {
  erp_up || start_erpnext
  start_api
  log "Smoke test of the API against real ERPNext data"
  local key base="http://127.0.0.1:$API_PORT" fail=0
  key=$("$VENV/bin/python" - "$SCRIPT_DIR" <<'EOF'
import sys; sys.path.insert(0, sys.argv[1])
from app.config import Settings
print(next(k for k, c in Settings.from_env().api_keys.items() if c == "manager"))
EOF
)
  check() {  # check <description> <path> <expected substring>
    local body code
    body=$(curl -sS -m 30 -w '\n%{http_code}' -H "X-API-Key: $key" "$base$2") || { warn "FAIL $1: request error"; fail=1; return; }
    code=${body##*$'\n'}; body=${body%$'\n'*}
    if [[ $code == 200 && $body == *"$3"* ]]; then printf '  ok   %s\n' "$1"; else warn "FAIL $1 (HTTP $code): ${body:0:200}"; fail=1; fi
  }
  check "clients (fuzzy: 'Nazwa Builders' finds Nizwa Builders)" "/v1/clients?q=Nazwa%20Builders" "Nizwa Builders"
  check "items with ERPNext prices"                              "/v1/items?q=workstation" "Workstation 4-Seat"
  check "quotation summary (counts and totals)"                  "/v1/summary?doctype=Quotation" "\"currency\""
  check "finalized sales orders"                                 "/v1/sales-orders?finalized=true" "SAL-ORD"
  check "activities (manager sees everyone's)"                   "/v1/activities?limit=50" "activity_type"
  if [[ -n ${LLM_API_KEY:-$(env_value LLM_API_KEY)} ]]; then
    log "Sending one chat prompt through the configured LLM"
    curl -sS -m 120 -X POST "$base/v1/prompt" -H "X-API-Key: $key" -H "Content-Type: application/json" \
      -d '{"message": "How many quotations do we have and what is their total value?", "session_id": "smoke"}' | head -c 800; echo
  else
    warn "LLM_API_KEY is not set in .env, so the chat prompt was skipped. Add it (see .env.example) and run: ./run.sh smoke"
  fi
  stop_proc api
  ((fail == 0)) || die "Smoke test failed"
  log "Smoke test passed"
}

serve() {
  [[ -f $SCRIPT_DIR/.env ]] || die ".env is missing. Run: ./run.sh prepare"
  erp_up || start_erpnext
  project_env
  cd "$SCRIPT_DIR"
  log "API on http://localhost:$API_PORT (docs at /docs). Ctrl+C to stop."
  exec "$VENV/bin/uvicorn" app.main:app_from_env --factory --port "$API_PORT"
}

bot() {
  [[ -f $SCRIPT_DIR/.env ]] || die ".env is missing. Run: ./run.sh prepare"
  [[ -n $(env_value TELEGRAM_BOT_TOKEN) ]] || die "Set TELEGRAM_BOT_TOKEN and TELEGRAM_USERS in .env first (see .env.example)."
  api_up || start_api
  cd "$SCRIPT_DIR"
  exec "$VENV/bin/python" telegram_bot.py "$@"
}

status() {
  printf 'ERPNext  %s  %s\n' "$ERP_URL" "$(erp_up && echo UP || echo down)"
  printf 'API      http://127.0.0.1:%s  %s\n' "$API_PORT" "$(api_up && echo UP || echo down)"
  printf 'bench    %s (site %s)  %s\n' "$BENCH_DIR" "$SITE_NAME" "$([[ -d $BENCH_DIR/sites/$SITE_NAME ]] && echo installed || echo 'not installed')"
  printf 'venv     %s  %s\n' "$VENV" "$([[ -x $VENV/bin/python ]] && echo ready || echo 'not created')"
  printf '.env     %s\n' "$([[ -f $SCRIPT_DIR/.env ]] && echo present || echo missing)"
}

all() {
  [[ ${SKIP_INSTALL:-0} == 1 ]] || install_erpnext
  start_erpnext
  prepare
  seed
  run_tests
  smoke
  cat <<EOF

$(printf '\033[1;32m')All done.$(printf '\033[0m')
  ERPNext   $ERP_URL   (Administrator / $ADMIN_PASSWORD; demo users alice, bob, manager @example.com)
  API       ./run.sh serve        then open http://localhost:$API_PORT/docs
  Telegram  put TELEGRAM_BOT_TOKEN and TELEGRAM_USERS in .env, then ./run.sh bot
  LLM       put LLM_API_KEY in .env (Claude or OpenAI-compatible, see .env.example) for natural-language prompts
  Stop      ./run.sh stop
EOF
}

case "${1:-all}" in
  all)       all ;;
  install)   install_erpnext ;;
  start)     start_erpnext ;;
  prepare)   prepare ;;
  seed)      seed ;;
  test)      run_tests ;;
  smoke)     smoke ;;
  serve)     serve ;;
  bot)       shift || true; bot "$@" ;;
  stop)      stop_all ;;
  status)    status ;;
  -h|--help|help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *)         die "Unknown command '$1'. Try: ./run.sh help" ;;
esac
