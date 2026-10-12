#!/usr/bin/env bash
REPOSITORY_URL='https://github.com/vladosamerikos/docker-mongo.git'

set -euo pipefail
unset MONGO_SETUP_VERSION

fail() { printf 'ERROR %s\n' "$1" >&2; exit "${2:-1}"; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  cat <<'HELP'
Usage: bash setup.sh
       curl -fsSL https://raw.githubusercontent.com/vladosamerikos/docker-mongo/main/setup.sh | bash

Configure the current Mongo template, or clone it into ./mongo<version>.
Requires an interactive terminal, Git, OpenSSL, Python 3.9+, Docker and Compose v2+.
Existing installations keep their MongoDB version and administrator credentials.
HELP
  exit 0
fi
if (( $# )); then
  fail 'Unknown argument. Use --help.' 2
fi
for dependency in python3 openssl docker; do
  command -v "$dependency" >/dev/null 2>&1 || fail "Required command is missing: $dependency"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail 'Python 3.9 or newer is required.'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 or newer is required.'
docker info >/dev/null 2>&1 || fail 'Docker is not accessible. Start Docker and check your permissions.'
# Never read answers from the pipe that contains the script itself.
{ exec 3<> /dev/tty; } 2>/dev/null || fail 'An interactive terminal is required (including when using curl | bash).'
is_template() {
  [[ -f "$1/docker-compose.yml" && -f "$1/scripts/lib/setup.py" && -f "$1/scripts/lib/mongo_cli.py" ]]
}
template_root=''
search_dir="$PWD"
while :; do
  if is_template "$search_dir"; then template_root="$search_dir"; break; fi
  [[ "$search_dir" == / ]] && break
  search_dir="$(dirname -- "$search_dir")"
done
if [[ -z "$template_root" && -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
  source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  if is_template "$source_dir"; then template_root="$source_dir"; fi
fi
if [[ -z "$template_root" ]]; then
  command -v git >/dev/null 2>&1 || fail 'Git is required to download the template.'
  while :; do
    printf 'MongoDB major version (3-9) [6]: ' >&3
    IFS= read -r version <&3 || exit 1
    version="${version:-6}"
    if [[ ! "$version" =~ ^[3-9]$ ]]; then
      printf 'ERROR Enter a major version between 3 and 9.\n' >&3
      continue
    fi
    template_root="$PWD/mongo$version"
    if [[ -e "$template_root" ]]; then
      if is_template "$template_root"; then
        printf 'INFO  Reusing %s.\n' "$template_root" >&3
        break
      fi
      printf 'ERROR %s already exists and is not a compatible template. Choose another version or directory.\n' "$template_root" >&3
      continue
    fi
    printf 'INFO  Cloning %s into %s.\n' "$REPOSITORY_URL" "$template_root" >&3
    if git clone -- "$REPOSITORY_URL" "$template_root"; then
      if ! is_template "$template_root"; then
        fail 'The repository does not contain the updated Mongo template. Publish setup.sh and scripts/lib/setup.py first.'
      fi
      break
    fi
    fail 'Clone failed. No configuration has been changed. Fix the connection and rerun setup.'
  done
  export MONGO_SETUP_VERSION="$version"
fi
exec python3 "$template_root/scripts/lib/setup.py" "$template_root" <&3
