# codex-cli pod bash profile.

# Standard bashrc bits.
[ -z "$PS1" ] && return
HISTSIZE=10000
HISTFILESIZE=20000
HISTCONTROL=ignoredups:ignorespace
shopt -s histappend checkwinsize

# Enable bash-completion if installed.
if [ -f /etc/bash_completion ]; then
    . /etc/bash_completion
fi

# Aliases.
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias gst='git status'
alias gd='git diff'
alias gco='git checkout'
alias k='kubectl'

# Prompt: green hostname (codex-cli pod), cwd, git branch.
parse_git_branch() {
    git branch 2>/dev/null | sed -e '/^[^*]/d' -e 's/* \(.*\)/ (\1)/'
}
PS1='\[\033[0;32m\]codex@\h\[\033[0m\]:\[\033[0;34m\]\w\[\033[0;35m\]$(parse_git_branch)\[\033[0m\]\$ '

# Show identity banner on login.
if [ -f "${HOME}/.codex-identity" ]; then
    echo "──── codex-cli ────"
    cat "${HOME}/.codex-identity"
    echo "───────────────────"
    echo "  tmux            → start a persistent session (survives tab close)"
    echo "  codex --help    → codex-cli help"
    echo "  gh auth status  → confirm github identity"
    echo
fi
