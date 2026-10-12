# MongoDB Docker boilerplate

A Docker Compose boilerplate for running MongoDB, with commands to create databases
and users, manage backups and open a database shell.

## Start

From a cloned repository:

```sh
./setup.sh
```

Or install into `mongo<VERSION>` in the current directory:
Requires Bash, Python 3.9+, OpenSSL, Docker and Docker Compose. Git is needed when cloning.

```sh
curl -fsSL https://raw.githubusercontent.com/vladosamerikos/docker-mongo/main/setup.sh | bash
```

Setup selects MongoDB 3–9, generates the administrator password, configures networking
and CPU/RAM limits, and starts the container. Enter accepts each default.
Running it again reconfigures the existing installation, preserving the configured
image tag, credentials and `data/`. Changing the major version requires a separate migration.
The administrator password is saved in `.env` with permissions `600`.

Selecting `8` uses the moving tag `mongo:8`, which follows the latest published
release in major 8, including minor and patch updates. Compose checks for an updated
image on each `docker compose up -d`; setup downloads it before recreating the
container. Running containers do not update until redeployed. Set a full tag in
`MONGO_VERSION` before the first setup to pin a release instead.
The [official MongoDB image](https://hub.docker.com/_/mongo) has no Alpine variant.

To configure manually, copy `.env.example` to `.env`, set
`MONGO_INITDB_ROOT_PASSWORD`, then run `docker compose up -d`.

## Networking

Set `MONGO_PORT_MODE` in `.env`:

| Mode | Published address |
| --- | --- |
| `host` | `127.0.0.1:HOST_MONGO_PORT` |
| `lan` | `0.0.0.0:HOST_MONGO_PORT` |
| `none` | No published ports; Docker network only |

Setup applies network changes automatically. When editing `.env` manually, run
`docker compose down` **before** changing the mode, then `docker compose up -d`.
Set `MONGO_LAN_HOST` to the server's LAN IP/hostname for LAN connection strings.
Setup asks for a Docker network name, defaulting to `mongo<VERSION>_network`
(or the configured network when rerun). It creates missing networks and reuses
existing ones. Networks belonging to another project are marked external and
are not removed by Compose. Their existing settings are preserved.
For manual configuration, set `MONGO_NETWORK_NAME` and `MONGO_NETWORK_EXTERNAL=true`
to reuse a network. External ownership and network isolation (`internal`) are
separate [Compose settings](https://docs.docker.com/reference/compose-file/networks/).

## Databases and users

Run these commands from the repository root. Scripts also work when invoked by
absolute path or from `scripts/`. Use `--env-file PATH` before a command to select
another installation's configuration.

```sh
./scripts/manage.sh                         # Interactive menu
./scripts/manage.sh --help
./scripts/manage.sh status
./scripts/manage.sh list-dbs
./scripts/manage.sh list-users
./scripts/manage.sh create-db -d app
./scripts/manage.sh create-db -d reports --with-user -r dbOwner
./scripts/manage.sh create-user -d app -u app_writer -r w
printf '%s\n' 'my-password' | ./scripts/manage.sh create-user \
  -d app -u app_reader -r r --password-stdin
./scripts/manage.sh delete-user -d app -u app_reader --yes
./scripts/manage.sh delete-db -d reports --delete-users --yes
```

Creation commands return JSON with the summary, password, roles and connection
strings for localhost, LAN and the Docker network. Usernames and passwords are
generated when omitted. `create-user` also creates the database if needed.
`create-db` creates a user only with `--username` or `--with-user`.

| Permission | MongoDB role |
| --- | --- |
| `r` | `read` |
| `w` | `readWrite` (default) |
| `a` | `dbAdmin` |

Combine flags, such as `wa`, or use `read`, `readWrite`, `dbAdmin` or `dbOwner`.
Roles apply to the selected database. Deletion requires `--yes`; deleting a database
with users also requires `--delete-users`. System databases are protected.

Set `STORE_CREDENTIALS=true` in `.env` to append created user credentials to
`.credentials`. The default is `false`. Records contain plaintext passwords,
use permissions `600` and remain after user deletion. `.env`, `.credentials`,
`data/` and `backups/` are ignored by Git.

## Backups and shell

```sh
./scripts/backup.sh export -d app -o app.tar.gz
./scripts/backup.sh export -d app -p /srv/backups -o app.tar.gz
./scripts/backup.sh import -d app_copy -f backups/app.tar.gz --yes
./scripts/backup.sh import -d app_copy -f backups/app.tar.gz --drop --yes
# Import backups created by the previous archive format:
./scripts/backup.sh import -d app_copy -f old_backup.gz --source-database app --yes
./scripts/login-as-root.sh
./scripts/login-as-root.sh --eval 'print(db.version());'
```

Exports default to `backups/`, use private `.tar.gz` files and never overwrite
existing files. Restores merge collections; `--drop` replaces matching collections.
Application backups contain data and collection metadata; migrate users and roles separately.
Legacy archive imports require MongoDB 3.2+; renaming them requires 3.4+.
