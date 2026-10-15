#!/usr/bin/env bash
# Mira Server installer for Ubuntu 22.04 and 24.04.
#
#   curl -fsSLO https://raw.githubusercontent.com/MIRA-Intelligence/mira/<release>/deploy/mira-server/install.sh
#   sudo bash install.sh           # interactive (whiptail dialogs)
#   sudo bash install.sh --help    # options, unattended installs
#
# Every step first checks the host and skips what is already in place. After a
# failure, fix the problem and run the installer again: it continues where it
# stopped with the same answers. Run it again later (or `sudo mira-server-install`)
# to upgrade, reconfigure or manage users. Answers are kept in
# /var/lib/mira-server-installer/state (never passwords).
# Uninstall with `sudo mira-server-uninstall`.

# ---- begin common (keep identical in install.sh and uninstall.sh) ----
set -Eeuo pipefail
umask 022
export DEBIAN_FRONTEND=noninteractive

STATE_DIR=/var/lib/mira-server-installer
STATE_FILE=$STATE_DIR/state
BACKUP_DIR=$STATE_DIR/backup
LOG_FILE=/var/log/mira-server-install.log
VENV=/opt/mira
UV_DIR=/opt/mira-uv
UV_PYTHON_DIR=/opt/mira-python
SRC_DIR=/opt/mira-src
LAUNCHER=/usr/local/libexec/mira/engine-launch
SUDOERS_FILE=/etc/sudoers.d/mira-server
PAM_SERVER=/etc/pam.d/mira-server
PAM_ENGINE=/etc/pam.d/mira-engine
CONFIG_DIR=/etc/mira-server
CONFIG_FILE=$CONFIG_DIR/mira-server.toml
CLIENT_CA=$CONFIG_DIR/client-ca.crt
CLIENT_SETUP=$CONFIG_DIR/CLIENT-SETUP.txt
DATA_DIR=/var/lib/mira-server
POLICY_FILE=/etc/mira/policy.toml
APPARMOR_FILE=/etc/apparmor.d/mira-bwrap
SLICE_FILE=/etc/systemd/system/user-.slice.d/50-mira.conf
UNIT_FILE=/etc/systemd/system/mira-server.service
CADDYFILE=/etc/caddy/Caddyfile
CADDY_SNIPPET=/etc/caddy/mira-server.caddy
CADDY_ROOT_CA=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
CADDY_REPO_LIST=/etc/apt/sources.list.d/caddy-stable.list
CADDY_REPO_KEY=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
SBIN_INSTALL=/usr/local/sbin/mira-server-install
SBIN_UNINSTALL=/usr/local/sbin/mira-server-uninstall
SERVER_USER=mira-server
USERS_GROUP=mira-users
ADMINS_GROUP=mira-admins
SERVER_URL_LOCAL=http://127.0.0.1:8740
MANAGED_MARK="# Managed by the Mira Server installer."
TITLE="Mira Server"
INTERACTIVE=1
CURRENT_STEP=""

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE"; }
say() { printf '%s\n' "$*"; log "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; log "warning: $*"; }
# Runs a command with its output in the log only. A failure reaches the ERR trap.
run() { log "+ $*"; "$@" >>"$LOG_FILE" 2>&1; }

require_root() {
	[[ $(id -u) -eq 0 ]] || { printf 'Run this as root: sudo bash %s\n' "$0" >&2; exit 1; }
}

prepare_log() {
	touch "$LOG_FILE"
	chmod 600 "$LOG_FILE"
	log "---- $(basename "$0") $*"
}

# ---- state: answers and facts, sourced as shell variables named S_<key>
state_load() {
	if [[ -f $STATE_FILE ]]; then
		# shellcheck disable=SC1090
		source "$STATE_FILE"
	fi
}

state_set() {
	local key="S_$1" value=$2 tmp
	printf -v "$key" '%s' "$value"
	mkdir -p "$STATE_DIR"
	chmod 700 "$STATE_DIR"
	tmp=$(mktemp "$STATE_DIR/.state.XXXXXX")
	if [[ -f $STATE_FILE ]]; then
		grep -v "^$key=" "$STATE_FILE" >"$tmp" || true
	fi
	printf '%s=%q\n' "$key" "$value" >>"$tmp"
	chmod 600 "$tmp"
	mv -f "$tmp" "$STATE_FILE"
}

# Copies a file to the backup directory and prints the copy's path.
backup_file() {
	local path=$1 dest
	mkdir -p "$BACKUP_DIR"
	chmod 700 "$STATE_DIR" "$BACKUP_DIR"
	dest="$BACKUP_DIR/$(printf '%s' "$path" | tr '/' '_').$(date +%Y%m%d%H%M%S)"
	cp -p "$path" "$dest"
	log "backed up $path to $dest"
	printf '%s\n' "$dest"
}

# ---- dialogs (whiptail; plain prompts without it; defaults when unattended)
have_whiptail() { command -v whiptail >/dev/null 2>&1; }

ui_size() {
	local lines cols
	lines=$(tput lines 2>/dev/null || echo 24)
	cols=$(tput cols 2>/dev/null || echo 80)
	UI_H=$((lines - 4))
	UI_W=$((cols - 8))
	((UI_H > 30)) && UI_H=30
	((UI_W > 92)) && UI_W=92
	((UI_H < 12)) && UI_H=12
	((UI_W < 50)) && UI_W=50
	return 0
}

# Exits with 130. Inside $(...) the caller's assignment fails with 130, which
# on_error treats as a cancel too.
cancelled() {
	printf '\nCancelled. Run the command again to continue.\n' >&2
	exit 130
}

ui_msg() {
	if ((INTERACTIVE)) && have_whiptail; then
		ui_size
		whiptail --title "$TITLE" --msgbox "$1" "$UI_H" "$UI_W" || true
	else
		printf '\n%s\n\n' "$1"
	fi
}

ui_text_file() {
	if ((INTERACTIVE)) && have_whiptail; then
		ui_size
		whiptail --title "$TITLE" --scrolltext --textbox "$1" "$UI_H" "$UI_W" || true
	else
		printf '\n'
		cat "$1"
		printf '\n'
	fi
}

# ui_yesno TEXT [yes|no]: the default is used when unattended.
ui_yesno() {
	local text=$1 default=${2:-yes} reply
	if ! ((INTERACTIVE)); then
		[[ $default == yes ]]
		return
	fi
	if have_whiptail; then
		ui_size
		local flags=()
		[[ $default == no ]] && flags=(--defaultno)
		whiptail --title "$TITLE" "${flags[@]}" --yesno "$text" "$UI_H" "$UI_W"
		return
	fi
	read -r -p "$text [$([[ $default == yes ]] && echo Y/n || echo y/N)] " reply || cancelled
	reply=${reply:-$default}
	[[ $reply =~ ^[Yy] ]]
}

# ui_input TEXT DEFAULT: prints the answer.
ui_input() {
	local text=$1 default=${2:-} reply
	if ! ((INTERACTIVE)); then
		printf '%s' "$default"
		return
	fi
	if have_whiptail; then
		ui_size
		reply=$(whiptail --title "$TITLE" --inputbox "$text" "$UI_H" "$UI_W" "$default" 3>&1 1>&2 2>&3) || cancelled
	else
		read -r -p "$text [$default] " reply || cancelled
		reply=${reply:-$default}
	fi
	printf '%s' "$reply"
}

ui_password() {
	local reply
	if have_whiptail; then
		ui_size
		reply=$(whiptail --title "$TITLE" --passwordbox "$1" "$UI_H" "$UI_W" 3>&1 1>&2 2>&3) || return 1
	else
		read -r -s -p "$1 " reply || return 1
		printf '\n' >&2
	fi
	printf '%s' "$reply"
}

# ui_menu TEXT DEFAULT TAG DESCRIPTION...: prints the chosen tag.
ui_menu() {
	local text=$1 default=$2 reply i
	shift 2
	if ! ((INTERACTIVE)); then
		printf '%s' "$default"
		return
	fi
	if have_whiptail; then
		ui_size
		reply=$(whiptail --title "$TITLE" --default-item "$default" --menu "$text" "$UI_H" "$UI_W" \
			$(($# / 2)) "$@" 3>&1 1>&2 2>&3) || cancelled
	else
		local items=("$@")
		printf '\n%s\n' "$text" >&2
		for ((i = 0; i < ${#items[@]}; i += 2)); do
			printf '  %-12s %s\n' "${items[i]}" "${items[i + 1]}" >&2
		done
		read -r -p "Choice [$default] " reply || cancelled
		reply=${reply:-$default}
	fi
	printf '%s' "$reply"
}

# ui_checklist TEXT TAG DESCRIPTION on|off ...: prints the chosen tags, one per line.
ui_checklist() {
	local text=$1 reply i
	shift
	if ! ((INTERACTIVE)); then
		for ((i = 1; i <= $#; i += 3)); do
			local state=$((i + 2))
			[[ ${!state} == on ]] && printf '%s\n' "${!i}"
		done
		return 0
	fi
	if have_whiptail; then
		ui_size
		reply=$(whiptail --title "$TITLE" --separate-output --checklist "$text" "$UI_H" "$UI_W" \
			$(($# / 3)) "$@" 3>&1 1>&2 2>&3) || cancelled
		printf '%s\n' "$reply"
		return 0
	fi
	printf '\n%s\n' "$text" >&2
	for ((i = 1; i <= $#; i += 3)); do
		local desc=$((i + 1)) state=$((i + 2))
		if ui_yesno "  ${!desc}?" "$([[ ${!state} == on ]] && echo yes || echo no)"; then
			printf '%s\n' "${!i}"
		fi
	done
}

# ---- errors
on_error() {
	local code=$? command=$BASH_COMMAND
	trap - ERR
	# A failing command substitution reports in the parent shell.
	if ((BASH_SUBSHELL > 0)); then
		exit "$code"
	fi
	if ((code == 130)); then
		log "cancelled"
		exit 130
	fi
	log "failed (exit $code) in step '${CURRENT_STEP:-none}': $command"
	if [[ -n $CURRENT_STEP ]]; then
		state_set failed_step "$CURRENT_STEP"
	fi
	report_failure "$code" "$command"
	exit "$code"
}

last_log_lines() { tail -n "${1:-15}" "$LOG_FILE" 2>/dev/null | sed 's/^/  /'; }

check_ubuntu() {
	local id="" version=""
	if [[ -r /etc/os-release ]]; then
		# shellcheck source=/dev/null
		id=$(. /etc/os-release && printf '%s' "${ID:-}")
		# shellcheck source=/dev/null
		version=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
	fi
	if [[ $id != ubuntu || ! $version =~ ^(22\.04|24\.04)$ ]]; then
		ui_yesno "This installer supports Ubuntu 22.04 and 24.04; this host is ${id:-unknown} ${version:-}.

Continue anyway?" no || exit 1
	fi
	command -v systemctl >/dev/null 2>&1 || { printf 'systemd is required.\n' >&2; exit 1; }
}

# The first three numbers of a version, compared like the desktop app does.
version_ge() {
	local a b
	a=$(grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' <<<"$1" || true)
	b=$(grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' <<<"$2" || true)
	[[ -n $a && -n $b ]] || return 1
	[[ $(printf '%s\n%s\n' "$a" "$b" | sort -V | head -n 1) == "$b" ]]
}

pkg_installed() { [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true) == "install ok installed" ]]; }

in_group() {
	local groups
	groups=" $(id -nG "$1" 2>/dev/null || true) "
	[[ $groups == *" $2 "* ]]
}

caddyfile_body() { sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "$CADDYFILE" 2>/dev/null || true; }

# missing | empty | managed | default (the Ubuntu package's) | mira (only Mira
# sites, e.g. written by hand from the guide) | custom
caddyfile_kind() {
	local body
	[[ -f $CADDYFILE ]] || { echo missing; return; }
	grep -qF "$MANAGED_MARK" "$CADDYFILE" && { echo managed; return; }
	body=$(caddyfile_body)
	[[ -z $body ]] && { echo empty; return; }
	if grep -q '/usr/share/caddy' <<<"$body" && grep -q 'file_server' <<<"$body" &&
		! grep -q 'reverse_proxy' <<<"$body"; then
		echo default
		return
	fi
	if grep -q 'reverse_proxy[[:space:]]\+127\.0\.0\.1:8740' <<<"$body" &&
		! grep -E 'reverse_proxy' <<<"$body" | grep -qv '127\.0\.0\.1:8740' &&
		! grep -qE 'file_server|php_fastcgi|^[[:space:]]*root[[:space:]]' <<<"$body"; then
		echo mira
		return
	fi
	echo custom
}
# ---- end common ----

MIN_ENGINE_VERSION=0.3.5        # mira-ui refuses older engines (min_agent_for_ui)
DEFAULT_PRETEND_VERSION=0.3.5.dev0
GIT_URL=https://github.com/MIRA-Intelligence/mira.git
DEFAULT_GIT_REF=feat/server-edition
STEPS=(packages python engine accounts launcher pam config policy apparmor slice sssd caddy firewall service clientca)
declare -A STEP_TITLE=(
	[packages]="System packages"
	[python]="Python 3.11+"
	[engine]="Mira engine and Mira Server in $VENV"
	[accounts]="Groups, the $SERVER_USER account and users"
	[launcher]="Engine launcher and sudoers rules"
	[pam]="PAM services"
	[config]="Mira Server configuration"
	[policy]="Host policy (agent sandbox)"
	[apparmor]="AppArmor rule for bubblewrap"
	[slice]="Per-user resource limits"
	[sssd]="SSSD access rules"
	[caddy]="Caddy HTTPS reverse proxy"
	[firewall]="Firewall (ufw)"
	[service]="Mira Server service"
	[clientca]="Client certificate (LAN)"
)

usage() {
	cat <<EOF
Mira Server installer (Ubuntu 22.04 / 24.04).

Usage: sudo bash install.sh [options]

Without options it asks everything in dialogs. With --non-interactive the
options below (or the answers saved by an earlier run) are used.

  --non-interactive, -y      No dialogs; use options, saved answers and defaults
  --mode lan|public|proxy    lan: HTTPS on this host's IP (default); public: a
                             domain with a Let's Encrypt certificate; proxy:
                             your own reverse proxy or SSH tunnels only
  --ip ADDRESS               LAN address clients use (lan mode)
  --domain NAME              Public domain (public mode)
  --email ADDRESS            ACME account e-mail (public mode, optional)
  --public-url URL           URL clients use (proxy mode)
  --source pypi|git|DIR      Install mira-engine from PyPI (default), from git
                             (--ref) or from a source directory
  --version VERSION          PyPI version (default: latest)
  --ref REF                  Git branch or tag (default: $DEFAULT_GIT_REF)
  --pretend-version VERSION  Version for source installs (default: $DEFAULT_PRETEND_VERSION)
  --users a,b                Accounts allowed to sign in (added to $USERS_GROUP)
  --admins a                 Accounts that also get the admin role
  --local-accounts yes|no    Local /etc/shadow accounts sign in (adds $SERVER_USER to shadow)
  --sssd-gpo yes|no          Allow the PAM services in SSSD AD GPO access control
  --limits MEM,CPU,TASKS     Per-user limits, e.g. 16G,400%,4096; "none" to skip
  --network full|none        Network access for agents' shell commands
  --ufw yes|no               Open the HTTPS port(s) when ufw is active
  --upgrade                  Upgrade an existing install
  --help                     This text

Log: $LOG_FILE    State: $STATE_FILE
EOF
}

declare -A OPT=()
UPGRADE=0

parse_args() {
	while (($#)); do
		case $1 in
		-y | --non-interactive) INTERACTIVE=0 ;;
		--upgrade) UPGRADE=1 ;;
		-h | --help) usage; exit 0 ;;
		--mode | --ip | --domain | --email | --public-url | --source | --version | --ref | --pretend-version | \
			--users | --admins | --local-accounts | --sssd-gpo | --limits | --network | --ufw)
			(($# >= 2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
			OPT[${1#--}]=$2
			shift
			;;
		*) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
		esac
		shift
	done
	if ((INTERACTIVE)) && [[ ! -t 0 || ! -t 1 ]]; then
		printf 'No terminal: download the script and run "sudo bash install.sh", or pass --non-interactive.\n' >&2
		exit 2
	fi
}

# ---- helpers
system_python_ok() {
	command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null
}

uv_python() {
	local found=("$UV_PYTHON_DIR"/cpython-3.11*/bin/python3.11)
	[[ -x ${found[0]} ]] && printf '%s' "${found[0]}"
	return 0
}

venv_python_ok() {
	[[ -x $VENV/bin/python ]] && "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null
}

engine_version() {
	"$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("mira-engine"))' 2>/dev/null
}

deploy_dir() { "$VENV/bin/mira-server" deploy-dir; }

private_ips() {
	ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}'
}

default_ip() {
	local ip all
	all=$(private_ips)
	for ip in $all; do
		if [[ $ip =~ ^10\. || $ip =~ ^192\.168\. || $ip =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]]; then
			printf '%s' "$ip"
			return 0
		fi
	done
	for ip in $all; do printf '%s' "$ip"; return 0; done
}

ufw_active() { command -v ufw >/dev/null 2>&1 && [[ $(ufw status 2>/dev/null || true) == "Status: active"* ]]; }

sssd_access_provider() {
	[[ -r /etc/sssd/sssd.conf ]] || return 0
	sed -n 's/^[[:space:]]*access_provider[[:space:]]*=[[:space:]]*\([a-z_]*\).*/\1/p;T;q' /etc/sssd/sssd.conf
}

uses_sss() { grep -qE '^[[:space:]]*(passwd|group):.*[[:space:]]sss' /etc/nsswitch.conf 2>/dev/null; }

split_list() { sed -e 's/[[:space:],]\{1,\}/\n/g' -e '/^$/d' <<<"$1"; }

join_list() { paste -sd, -; }

# Places a generated file; an existing different file is backed up first.
place_file() {
	local src=$1 dest=$2 mode=$3
	if [[ -f $dest ]] && ! cmp -s "$src" "$dest"; then
		backup_file "$dest" >/dev/null
	fi
	install -D -o root -g root -m "$mode" "$src" "$dest"
	log "wrote $dest"
}

same_content() { [[ -f $2 ]] && cmp -s "$1" "$2"; }

tmpfile() { mktemp "${TMPDIR:-/tmp}/mira-install.XXXXXX"; }

# ---- questions
ask_source() {
	local source=${OPT[source]:-${S_source:-pypi}} choice
	if [[ -z ${OPT[source]:-} ]]; then
		choice=$(ui_menu "Where should mira-engine (with Mira Server) be installed from?" \
			"$([[ $source == pypi || $source == git ]] && echo "$source" || echo dir)" \
			pypi "PyPI release (recommended)" \
			git "Git branch or tag from GitHub" \
			dir "A source directory on this host")
		case $choice in
		pypi | git) source=$choice ;;
		dir) source=$(ui_input "Source directory (a mira checkout with pyproject.toml):" \
			"$([[ $source == /* ]] && echo "$source")") ;;
		esac
	fi
	local answer
	case $source in
	pypi)
		answer=$(ui_input "mira-engine version on PyPI (empty: latest):" "${OPT[version]:-${S_version:-}}")
		state_set version "$answer"
		;;
	git)
		answer=$(ui_input "Git branch or tag of $GIT_URL:" "${OPT[ref]:-${S_ref:-$DEFAULT_GIT_REF}}")
		[[ -n $answer ]] || fail_input "a git branch or tag is needed"
		state_set ref "$answer"
		;;
	/*)
		[[ -f $source/pyproject.toml ]] || fail_input "$source has no pyproject.toml"
		;;
	*) fail_input "--source must be pypi, git or an absolute directory, not '$source'" ;;
	esac
	state_set source "$source"
	if [[ $source != pypi ]]; then
		state_set pretend_version "${OPT[pretend-version]:-${S_pretend_version:-$DEFAULT_PRETEND_VERSION}}"
	fi
}

ask_mode() {
	local mode=${OPT[mode]:-${S_mode:-lan}} ip domain email url items=() address
	if [[ -z ${OPT[mode]:-} ]]; then
		mode=$(ui_menu "How will clients reach Mira Server?" "$mode" \
			lan "Local network: HTTPS on this host's IP address, no domain needed" \
			public "Internet: a domain name with a Let's Encrypt certificate" \
			proxy "My own reverse proxy, or SSH tunnels only (no Caddy)")
	fi
	case $mode in
	lan)
		ip=${OPT[ip]:-${S_ip:-$(default_ip)}}
		if [[ -z ${OPT[ip]:-} ]] && ((INTERACTIVE)); then
			for address in $(private_ips); do items+=("$address" "$(ip -4 -o addr show to "$address" | awk '{print $2}')"); done
			items+=(other "Enter another address")
			ip=$(ui_menu "Address clients use to reach this host (the app will use https://<address>):" \
				"$ip" "${items[@]}")
			[[ $ip == other ]] && ip=$(ui_input "IP address:" "")
		fi
		[[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail_input "'$ip' is not an IPv4 address"
		state_set ip "$ip"
		state_set public_url "https://$ip"
		;;
	public)
		domain=$(ui_input "Domain name of this server (its DNS A/AAAA record must point here, ports 80 and 443 reachable):" \
			"${OPT[domain]:-${S_domain:-}}")
		[[ $domain =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$ ]] || fail_input "'$domain' is not a domain name"
		email=$(ui_input "E-mail for Let's Encrypt notices (optional):" "${OPT[email]:-${S_email:-}}")
		state_set domain "$domain"
		state_set email "$email"
		state_set public_url "https://$domain"
		;;
	proxy)
		url=$(ui_input "URL clients use (your proxy's address, or $SERVER_URL_LOCAL for SSH tunnels):" \
			"${OPT[public-url]:-${S_public_url:-$SERVER_URL_LOCAL}}")
		[[ $url =~ ^https?://[^/?#]+$ ]] || fail_input "'$url' must look like https://host or http://127.0.0.1:8740"
		state_set public_url "$url"
		;;
	*) fail_input "--mode must be lan, public or proxy" ;;
	esac
	state_set mode "$mode"
}

ask_accounts() {
	local local_default=yes provider
	uses_sss && local_default=no
	local answer=${OPT[local-accounts]:-${S_local_accounts:-}}
	if [[ -z $answer ]]; then
		if ui_yesno "Will accounts with local passwords (/etc/shadow) sign in?

Yes adds the $SERVER_USER account to the 'shadow' group so PAM can check their
passwords. Accounts from LDAP/AD through SSSD do not need it.$(uses_sss && printf '\n\nSSSD is configured on this host.')" "$local_default"; then
			answer=yes
		else
			answer=no
		fi
	fi
	state_set local_accounts "$answer"

	provider=$(sssd_access_provider)
	answer=${OPT[sssd-gpo]:-${S_sssd_gpo:-}}
	if [[ $provider == ad && -z $answer ]]; then
		if ui_yesno "SSSD uses Active Directory GPO access control (access_provider = ad).

It refuses PAM services it does not know, so sign-in and engine start may fail.
Add this line to the [domain/...] sections of /etc/sssd/sssd.conf (backed up first)?

  ad_gpo_map_remote_interactive = +mira-server, +mira-engine" no; then
			answer=yes
		else
			answer=no
		fi
	fi
	state_set sssd_gpo "${answer:-no}"
}

valid_users() {
	local user missing=()
	for user in $(split_list "$1"); do
		getent passwd "$user" >/dev/null || missing+=("$user")
	done
	if ((${#missing[@]})); then
		printf '%s' "${missing[*]}"
		return 1
	fi
}

ask_users() {
	local users admins missing
	while :; do
		users=$(ui_input "Accounts that may use Mira Server (comma separated; local or LDAP/AD).
They are added to the '$USERS_GROUP' group. Add more later with: sudo gpasswd -a <user> $USERS_GROUP" \
			"${OPT[users]:-${S_users:-$( ((INTERACTIVE)) && printf '%s' "${SUDO_USER:-}")}}")
		if missing=$(valid_users "$users"); then break; fi
		((INTERACTIVE)) || fail_input "unknown account(s): $missing"
		ui_msg "Unknown account(s): $missing

Check the names with: getent passwd <name>"
	done
	while :; do
		admins=$(ui_input "Administrators (comma separated, may be empty). They also join '$ADMINS_GROUP':" \
			"${OPT[admins]:-${S_admins:-}}")
		if missing=$(valid_users "$admins"); then break; fi
		((INTERACTIVE)) || fail_input "unknown account(s): $missing"
		ui_msg "Unknown account(s): $missing"
	done
	state_set users "$(split_list "$users" | join_list)"
	state_set admins "$(split_list "$admins" | join_list)"
}

ask_limits() {
	local limits=${OPT[limits]:-} memory cpu tasks
	if [[ -z $limits ]]; then
		if [[ ${S_limits:-} == none ]] && ! ((INTERACTIVE)); then
			limits=none
		elif ui_yesno "Limit each user's memory, CPU and processes?

Limits apply per person to their engine and their SSH sessions together
(systemd user-<uid>.slice)." "$([[ ${S_limits:-} == none ]] && echo no || echo yes)"; then
			IFS=, read -r memory cpu tasks <<<"${S_limits:-16G,400%,4096}"
			memory=$(ui_input "Memory per user (e.g. 16G):" "$memory")
			cpu=$(ui_input "CPU per user (100% = one core):" "$cpu")
			tasks=$(ui_input "Processes and threads per user:" "$tasks")
			limits="$memory,$cpu,$tasks"
		else
			limits=none
		fi
	fi
	if [[ $limits != none ]]; then
		IFS=, read -r memory cpu tasks <<<"$limits"
		[[ $memory =~ ^[0-9]+[KMGT]?$ && $cpu =~ ^[0-9]+%$ && $tasks =~ ^[0-9]+$ ]] ||
			fail_input "limits must look like 16G,400%,4096 (got '$limits')"
	fi
	state_set limits "$limits"
}

ask_network() {
	local network=${OPT[network]:-}
	if [[ -z $network ]]; then
		if ui_yesno "May agents' shell commands use the network (pip install, git clone, web APIs)?

They always run in the bubblewrap sandbox. 'No' gives them no network at all." \
			"$([[ ${S_network:-full} == none ]] && echo no || echo yes)"; then
			network=full
		else
			network=none
		fi
	fi
	[[ $network == full || $network == none ]] || fail_input "--network must be full or none"
	state_set network "$network"
}

ask_firewall() {
	local answer=${OPT[ufw]:-${S_ufw:-}} ports="443"
	[[ $S_mode == public ]] && ports="80 and 443"
	if [[ $S_mode != proxy ]] && ufw_active && [[ -z ${OPT[ufw]:-} ]]; then
		if ui_yesno "The ufw firewall is active. Allow TCP port(s) $ports?" "${answer:-yes}"; then
			answer=yes
		else
			answer=no
		fi
	fi
	state_set ufw "${answer:-no}"
}

fail_input() {
	ui_msg "Error: $1"
	exit 2
}

ask_all() {
	ask_source
	ask_mode
	ask_accounts
	ask_users
	ask_limits
	ask_network
	ask_firewall
}

summary() {
	cat <<EOF
Install from:   $(case $S_source in pypi) echo "PyPI mira-engine${S_version:+==$S_version}" ;; git) echo "git $GIT_URL @ ${S_ref}" ;; *) echo "$S_source" ;; esac)
Clients use:    $S_public_url ($(case $S_mode in lan) echo "LAN, Caddy with its own CA" ;; public) echo "public, Caddy with Let's Encrypt" ;; *) echo "your own proxy or SSH tunnels" ;; esac))
Users:          ${S_users:-(none yet)}
Admins:         ${S_admins:-(none)}
Local accounts: $S_local_accounts$([[ $S_sssd_gpo == yes ]] && printf '\nSSSD:           add mira-server, mira-engine to ad_gpo_map_remote_interactive')
Limits/user:    $(if [[ $S_limits == none ]]; then echo none; else sed 's/,/ memory, /; s/,/ CPU, /; s/$/ tasks/' <<<"$S_limits"; fi)
Agent network:  $S_network
Firewall:       $([[ $S_ufw == yes ]] && echo "open HTTPS in ufw" || echo "unchanged")
EOF
}

confirm_answers() {
	while :; do
		if ui_yesno "Install Mira Server with these settings?

$(summary)" yes; then
			return 0
		fi
		local next
		next=$(ui_menu "What now?" change change "Change the answers" quit "Quit without changes")
		case $next in
		change) ask_all ;;
		*) cancelled ;;
		esac
	done
}

# ---- steps: check_<step> tests the host, apply_<step> makes it so (idempotent)
check_packages() {
	local pkg
	for pkg in $(required_packages); do pkg_installed "$pkg" || return 1; done
}

required_packages() {
	local pkgs=(sudo bubblewrap curl ca-certificates iproute2)
	[[ $S_source == git ]] && pkgs+=(git)
	system_python_ok && pkgs+=(python3-venv)
	[[ $S_mode != proxy ]] && pkgs+=(caddy)
	printf '%s\n' "${pkgs[@]}"
}

add_caddy_repo() {
	say "  adding the official Caddy apt repository (Ubuntu has no caddy package here)"
	run apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https gnupg
	run bash -c "curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o $CADDY_REPO_KEY"
	run curl -fsSL -o "$CADDY_REPO_LIST" https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt
	state_set added_caddy_repo yes
	run apt-get update -q
}

apply_packages() {
	local missing=() pkg
	run apt-get update -q
	for pkg in $(required_packages); do pkg_installed "$pkg" || missing+=("$pkg"); done
	if [[ " ${missing[*]} " == *" caddy "* ]]; then
		state_set installed_caddy yes
		if ! grep -q 'Candidate: [0-9]' <<<"$(apt-cache policy caddy 2>/dev/null || true)"; then
			add_caddy_repo
		fi
	fi
	say "  installing: ${missing[*]}"
	run apt-get install -y -q "${missing[@]}"
}

check_python() { system_python_ok || [[ -n $(uv_python) ]]; }

apply_python() {
	local arch
	case $(uname -m) in
	x86_64) arch=x86_64 ;;
	aarch64 | arm64) arch=aarch64 ;;
	*) printf 'Unsupported architecture %s\n' "$(uname -m)" >>"$LOG_FILE"; return 1 ;;
	esac
	say "  installing uv and a root-owned Python 3.11 (this Ubuntu has Python < 3.11)"
	mkdir -p "$UV_DIR"
	run bash -c "curl -fsSL https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-unknown-linux-gnu.tar.gz | tar -xz --strip-components 1 -C $UV_DIR"
	state_set created_uv yes
	run env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" "$UV_DIR/uv" python install 3.11
	run chmod -R go+rX,go-w "$UV_PYTHON_DIR"
}

check_engine() {
	local version
	((UPGRADE)) && return 1
	[[ -x $VENV/bin/mira-server ]] && venv_python_ok || return 1
	version=$(engine_version) || return 1
	if [[ $S_source == pypi && -n ${S_version:-} && $version != "$S_version" ]]; then return 1; fi
	version_ge "$version" "$MIN_ENGINE_VERSION" || return 1
	deploy_dir >/dev/null 2>&1
}

sync_git_source() {
	if [[ -d $SRC_DIR/.git ]]; then
		say "  updating $SRC_DIR to $S_ref"
		run git -C "$SRC_DIR" fetch --depth 1 origin "$S_ref"
		run git -C "$SRC_DIR" checkout -q -f FETCH_HEAD
	else
		[[ -e $SRC_DIR ]] && { printf '%s exists and is not a git checkout\n' "$SRC_DIR" >>"$LOG_FILE"; return 1; }
		say "  cloning $GIT_URL ($S_ref) to $SRC_DIR"
		run git clone -q --depth 1 -b "$S_ref" "$GIT_URL" "$SRC_DIR"
		state_set created_src yes
	fi
	run git -C "$SRC_DIR" submodule update -q --init --depth 1 mira_engine/skills
}

apply_engine() {
	local spec version base
	if [[ -d $VENV ]] && ! venv_python_ok; then
		say "  $VENV has no usable Python 3.11+; recreating it"
		run rm -rf "$VENV"
	fi
	if [[ ! -x $VENV/bin/python ]]; then
		if system_python_ok; then
			run python3 -m venv "$VENV"
		else
			base=$(uv_python)
			run env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" "$UV_DIR/uv" venv -q --seed --python "$base" "$VENV"
		fi
		state_set created_venv yes
		run "$VENV/bin/python" -m pip install -q -U pip
	fi
	case $S_source in
	pypi) spec="mira-engine[server]${S_version:+==$S_version}" ;;
	git) sync_git_source; spec="${SRC_DIR}[server]" ;;
	*) spec="${S_source}[server]" ;;
	esac
	say "  pip install $spec (a few minutes)"
	if [[ $S_source == pypi ]]; then
		run "$VENV/bin/python" -m pip install -q -U "$spec"
	else
		run env SETUPTOOLS_SCM_PRETEND_VERSION="$S_pretend_version" "$VENV/bin/python" -m pip install -q -U "$spec"
	fi
	run chmod -R go-w "$VENV"
	version=$(engine_version)
	say "  installed mira-engine $version"
	if ! version_ge "$version" "$MIN_ENGINE_VERSION"; then
		printf 'mira-engine %s is older than %s, which the desktop app requires\n' "$version" "$MIN_ENGINE_VERSION" >>"$LOG_FILE"
		return 1
	fi
	UPGRADE=0
	state_set need_restart 1
}

check_accounts() {
	local user
	getent group "$USERS_GROUP" >/dev/null && getent group "$ADMINS_GROUP" >/dev/null || return 1
	id "$SERVER_USER" >/dev/null 2>&1 || return 1
	[[ $(stat -c '%U %a' "$DATA_DIR" 2>/dev/null) == "$SERVER_USER 700" ]] || return 1
	if [[ $S_local_accounts == yes ]] && getent group shadow >/dev/null; then
		in_group "$SERVER_USER" shadow || return 1
	fi
	for user in $(split_list "$S_users"); do in_group "$user" "$USERS_GROUP" || return 1; done
	for user in $(split_list "$S_admins"); do
		in_group "$user" "$USERS_GROUP" && in_group "$user" "$ADMINS_GROUP" || return 1
	done
}

apply_accounts() {
	local user
	if ! getent group "$USERS_GROUP" >/dev/null; then run groupadd "$USERS_GROUP"; state_set created_users_group yes; fi
	if ! getent group "$ADMINS_GROUP" >/dev/null; then run groupadd "$ADMINS_GROUP"; state_set created_admins_group yes; fi
	if ! id "$SERVER_USER" >/dev/null 2>&1; then
		run useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVER_USER"
		state_set created_account yes
	fi
	mkdir -p "$DATA_DIR"
	chown "$SERVER_USER:" "$DATA_DIR"
	chmod 700 "$DATA_DIR"
	if [[ $S_local_accounts == yes ]] && getent group shadow >/dev/null && ! in_group "$SERVER_USER" shadow; then
		run usermod -aG shadow "$SERVER_USER"
		state_set added_shadow yes
		state_set need_restart 1
	fi
	for user in $(split_list "$S_users") $(split_list "$S_admins"); do
		in_group "$user" "$USERS_GROUP" || run gpasswd -a "$user" "$USERS_GROUP"
	done
	for user in $(split_list "$S_admins"); do
		in_group "$user" "$ADMINS_GROUP" || run gpasswd -a "$user" "$ADMINS_GROUP"
	done
}

expected_sudoers_rules() {
	"$VENV/bin/python" -c '
import sys
from pathlib import Path
from mira_server.spawner.launcher import sudoers_rules
print("\n".join(sudoers_rules(Path(sys.argv[1]), server_user=sys.argv[2], users_group=sys.argv[3])))
' "$LAUNCHER" "$SERVER_USER" "$USERS_GROUP"
}

check_launcher() {
	[[ -f $LAUNCHER && -f $SUDOERS_FILE ]] || return 1
	"$VENV/bin/python" -c '
import sys
from mira_server.spawner.launcher import render_launcher
sys.stdout.write(render_launcher(sys.argv[1]))
' "$VENV/bin/python" | cmp -s - "$LAUNCHER" || return 1
	[[ $(grep -v '^[[:space:]]*#' "$SUDOERS_FILE" | sed '/^[[:space:]]*$/d') == "$(expected_sudoers_rules)" ]]
}

apply_launcher() {
	[[ -f $SUDOERS_FILE ]] && backup_file "$SUDOERS_FILE" >/dev/null
	run "$VENV/bin/mira-server" install-launcher --prefix /usr/local --engine-python "$VENV/bin/python" \
		--server-user "$SERVER_USER" --users-group "$USERS_GROUP" --sudoers-file "$SUDOERS_FILE"
}

check_pam() {
	local dir
	dir=$(deploy_dir)
	same_content "$dir/pam.d-mira-server" "$PAM_SERVER" && same_content "$dir/pam.d-mira-engine" "$PAM_ENGINE"
}

apply_pam() {
	local dir
	dir=$(deploy_dir)
	place_file "$dir/pam.d-mira-server" "$PAM_SERVER" 0644
	place_file "$dir/pam.d-mira-engine" "$PAM_ENGINE" 0644
}

render_config() {
	printf '%s\n' "$MANAGED_MARK" \
		"# Every setting and its default: $(deploy_dir)/mira-server.toml" \
		"# Re-run the installer (sudo mira-server-install) to change these." \
		"[server]" \
		"public_url = \"$S_public_url\""
	if [[ $S_public_url == https://* ]]; then
		printf '%s\n' "# The TLS proxy on this host; its X-Forwarded-For gives client IPs for rate limiting." \
			'trusted_proxies = ["127.0.0.1", "::1"]'
	fi
}

check_config() {
	[[ -f $CONFIG_FILE ]] || return 1
	[[ ${S_keep_config:-} == yes ]] && return 0
	same_content <(render_config) "$CONFIG_FILE"
}

apply_config() {
	local tmp
	tmp=$(tmpfile)
	render_config >"$tmp"
	if [[ -f $CONFIG_FILE ]] && ! grep -qF "$MANAGED_MARK" "$CONFIG_FILE"; then
		if ! ui_yesno "$CONFIG_FILE already exists (not written by this installer).

Replace it with the installer's configuration (public_url = $S_public_url)?
The current file is backed up. 'No' keeps it as it is." yes; then
			state_set keep_config yes
			rm -f "$tmp"
			return 0
		fi
	fi
	state_set keep_config no
	place_file "$tmp" "$CONFIG_FILE" 0644
	rm -f "$tmp"
	state_set need_restart 1
}

render_policy() {
	local dir
	dir=$(deploy_dir)
	if [[ $S_network == none ]]; then
		sed 's/^network = "full"/network = "none"/' "$dir/policy.toml"
	else
		cat "$dir/policy.toml"
	fi
}

check_policy() {
	[[ -f $POLICY_FILE ]] || return 1
	[[ ${S_keep_policy:-} == yes ]] && return 0
	same_content <(render_policy) "$POLICY_FILE"
}

apply_policy() {
	local tmp
	if [[ -f $POLICY_FILE ]] && [[ -z ${S_keep_policy:-} ]] &&
		! ui_yesno "$POLICY_FILE already exists with different settings.

Replace it with the installer's policy (agents confined to their project, shell
commands in the bubblewrap sandbox, network: $S_network)? It is backed up first.
'No' keeps your policy." no; then
		state_set keep_policy yes
		return 0
	fi
	tmp=$(tmpfile)
	render_policy >"$tmp"
	place_file "$tmp" "$POLICY_FILE" 0644
	rm -f "$tmp"
	state_set keep_policy no
}

apparmor_needed() { [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0) == 1 ]]; }

bwrap_works() {
	(cd / && runuser -u "$SERVER_USER" -- bwrap --ro-bind / / --unshare-pid --proc /proc --chdir / true) >/dev/null 2>&1
}

check_apparmor() {
	apparmor_needed || return 0
	bwrap_works
}

apply_apparmor() {
	place_file "$(deploy_dir)/apparmor-bwrap" "$APPARMOR_FILE" 0644
	run apparmor_parser -r "$APPARMOR_FILE"
	state_set installed_apparmor yes
}

render_slice() {
	local memory cpu tasks
	IFS=, read -r memory cpu tasks <<<"$S_limits"
	printf '%s\n' "$MANAGED_MARK" \
		"# Per-user limits for Mira engines and SSH sessions (systemd user-<uid>.slice)." \
		"[Slice]" "MemoryMax=$memory" "MemorySwapMax=0" "CPUQuota=$cpu" "TasksMax=$tasks"
}

check_slice() {
	[[ $S_limits == none ]] && return 0
	same_content <(render_slice) "$SLICE_FILE"
}

apply_slice() {
	local tmp
	tmp=$(tmpfile)
	render_slice >"$tmp"
	place_file "$tmp" "$SLICE_FILE" 0644
	rm -f "$tmp"
	run systemctl daemon-reload
}

check_sssd() {
	[[ $S_sssd_gpo == yes ]] || return 0
	local lines
	lines=$(grep -E '^[[:space:]]*ad_gpo_map_remote_interactive' /etc/sssd/sssd.conf 2>/dev/null || true)
	[[ $lines == *+mira-server* && $lines == *+mira-engine* ]]
}

apply_sssd() {
	state_set sssd_backup "$(backup_file /etc/sssd/sssd.conf)"
	run "$VENV/bin/python" - /etc/sssd/sssd.conf <<'EOF'
import re
import sys

path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
out, in_domain, seen = [], False, False
key = "ad_gpo_map_remote_interactive"
wanted = ["+mira-server", "+mira-engine"]

def close_section():
    if in_domain and not seen:
        out.append(f"{key} = {', '.join(wanted)}")

for line in lines:
    if re.match(r"\s*\[", line):
        close_section()
        in_domain, seen = bool(re.match(r"\s*\[domain/", line)), False
    elif in_domain and re.match(rf"\s*{key}\s*=", line):
        seen = True
        name, value = line.split("=", 1)
        items = [v.strip() for v in value.split(",") if v.strip()]
        items += [w for w in wanted if w not in items]
        line = f"{name.rstrip()} = {', '.join(items)}"
    out.append(line)
close_section()
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
EOF
	state_set sssd_changed yes
	run systemctl restart sssd
}

render_caddy_site() {
	local address
	if [[ $S_mode == lan ]]; then address="https://$S_ip"; else address=$S_domain; fi
	printf '%s {\n' "$address"
	[[ $S_mode == lan ]] && printf '\ttls internal\n'
	printf '%s\n' $'\treverse_proxy 127.0.0.1:8740 {' \
		$'\t\t# Streaming responses (chat, SSE) pass through unbuffered.' \
		$'\t\tflush_interval -1' $'\t}' \
		$'\t# Image, download and WebSocket URLs carry ?access_token=; keep it out of the log.' \
		$'\tlog {' $'\t\toutput file /var/log/caddy/mira-server.access.log' \
		$'\t\tformat filter {' $'\t\t\twrap json' $'\t\t\tfields {' \
		$'\t\t\t\trequest>uri query {' $'\t\t\t\t\tdelete access_token' $'\t\t\t\t}' \
		$'\t\t\t}' $'\t\t}' $'\t}' '}'
}

render_caddyfile() {
	printf '%s\n' "$MANAGED_MARK" "# Mira Server behind Caddy. Re-run the installer (sudo mira-server-install) to change it."
	if [[ $S_mode == lan ]]; then
		# Clients connecting by IP send no SNI; serve the IP's certificate to them.
		printf '{\n\tdefault_sni %s\n}\n\n' "$S_ip"
	elif [[ -n ${S_email:-} ]]; then
		printf '{\n\temail %s\n}\n\n' "$S_email"
	fi
	render_caddy_site
}

check_caddy() {
	[[ $S_mode == proxy ]] && return 0
	systemctl is-active --quiet caddy || return 1
	if [[ ${S_caddy_layout:-} == snippet ]]; then
		same_content <(render_caddy_site) "$CADDY_SNIPPET" || return 1
		grep -qE "^[[:space:]]*import[[:space:]]+$CADDY_SNIPPET" "$CADDYFILE" || return 1
		[[ $S_mode != lan ]] || grep -qE "^[[:space:]]*default_sni[[:space:]]+$S_ip" "$CADDYFILE"
		return
	fi
	same_content <(render_caddyfile) "$CADDYFILE"
}

apply_caddy() {
	local kind tmp
	kind=$(caddyfile_kind)
	if [[ $kind == mira ]] && ! ui_yesno "$CADDYFILE only serves Mira Server but was not written by this installer.

Replace it with the installer's version? It is backed up first." yes; then
		kind=custom
	fi
	tmp=$(tmpfile)
	if [[ $kind == custom ]]; then
		state_set caddy_layout snippet
		render_caddy_site >"$tmp"
		place_file "$tmp" "$CADDY_SNIPPET" 0644
		rm -f "$tmp"
		if grep -qE "^[[:space:]]*import[[:space:]]+$CADDY_SNIPPET" "$CADDYFILE" &&
			{ [[ $S_mode != lan ]] || grep -qE "^[[:space:]]*default_sni[[:space:]]+$S_ip" "$CADDYFILE"; }; then
			run caddy validate --config "$CADDYFILE" --adapter caddyfile
			run systemctl reload-or-restart caddy
		else
			need_manual "$CADDYFILE has other sites, so the installer did not change it.
The Mira Server site was written to $CADDY_SNIPPET. Please:

  1. Add this line to $CADDYFILE (outside any site block):
       import $CADDY_SNIPPET
$([[ $S_mode == lan ]] && printf '  2. Add to the global options block at the very top of %s:\n       {\n           default_sni %s\n       }\n' "$CADDYFILE" "$S_ip")
  Then: sudo caddy validate --config $CADDYFILE && sudo systemctl reload caddy
  and run the installer again."
		fi
		return 0
	fi
	if [[ $kind == default || $kind == mira ]]; then
		state_set caddyfile_backup "$(backup_file "$CADDYFILE")"
	fi
	state_set caddy_layout main
	render_caddyfile >"$tmp"
	run caddy validate --config "$tmp" --adapter caddyfile
	install -D -o root -g root -m 0644 "$tmp" "$CADDYFILE"
	rm -f "$tmp"
	mkdir -p /var/log/caddy
	chown caddy: /var/log/caddy 2>/dev/null || true
	run systemctl enable caddy
	run systemctl reload-or-restart caddy
}

ufw_ports() { if [[ $S_mode == public ]]; then echo "80/tcp 443/tcp"; else echo "443/tcp"; fi; }

check_firewall() {
	local port status
	[[ $S_ufw == yes && $S_mode != proxy ]] || return 0
	ufw_active || return 0
	status=$(ufw status 2>/dev/null)
	for port in $(ufw_ports); do grep -qE "^${port}[[:space:]]+ALLOW" <<<"$status" || return 1; done
}

apply_firewall() {
	local port
	for port in $(ufw_ports); do run ufw allow "$port"; done
	state_set ufw_rules "$(ufw_ports)"
}

check_service() {
	same_content "$(deploy_dir)/mira-server.service" "$UNIT_FILE" || return 1
	systemctl is-enabled --quiet mira-server && systemctl is-active --quiet mira-server || return 1
	[[ ${S_need_restart:-0} == 1 ]] && return 1
	curl -fsS -o /dev/null --max-time 5 "$SERVER_URL_LOCAL/server/info"
}

apply_service() {
	local i
	place_file "$(deploy_dir)/mira-server.service" "$UNIT_FILE" 0644
	run systemctl daemon-reload
	run systemctl enable mira-server
	run systemctl restart mira-server
	for i in $(seq 1 30); do
		curl -fsS -o /dev/null --max-time 2 "$SERVER_URL_LOCAL/server/info" && break
		sleep 1
	done
	state_set need_restart 0
}

check_clientca() {
	[[ $S_mode == lan ]] || return 0
	same_content "$CADDY_ROOT_CA" "$CLIENT_CA"
}

apply_clientca() {
	local i
	for i in $(seq 1 30); do
		[[ -f $CADDY_ROOT_CA ]] && break
		# The first TLS connection makes Caddy issue its internal certificates.
		curl -ks -o /dev/null --max-time 2 "https://$S_ip/server/info" || true
		sleep 1
	done
	[[ -f $CADDY_ROOT_CA ]] || { printf 'Caddy did not create %s\n' "$CADDY_ROOT_CA" >>"$LOG_FILE"; return 1; }
	install -D -o root -g root -m 0644 "$CADDY_ROOT_CA" "$CLIENT_CA"
}

# ---- step runner, failures
step_hint() {
	case $1 in
	packages) echo "apt failed. If another apt/dpkg process is running (unattended-upgrades), wait for it. Check network access and any apt proxy (/etc/apt/apt.conf.d)." ;;
	python) echo "Downloading uv or Python 3.11 failed. Check access to github.com and any HTTPS proxy (https_proxy)." ;;
	engine) echo "Installing mira-engine failed. Check access to PyPI (or the --source path/ref) and pip settings in /etc/pip.conf. Until mira-engine with Mira Server is on PyPI, use --source git." ;;
	accounts) echo "Creating groups/accounts or adding users failed. Check the names with 'getent passwd <name>' (SSSD/LDAP must resolve them)." ;;
	launcher) echo "The engine launcher or the sudoers rules could not be written (visudo checks them)." ;;
	sssd) echo "SSSD did not restart: 'journalctl -u sssd'. The original is in $BACKUP_DIR." ;;
	caddy) echo "Caddy could not start with the new config: 'journalctl -u caddy -n 50'. Ports 80/443 must be free ('ss -ltnp')." ;;
	service) echo "Mira Server did not start: 'journalctl -u mira-server -n 50'." ;;
	clientca) echo "Caddy did not create its internal CA: 'journalctl -u caddy -n 50'." ;;
	*) echo "See the log for details." ;;
	esac
}

report_failure() {
	local code=$1 command=$2 title=setup text
	[[ -n ${CURRENT_STEP:-} ]] && title=${STEP_TITLE[$CURRENT_STEP]}
	text="Step failed: $title
Command: $command (exit $code)

$(step_hint "${CURRENT_STEP:-}")

Last lines of $LOG_FILE:
$(last_log_lines 12)

Fix the problem, then run the installer again:
  sudo bash $(realpath "$0" 2>/dev/null || echo "$0")
Finished steps are skipped and your answers are kept."
	printf '\n%s\n' "$text" >&2
	if ((INTERACTIVE)) && have_whiptail; then ui_msg "$text"; fi
}

need_manual() {
	state_set failed_step "$CURRENT_STEP"
	log "manual action needed in step $CURRENT_STEP"
	printf '\n%s\n' "$1" >&2
	if ((INTERACTIVE)) && have_whiptail; then ui_msg "$1"; fi
	exit 3
}

run_steps() {
	local step n=0 total=${#STEPS[@]}
	state_set status in_progress
	for step in "${STEPS[@]}"; do
		n=$((n + 1))
		CURRENT_STEP=$step
		if check_"$step"; then
			say "[$n/$total] ${STEP_TITLE[$step]}: ok, skipped"
			continue
		fi
		say "[$n/$total] ${STEP_TITLE[$step]}"
		if [[ ${MIRA_INSTALLER_FAIL_AT:-} == "$step" ]]; then
			log "failing on purpose (MIRA_INSTALLER_FAIL_AT=$step)"
			false
		fi
		apply_"$step"
		if ! check_"$step"; then
			log "check_$step still fails after apply_$step"
			false
		fi
	done
	CURRENT_STEP=""
	state_set failed_step ""
	state_set status complete
}

# ---- after the steps
install_commands() {
	local dir
	dir=$(deploy_dir)
	install -o root -g root -m 0755 "$dir/install.sh" "$SBIN_INSTALL"
	install -o root -g root -m 0755 "$dir/uninstall.sh" "$SBIN_UNINSTALL"
}

self_check() {
	local report url
	report=$(tmpfile)
	{
		echo "mira-server check:"
		(cd / && runuser -u "$SERVER_USER" -- "$VENV/bin/mira-server" check --config "$CONFIG_FILE" 2>&1) || true
		echo
		case $S_mode in
		lan) url="https://$S_ip/server/info"
			echo "HTTPS ($url, with $CLIENT_CA):"
			curl -sS --max-time 10 --cacert "$CLIENT_CA" "$url" 2>&1 || true ;;
		public) url="https://$S_domain/server/info"
			echo "HTTPS ($url):"
			curl -sS --max-time 20 "$url" 2>&1 || echo "(the certificate may still be issuing; DNS must point here and ports 80/443 be reachable)" ;;
		*) url="$SERVER_URL_LOCAL/server/info"
			echo "Local ($url):"
			curl -sS --max-time 10 "$url" 2>&1 || true ;;
		esac
		echo
	} >"$report"
	log "self check:"
	cat "$report" >>"$LOG_FILE"
	ui_text_file "$report"
	rm -f "$report"
}

login_test() {
	local user password status
	((INTERACTIVE)) || return 0
	ui_yesno "Test a sign-in now? (The password goes straight to Mira Server on this host and is not stored.)" yes || return 0
	user=$(ui_input "Account:" "$(split_list "$S_users" | head -n 1)")
	password=$(ui_password "Password for $user:") || return 0
	status=$(printf '%s' "$password" | "$VENV/bin/python" -c '
import json, sys, urllib.error, urllib.request
body = json.dumps({"username": sys.argv[1], "password": sys.stdin.read(), "client": "installer"}).encode()
req = urllib.request.Request(sys.argv[2] + "/server/auth/login", body, {"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(resp.status)
except urllib.error.HTTPError as err:
    print(err.code)
except OSError as err:
    print(f"error: {err}")
' "$user" "$SERVER_URL_LOCAL")
	password=""
	case $status in
	200) ui_msg "Sign-in as $user works." ;;
	401) ui_msg "Sign-in as $user failed: wrong user name or password (401).

If the password is right: 'journalctl -u mira-server | grep PAM' shows why PAM refused it." ;;
	403) ui_msg "$user signed in but is not allowed (403): add the account to $USERS_GROUP." ;;
	429) ui_msg "Too many failed sign-ins for $user; wait 5 minutes." ;;
	*) ui_msg "Sign-in test failed: $status" ;;
	esac
}

client_setup_text() {
	cat <<EOF
Mira Server is installed.

In the Mira desktop app: Settings -> Deployment mode -> Team server
  Server address: $S_public_url
  Sign in with your account on this server (members of $USERS_GROUP).
EOF
	if [[ $S_mode == lan ]]; then
		cat <<EOF

Each client computer must trust this server's certificate authority once:
  macOS:
    scp <you>@$S_ip:$CLIENT_CA ~/Downloads/mira-server-ca.crt
    security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db ~/Downloads/mira-server-ca.crt
  Windows (PowerShell):
    scp <you>@${S_ip}:$CLIENT_CA mira-server-ca.crt
    certutil -addstore -user Root mira-server-ca.crt
  Check:  curl https://$S_ip/server/info   (no certificate error)

If the computer uses a proxy app (Clash, V2Ray, a PAC file...), add $S_ip to
its bypass list; the app's requests follow the system proxy.
EOF
	fi
	cat <<EOF

Users:     sudo gpasswd -a <user> $USERS_GROUP   (remove: gpasswd -d)
           or: sudo mira-server-install -> Manage users
Upgrade, reconfigure:  sudo mira-server-install
Uninstall:             sudo mira-server-uninstall
Logs:      journalctl -u mira-server    Install log: $LOG_FILE
EOF
}

finish() {
	install_commands
	client_setup_text >"$CLIENT_SETUP"
	chmod 0644 "$CLIENT_SETUP"
	self_check
	login_test
	ui_msg "$(cat "$CLIENT_SETUP")

(Saved in $CLIENT_SETUP)"
	say "Done. Client setup: $CLIENT_SETUP"
}

manage_users() {
	local add remove user missing
	add=$(ui_input "Current members of $USERS_GROUP: $(getent group "$USERS_GROUP" | cut -d: -f4)

Accounts to add (comma separated, may be empty):" "")
	if [[ -n $add ]] && ! missing=$(valid_users "$add"); then fail_input "unknown account(s): $missing"; fi
	remove=$(ui_input "Accounts to remove (comma separated, may be empty).
Their sessions end and their engines stop within a few minutes:" "")
	for user in $(split_list "$add"); do in_group "$user" "$USERS_GROUP" || run gpasswd -a "$user" "$USERS_GROUP"; done
	for user in $(split_list "$remove"); do
		in_group "$user" "$USERS_GROUP" && run gpasswd -d "$user" "$USERS_GROUP"
		in_group "$user" "$ADMINS_GROUP" && run gpasswd -d "$user" "$ADMINS_GROUP"
	done
	state_set users "$({ split_list "$S_users"; split_list "$add"; } | grep -vxF -f <(split_list "$remove"; echo) | sort -u | join_list)"
	state_set admins "$(split_list "$S_admins" | grep -vxF -f <(split_list "$remove"; echo) | join_list)"
	ui_msg "Members of $USERS_GROUP: $(getent group "$USERS_GROUP" | cut -d: -f4)"
}

uninstall_now() {
	local script=$SBIN_UNINSTALL
	[[ -x $script ]] || script="$(deploy_dir)/uninstall.sh"
	exec bash "$script"
}

existing_install() {
	local choice=upgrade
	if ((INTERACTIVE)); then
		choice=$(ui_menu "Mira Server is installed ($S_public_url, mira-engine $(engine_version || echo '?')).
What do you want to do?" upgrade \
			upgrade "Upgrade mira-engine and refresh the setup" \
			reconfigure "Change the settings" \
			users "Add or remove users" \
			uninstall "Uninstall Mira Server" \
			quit "Quit")
	elif ((!UPGRADE)); then
		choice=reconfigure
	fi
	case $choice in
	upgrade) UPGRADE=1; run_steps ;;
	reconfigure) ask_all; confirm_answers; run_steps ;;
	users) manage_users; return 0 ;;
	uninstall) uninstall_now ;;
	*) exit 0 ;;
	esac
	finish
}

resume_install() {
	local choice=continue
	if ((INTERACTIVE)); then
		choice=$(ui_menu "An earlier installation did not finish$([[ -n ${S_failed_step:-} ]] && printf ' (it stopped at: %s)' "${STEP_TITLE[$S_failed_step]:-$S_failed_step}").
Finished steps are detected and skipped." continue \
			continue "Continue with the same answers" \
			change "Change the answers, then continue" \
			restart "Forget the answers and start over" \
			quit "Quit")
	fi
	case $choice in
	continue) apply_option_overrides ;;
	change) ask_all; confirm_answers ;;
	restart) rm -f "$STATE_FILE"; unset "${!S_@}"; new_install; return ;;
	*) exit 0 ;;
	esac
	run_steps
	finish
}

# Unattended re-runs: options given on the command line replace saved answers.
apply_option_overrides() {
	if ((${#OPT[@]})); then
		INTERACTIVE_SAVE=$INTERACTIVE
		INTERACTIVE=0
		ask_all
		INTERACTIVE=$INTERACTIVE_SAVE
	fi
}

new_install() {
	ui_msg "This installs Mira Server on this host:

 - mira-engine and Mira Server in $VENV
 - sign-in with host accounts (PAM), one engine per user as that user
 - sudo launcher, PAM services, sandbox policy, per-user limits
 - an HTTPS reverse proxy (Caddy) for local-network or Internet access

Every step can be re-run safely. Log: $LOG_FILE"
	ask_all
	((INTERACTIVE)) && confirm_answers
	run_steps
	finish
}

main() {
	parse_args "$@"
	require_root
	prepare_log "$@"
	check_ubuntu
	state_load
	trap on_error ERR
	case ${S_status:-} in
	complete) existing_install ;;
	in_progress) resume_install ;;
	*) new_install ;;
	esac
}

main "$@"
