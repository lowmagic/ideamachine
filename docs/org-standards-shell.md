<!-- lowmagic/.github code-standards/shell.md at cca8c65ba8c04cfe250b83b91ba69947f014e082. Change it there, then run make sync-standards. -->
# Shell standards

Decisions for shell scripts, beneath the org [standards](https://github.com/lowmagic/.github/blob/main/code-standards/README.md).

## Writing a script

- Scripts are bash. Line 1 is `#!/usr/bin/env bash`. A comment follows, saying what the script does and, if it takes arguments, how to call it. Then `set -euo pipefail`.
- A file that's sourced, not run, isn't executable and has no shebang. Line 1 is `# shellcheck shell=bash`, and a comment follows, saying what it holds.
- A script finds its other files from its own path (`$(dirname "$0")`), not the working directory, unless its header says where to run it.
- A temporary file comes from `mktemp`, and a `trap … EXIT` removes it.
- A heredoc's delimiter is quoted (`<<'EOF'`) unless the text interpolates.
- Errors go to stderr, and the script exits non-zero.
- A script that runs on macOS works in its bash 3.2: no `mapfile`, associative arrays or `${var,,}`.

## Safety

- A script that installs, sets up or deploys is safe to re-run, and its header says so.
- A secret the operator types is read with `read -rs`.
- Nothing pipes a download into a shell, and a tool a script installs from outside the distribution's packages is a pinned version.
- Input from the operator or the network is checked against a pattern before it goes into a command string, such as a `sed` program or `sh -c`.

## Checking

- `make check` runs shellcheck over every tracked script, meaning each file with a `.sh` name or a shell shebang, and shellcheck reports nothing. A `# shellcheck disable=` says why.
