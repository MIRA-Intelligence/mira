#!/usr/bin/env bash
set -e

# Terminal colors
YELLOW="\033[33m"
CYAN="\033[36m"
GREEN="\033[32m"
RED="\033[31m"
RESET="\033[0m"

echo "Welcome to MedPilot Installer"
echo "-----------------------------"

if ! command -v conda &> /dev/null; then
    echo -e "${YELLOW}Warning: conda is not installed. It is highly recommended to run MedPilot in an isolated conda environment.${RESET}"
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
    read -p "Do you want to create a new conda environment 'medpilot' for isolation? [Y/n] " -r || true
    echo
    if [[ "$REPLY" =~ ^[Yy]$ ]] || [[ -z "$REPLY" ]]; then
        if conda env list | awk '{print $1}' | grep -x "medpilot" > /dev/null; then
            echo -e "${YELLOW}Conda environment 'medpilot' already exists.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate medpilot || true
        else
            echo -e "${CYAN}Creating conda environment 'medpilot' (Python 3.11)...${RESET}"
            conda create -n medpilot python=3.11 pip -y
            echo -e "${GREEN}✓ Conda environment 'medpilot' created.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate medpilot || true
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
            echo -e "${YELLOW}Please run 'conda activate $env_choice' before using MedPilot further.${RESET}"
            eval "$(conda shell.bash hook 2>/dev/null)" || true
            conda activate "$env_choice" || true
        fi
    fi
fi

echo -e "\n${CYAN}Checking Python version...${RESET}"
if command -v python >/dev/null 2>&1; then
    PY_VERSION=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "Unknown")
    if ! python -c 'import sys; exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        echo -e "${RED}Error: Current Python version is $PY_VERSION. MedPilot requires Python >= 3.11.${RESET}"
        echo -e "${YELLOW}Please select or create an environment with Python 3.11+. Installation might fail.${RESET}"
    else
        echo -e "${GREEN}✓ Python $PY_VERSION detected.${RESET}"
    fi
else
    echo -e "${RED}Error: Python not found.${RESET}"
fi

echo -e "\n${CYAN}Installing MedPilot via pip...${RESET}"
pip install -e .

echo -e "\n${GREEN}✓ Installation complete! Run 'medpilot onboard' to setup your workspace.${RESET}"
