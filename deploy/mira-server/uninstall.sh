#!/usr/bin/env bash
# shellcheck disable=SC2034  # the common block defines constants install.sh uses
# Mira Server uninstaller (Ubuntu). Installed as /usr/local/sbin/mira-server-uninstall.
#
#   sudo mira-server-uninstall            # choose what to remove (whiptail dialogs)
#   sudo mira-server-uninstall --yes      # unattended; add --purge to remove config and data too
#
# With the installer's record (/var/lib/mira-server-installer/state) it removes
# what the installer set up. Without one (Mira Server set up by hand from the
# guide) it detects the usual files and asks. If a step fails, fix it and run
# the uninstaller again: it continues with the same selection. Users' home
# directories and ~/.mira are never touched.

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

WORK_COPY=$STATE_DIR/uninstall.sh
SELECTION_FILE=$STATE_DIR/uninstall-selection
PURGE=0
U_VENV=""
U_SRC=""
declare -A PRESENT=() DEFAULT=() LABEL=()
ITEMS=(engines service caddy caddy_pkg firewall sudoers pam launcher policy apparmor slice sssd account venv uv src commands config data groups)

usage() {
	cat <<EOF
Mira Server uninstaller.

Usage: sudo mira-server-uninstall [--yes] [--purge]

Removes what the installer set up. Without an install record (a Mira Server set
up by hand, e.g. from the guide), it looks for the usual files and asks.

  --yes, -y   No dialogs: remove the default selection
  --purge     Also remove $CONFIG_DIR (config, client certificate),
              $DATA_DIR (sessions, audit log), the $USERS_GROUP and
              $ADMINS_GROUP groups and the install log
  --help      This text

Never touched: users' home directories and their ~/.mira data, and engines
users run from their own installs.
EOF
}

parse_args() {
	while (($#)); do
		case $1 in
		-y | --yes) INTERACTIVE=0 ;;
		--purge) PURGE=1 ;;
		-h | --help) usage; exit 0 ;;
		*) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
		esac
		shift
	done
	if ((INTERACTIVE)) && [[ ! -t 0 || ! -t 1 ]]; then
		printf 'No terminal: pass --yes to uninstall without dialogs.\n' >&2
		exit 2
	fi
}

# Runs from a copy in the state directory, so a failed uninstall can be
# resumed even after the installed copy (in the venv or /usr/local/sbin) is gone.
use_work_copy() {
	local self
	self=$(realpath "$0" 2>/dev/null || printf '%s' "$0")
	[[ $self == "$WORK_COPY" ]] && return 0
	mkdir -p "$STATE_DIR"
	chmod 700 "$STATE_DIR"
	install -m 0700 "$self" "$WORK_COPY"
	exec bash "$WORK_COPY" "$@"
}

# ---- detection
detect_venv() {
	local path=""
	if [[ -f $UNIT_FILE ]]; then
		path=$(sed -n 's#^ExecStart=\([^ ]*\)/bin/mira-server .*#\1#p' "$UNIT_FILE")
	fi
	if [[ -z $path && -f $LAUNCHER ]]; then
		path=$(sed -n "s#^ENGINE_PYTHON = '\\(.*\\)/bin/python[0-9.]*'\$#\\1#p" "$LAUNCHER")
	fi
	[[ -z $path && -x $VENV/bin/mira-server ]] && path=$VENV
	if [[ -n $path && -d $path && $path != / ]]; then printf '%s' "$path"; fi
	return 0
}

# The source directory mira-engine was installed from, if any (pip's direct_url.json).
detect_source() {
	local file url
	for file in "$1"/lib/python3*/site-packages/mira_engine-*.dist-info/direct_url.json; do
		[[ -f $file ]] || continue
		url=$(sed -n 's#.*"url": *"file://\([^"]*\)".*#\1#p' "$file")
		if [[ -n $url && -f $url/pyproject.toml ]] && grep -q '^name = "mira-engine"' "$url/pyproject.toml"; then
			printf '%s' "$url"
		fi
	done
	return 0
}

item() {
	local id=$1 default=$2
	shift 2
	PRESENT[$id]=1
	DEFAULT[$id]=$default
	LABEL[$id]="$*"
}

on_off() { if [[ $1 == yes ]]; then echo on; else echo off; fi; }

purge_default() { if ((PURGE)); then echo on; else echo off; fi; }

sssd_has_tokens() { grep -qE '^[[:space:]]*ad_gpo_map_remote_interactive.*\+mira-(server|engine)' /etc/sssd/sssd.conf 2>/dev/null; }

inventory() {
	local kind
	# A resumed uninstall may already have removed the files these come from.
	U_VENV=${S_u_venv:-$(detect_venv)}
	if [[ -n $U_VENV && -z ${S_u_src:-} ]]; then U_SRC=$(detect_source "$U_VENV"); else U_SRC=${S_u_src:-}; fi
	if [[ -z $U_SRC && ${S_created_src:-} == yes && -d $SRC_DIR ]]; then U_SRC=$SRC_DIR; fi
	state_set u_venv "$U_VENV"
	state_set u_src "$U_SRC"

	if [[ -n $U_VENV || -f $LAUNCHER ]]; then item engines on "Stop the engines Mira Server runs for users"; fi
	if [[ -f $UNIT_FILE ]] || systemctl cat mira-server >/dev/null 2>&1; then
		item service on "The mira-server service ($UNIT_FILE)"
	fi
	kind=$(caddyfile_kind)
	if [[ -f $CADDY_SNIPPET ]]; then
		item caddy on "Caddy: the Mira site ($CADDY_SNIPPET and its import line)"
	elif [[ $kind == managed || $kind == mira ]]; then
		if [[ -n ${S_caddyfile_backup:-} && -f ${S_caddyfile_backup:-} ]]; then
			item caddy on "Caddy: restore $CADDYFILE from before the install"
		else
			item caddy on "Caddy: remove the Mira site from $CADDYFILE (leaves it empty)"
		fi
	fi
	if pkg_installed caddy; then
		if [[ ${S_installed_caddy:-} == yes ]]; then
			item caddy_pkg on "The caddy package (installed by the installer)"
		else
			item caddy_pkg off "The caddy package (not installed by the installer)"
		fi
	fi
	if [[ -n ${S_ufw_rules:-} ]]; then item firewall on "Firewall rules added for Mira Server (ufw: ${S_ufw_rules})"; fi
	if [[ -f $SUDOERS_FILE ]]; then item sudoers on "sudoers rules ($SUDOERS_FILE)"; fi
	if [[ -f $PAM_SERVER || -f $PAM_ENGINE ]]; then item pam on "PAM services ($PAM_SERVER, $PAM_ENGINE)"; fi
	if [[ -f $LAUNCHER ]]; then item launcher on "Engine launcher ($LAUNCHER)"; fi
	if [[ -f $POLICY_FILE ]]; then
		item policy on "Host policy $POLICY_FILE (also used by engines users start over SSH)"
	fi
	if [[ -f $APPARMOR_FILE ]]; then item apparmor on "AppArmor rule for bubblewrap ($APPARMOR_FILE)"; fi
	if [[ -f $SLICE_FILE ]]; then item slice on "Per-user resource limits ($SLICE_FILE)"; fi
	if [[ ${S_sssd_changed:-} == yes ]] || sssd_has_tokens; then
		item sssd on "SSSD: remove mira-server/mira-engine from ad_gpo_map_remote_interactive"
	fi
	if id "$SERVER_USER" >/dev/null 2>&1; then item account on "The $SERVER_USER system account"; fi
	if [[ -n $U_VENV ]]; then item venv on "mira-engine and Mira Server ($U_VENV)"; fi
	if [[ -d $UV_DIR || -d $UV_PYTHON_DIR ]]; then item uv on "uv and its Python 3.11 ($UV_DIR, $UV_PYTHON_DIR)"; fi
	if [[ -n $U_SRC ]]; then
		if [[ ${S_created_src:-} == yes || $U_SRC == "$SRC_DIR" ]]; then
			item src on "Source checkout $U_SRC"
		else
			item src off "Source directory $U_SRC (mira-engine was installed from it)"
		fi
	fi
	if [[ -f $SBIN_INSTALL || -f $SBIN_UNINSTALL ]]; then item commands on "$SBIN_INSTALL and $SBIN_UNINSTALL"; fi
	if [[ -d $CONFIG_DIR ]]; then item config "$(purge_default)" "Configuration $CONFIG_DIR (incl. client certificate)"; fi
	if [[ -d $DATA_DIR ]]; then item data "$(purge_default)" "Data $DATA_DIR (sessions, audit log) and $LOG_FILE"; fi
	if getent group "$USERS_GROUP" >/dev/null || getent group "$ADMINS_GROUP" >/dev/null; then
		item groups "$(purge_default)" "Groups $USERS_GROUP and $ADMINS_GROUP (membership is lost)"
	fi
	return 0
}

choose() {
	local args=() id selection
	for id in "${ITEMS[@]}"; do
		[[ -n ${PRESENT[$id]:-} ]] && args+=("$id" "${LABEL[$id]}" "${DEFAULT[$id]}")
	done
	if ((${#args[@]} == 0)); then
		ui_msg "Mira Server does not seem to be installed on this host. Nothing to do."
		rm -f "$SELECTION_FILE"
		exit 0
	fi
	selection=$(ui_checklist "Remove these parts of Mira Server? (space toggles)

Users' home directories and ~/.mira are never touched." "${args[@]}")
	[[ -n $selection ]] || cancelled
	if ((INTERACTIVE)) && ! ui_yesno "Remove:

$(for id in $selection; do printf ' - %s\n' "${LABEL[$id]}"; done)
Continue?" no; then
		cancelled
	fi
	printf '%s\n' "$selection" >"$SELECTION_FILE"
	chmod 600 "$SELECTION_FILE"
}

# ---- removal (each step tolerates a partly removed host, so it can be re-run)
remove_path() {
	local path
	for path in "$@"; do
		if [[ -e $path || -L $path ]]; then run rm -rf -- "$path"; fi
	done
}

remove_empty_dir() { rmdir "$1" 2>/dev/null || true; }

engine_users() {
	{
		getent group "$USERS_GROUP" | cut -d: -f4 | tr , '\n' || true
		if [[ -n $U_VENV ]]; then
			ps -eo user:64=,args= | awk -v venv="$U_VENV/bin/python" 'index($0, venv) {print $1}' || true
		fi
	} | sed '/^$/d' | sort -u
}

remove_engines() {
	local user home
	for user in $(engine_users); do
		[[ $user == root ]] && continue
		home=$(getent passwd "$user" | cut -d: -f6 || true)
		if [[ -x $LAUNCHER ]]; then
			(cd / && runuser -u "$user" -- "$LAUNCHER" stop) >>"$LOG_FILE" 2>&1 || warn "could not stop the engine of $user"
		elif [[ -n $U_VENV && -x $U_VENV/bin/python && -n $home ]]; then
			(cd / && runuser -u "$user" -- env -i HOME="$home" USER="$user" MIRA_HOME="$home/.mira" PATH=/usr/bin:/bin \
				"$U_VENV/bin/python" -I -m mira_engine remote stop --json) >>"$LOG_FILE" 2>&1 ||
				warn "could not stop the engine of $user"
		fi
	done
}

remove_service() {
	if systemctl cat mira-server >/dev/null 2>&1; then
		run systemctl disable --now mira-server || warn "could not stop mira-server"
	fi
	remove_path "$UNIT_FILE"
	run systemctl daemon-reload
	systemctl reset-failed mira-server >/dev/null 2>&1 || true
}

reload_caddy() {
	if systemctl is-active --quiet caddy; then
		run systemctl reload-or-restart caddy || warn "Caddy did not reload; check 'journalctl -u caddy'"
	fi
}

remove_caddy() {
	local kind
	if [[ -f $CADDY_SNIPPET ]]; then
		if grep -qE "^[[:space:]]*import[[:space:]]+$CADDY_SNIPPET" "$CADDYFILE" 2>/dev/null; then
			backup_file "$CADDYFILE" >/dev/null
			run sed -i "\\#^[[:space:]]*import[[:space:]]\\{1,\\}$CADDY_SNIPPET#d" "$CADDYFILE"
		fi
		remove_path "$CADDY_SNIPPET"
	fi
	kind=$(caddyfile_kind)
	if [[ $kind == managed || $kind == mira ]]; then
		backup_file "$CADDYFILE" >/dev/null
		if [[ -n ${S_caddyfile_backup:-} && -f ${S_caddyfile_backup:-} ]]; then
			install -m 0644 "$S_caddyfile_backup" "$CADDYFILE"
		else
			printf '# Emptied by the Mira Server uninstaller.\n' >"$CADDYFILE"
		fi
	fi
	reload_caddy
}

remove_caddy_pkg() {
	if pkg_installed caddy; then run apt-get remove -y -q caddy; fi
	if [[ ${S_added_caddy_repo:-} == yes ]]; then remove_path "$CADDY_REPO_LIST" "$CADDY_REPO_KEY"; fi
}

remove_firewall() {
	local port
	command -v ufw >/dev/null 2>&1 || return 0
	for port in ${S_ufw_rules:-}; do run ufw delete allow "$port" || warn "could not remove the ufw rule for $port"; done
}

remove_sudoers() { remove_path "$SUDOERS_FILE"; }

remove_pam() { remove_path "$PAM_SERVER" "$PAM_ENGINE"; }

remove_launcher() {
	remove_path "$LAUNCHER"
	remove_empty_dir "$(dirname "$LAUNCHER")"
}

remove_policy() {
	remove_path "$POLICY_FILE"
	remove_empty_dir "$(dirname "$POLICY_FILE")"
}

remove_apparmor() {
	if [[ -f $APPARMOR_FILE ]] && command -v apparmor_parser >/dev/null 2>&1; then
		run apparmor_parser -R "$APPARMOR_FILE" || warn "could not unload $APPARMOR_FILE"
	fi
	remove_path "$APPARMOR_FILE"
}

remove_slice() {
	remove_path "$SLICE_FILE"
	remove_empty_dir "$(dirname "$SLICE_FILE")"
	run systemctl daemon-reload
}

remove_sssd() {
	[[ -f /etc/sssd/sssd.conf ]] || return 0
	sssd_has_tokens || return 0
	backup_file /etc/sssd/sssd.conf >/dev/null
	run python3 - /etc/sssd/sssd.conf <<'EOF'
import re
import sys

path = sys.argv[1]
out = []
for line in open(path, encoding="utf-8").read().splitlines():
    match = re.match(r"(\s*ad_gpo_map_remote_interactive\s*)=(.*)", line)
    if match:
        items = [v.strip() for v in match.group(2).split(",") if v.strip()]
        items = [v for v in items if v not in ("+mira-server", "+mira-engine")]
        if not items:
            continue
        line = f"{match.group(1).rstrip()} = {', '.join(items)}"
    out.append(line)
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
EOF
	run systemctl restart sssd || warn "sssd did not restart; check 'journalctl -u sssd'"
}

remove_account() {
	if id "$SERVER_USER" >/dev/null 2>&1; then run userdel "$SERVER_USER"; fi
}

remove_venv() { if [[ -n $U_VENV ]]; then remove_path "$U_VENV"; fi; }

remove_uv() { remove_path "$UV_DIR" "$UV_PYTHON_DIR"; }

remove_src() { if [[ -n $U_SRC ]]; then remove_path "$U_SRC"; fi; }

remove_commands() { remove_path "$SBIN_INSTALL" "$SBIN_UNINSTALL"; }

remove_config() { remove_path "$CONFIG_DIR"; }

remove_data() { remove_path "$DATA_DIR"; }

remove_groups() {
	local group
	for group in "$ADMINS_GROUP" "$USERS_GROUP"; do
		if getent group "$group" >/dev/null; then
			run groupdel "$group" || warn "could not remove group $group (is it someone's primary group?)"
		fi
	done
}

report_failure() {
	local code=$1 command=$2 text
	text="Uninstall step failed: ${LABEL[${CURRENT_STEP:-x}]:-${CURRENT_STEP:-setup}}
Command: $command (exit $code)

Last lines of $LOG_FILE:
$(last_log_lines 12)

Fix the problem, then run the uninstaller again; it continues with the same selection:
  sudo bash $WORK_COPY"
	printf '\n%s\n' "$text" >&2
	if ((INTERACTIVE)) && have_whiptail; then ui_msg "$text"; fi
}

finish() {
	local selection=$1 kept=() text
	[[ " $selection " == *" config "* ]] || { [[ -d $CONFIG_DIR ]] && kept+=("$CONFIG_DIR"); }
	[[ " $selection " == *" data "* ]] || { [[ -d $DATA_DIR ]] && kept+=("$DATA_DIR"); }
	[[ " $selection " == *" groups "* ]] || { getent group "$USERS_GROUP" >/dev/null && kept+=("groups $USERS_GROUP, $ADMINS_GROUP"); }
	rm -f "$STATE_FILE" "$SELECTION_FILE"
	if [[ " $selection " == *" data "* ]]; then
		rm -rf "$BACKUP_DIR"
	elif [[ -d $BACKUP_DIR ]] && [[ -n $(ls -A "$BACKUP_DIR") ]]; then
		kept+=("backups of replaced files in $BACKUP_DIR")
	fi
	text="Mira Server was uninstalled."
	if ((${#kept[@]})); then text+=$'\n\nKept:\n'"$(printf ' - %s\n' "${kept[@]}")"; fi
	text+=$'\n\n'"Users' home directories and ~/.mira were not touched."
	say "$text"
	if ((INTERACTIVE)) && have_whiptail; then ui_msg "$text"; fi
	if [[ " $selection " == *" data "* ]]; then rm -f "$LOG_FILE"; fi
	rm -f "$WORK_COPY"
	rmdir "$STATE_DIR" 2>/dev/null || true
}

main() {
	local selection="" id
	parse_args "$@"
	require_root
	use_work_copy "$@"
	prepare_log "$@"
	state_load
	trap on_error ERR
	inventory
	if [[ -s $SELECTION_FILE ]] && ui_yesno "An earlier uninstall did not finish. Continue it with the same selection?" yes; then
		selection=$(cat "$SELECTION_FILE")
	else
		choose
		selection=$(cat "$SELECTION_FILE")
	fi
	selection=$(tr '\n' ' ' <<<"$selection")
	for id in "${ITEMS[@]}"; do
		[[ " $selection " == *" $id "* ]] || continue
		CURRENT_STEP=$id
		LABEL[$id]=${LABEL[$id]:-$id}
		say "Removing: ${LABEL[$id]}"
		"remove_$id"
	done
	CURRENT_STEP=""
	finish "$selection"
}

main "$@"
