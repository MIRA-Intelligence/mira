#!/usr/bin/env bash
set -e

# Terminal colors
YELLOW="\033[33m"
CYAN="\033[36m"
GREEN="\033[32m"
RED="\033[31m"
RESET="\033[0m"

echo "Welcome to Mira Installer"
echo "-----------------------------"

DEFAULT_BRANCH="main"
INSTALL_BRANCH="${MIRA_BRANCH:-}"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -z "$INSTALL_BRANCH" ] && [ -t 0 ]; then
        read -p "Which git branch should be installed? [${DEFAULT_BRANCH}] " -r || true
        INSTALL_BRANCH="${REPLY:-$DEFAULT_BRANCH}"
    fi

    CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
    if [ -n "$INSTALL_BRANCH" ] && [ "$CURRENT_BRANCH" != "$INSTALL_BRANCH" ]; then
        echo -e "${CYAN}Switching to branch '${INSTALL_BRANCH}'...${RESET}"
        git fetch origin "$INSTALL_BRANCH" >/dev/null 2>&1 || true
        if git show-ref --verify --quiet "refs/heads/${INSTALL_BRANCH}"; then
            git checkout "$INSTALL_BRANCH"
        elif git show-ref --verify --quiet "refs/remotes/origin/${INSTALL_BRANCH}"; then
            git checkout -b "$INSTALL_BRANCH" --track "origin/$INSTALL_BRANCH"
        else
            echo -e "${RED}Error: Branch '${INSTALL_BRANCH}' not found locally or on origin.${RESET}"
            exit 1
        fi
    fi
else
    echo -e "${YELLOW}Warning: Not in a git repository. Branch selection skipped.${RESET}"
fi

if ! command -v conda &> /dev/null; then
    echo -e "${YELLOW}Warning: conda is not installed. It is highly recommended to run Mira in an isolated conda environment.${RESET}"
    read -p "Do you want to create a standard Python virtual environment instead? [Y/n] " -r || true
    echo
    if [[ "$REPLY" =~ ^[Yy]$ ]] || [[ -z "$REPLY" ]]; then
        echo -e "${CYAN}Creating standard Python venv at venv...${RESET}"
        python -m venv venv
        source venv/bin/activate
        echo -e "${GREEN}✓ Virtual environment created.${RESET}"
    fi
else
    echo -e "${CYAN}Conda is installed.${RESET}"
    read -p "Do you want to create a new conda environment 'mira' for isolation? [Y/n] " -r || true
    echo
    if [[ "$REPLY" =~ ^[Yy]$ ]] || [[ -z "$REPLY" ]]; then
        if conda env list | awk '{print $1}' | grep -x "mira" > /dev/null; then
            echo -e "${YELLOW}Conda environment 'mira' already exists.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate mira || true
        else
            echo -e "${CYAN}Creating conda environment 'mira' (Python 3.11)...${RESET}"
            conda create -n mira python=3.11 pip -y
            echo -e "${GREEN}✓ Conda environment 'mira' created.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate mira || true
        fi
    else
        echo "Available conda environments:"
        ENV_LIST=($(conda env list | grep -v "^#" | awk '{print $1}' | grep -v '^$'))
        for i in "${!ENV_LIST[@]}"; do
            printf "%4d  %s\n" "$((i+1))" "${ENV_LIST[$i]}"
        done
        read -p "Select an existing environment by name or number (or leave empty to skip): " env_choice || true
        if [ -n "$env_choice" ]; then
            if [[ "$env_choice" =~ ^[0-9]+$ ]] && [ "$env_choice" -le "${#ENV_LIST[@]}" ] && [ "$env_choice" -gt 0 ]; then
                env_choice="${ENV_LIST[$((env_choice-1))]}"
            fi
            echo -e "${GREEN}Selected environment: $env_choice${RESET}"
            echo -e "${YELLOW}Please run 'conda activate $env_choice' before using Mira further.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate "$env_choice" || true
        fi
    fi
fi

echo -e "\n${CYAN}Checking Python version...${RESET}"
if command -v python >/dev/null 2>&1; then
    PY_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "Unknown")
    if ! python -c 'import sys; exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        echo -e "${RED}Error: Current Python version is $PY_VERSION. Mira requires Python >= 3.11.${RESET}"
        echo -e "${YELLOW}Please select or create an environment with Python 3.11+. Installation might fail.${RESET}"
    else
        echo -e "${GREEN}✓ Python $PY_VERSION detected.${RESET}"
    fi
else
    echo -e "${RED}Error: Python not found.${RESET}"
fi

echo -e "\n${CYAN}Installing Mira via pip...${RESET}"
pip install -e .

echo -e "\n${GREEN}✓ Installation complete! Run 'mira onboard' to setup your workspace.${RESET}"
