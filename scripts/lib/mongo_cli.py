#!/usr/bin/env python3
"""Commands for the MongoDB Docker boilerplate."""
import argparse
import datetime as dt
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
MARKER = '__MONGO_JSON__'
SYSTEM_DATABASES = {'admin', 'local', 'config'}


class ToolError(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(',', ':')))


def message(text, level='INFO', stream=None):
    stream = stream or sys.stderr
    color = stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    colors = {'INFO': '36', 'OK': '32', 'ERROR': '31', 'WARN': '33'}
    label = f'\033[{colors.get(level, "36")}m{level:<5}\033[0m' if color else f'{level:<5}'
    print(f'{label} {text}', file=stream)


def load_dotenv(path, environ=None):
    """Read values without sourcing executable shell code or splitting passwords."""
    environ = os.environ if environ is None else environ
    values = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as error:
        raise ToolError(f'Cannot read configuration file: {path}') from error
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise ToolError(f'Invalid .env assignment on line {number}.')
        key, value = match.groups()
        literal = value.startswith("'")
        if value.startswith(('"', "'")):
            delimiter = value[0]
            closing = None
            for index in range(1, len(value)):
                backslashes = len(value[:index]) - len(value[:index].rstrip('\\'))
                if value[index] == delimiter and backslashes % 2 == 0:
                    closing = index
                    break
            if closing is None or (value[closing + 1:].strip() and not value[closing + 1:].lstrip().startswith('#')):
                raise ToolError(f'Invalid quoted value on .env line {number}.')
            value = value[1:closing]
            if literal:
                value = value.replace("\\'", "'")
            else:
                escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"', '$': '\x00DOTENV_DOLLAR\x00'}
                value = re.sub(r'\\([nrt\\"$])', lambda item: escapes[item[1]], value)
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        if not literal:
            def expand(item):
                name, default = item[1], item[2]
                return environ.get(name, values.get(name, '')) or (default or '')
            value = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                           lambda item: expand(item) if item[1] else environ.get(item[3], values.get(item[3], '')), value)
            value = value.replace('\x00DOTENV_DOLLAR\x00', '$')
        values[key] = value
    return values


class Config:
    def __init__(self, env_file=None, environ=None):
        environ = os.environ if environ is None else environ
        self.path = Path(env_file or environ.get('MONGO_ENV_FILE', TEMPLATE_ROOT / '.env')).expanduser().resolve()
        self.root = self.path.parent
        self.values = load_dotenv(self.path, environ)
        self.values.update(environ)
        for key in ('CONTAINER_NAME', 'MONGO_INITDB_ROOT_USERNAME', 'MONGO_INITDB_ROOT_PASSWORD'):
            if not self.values.get(key):
                raise ToolError(f'Missing required configuration: {key}')
        self.container = self.values['CONTAINER_NAME']
        self.mode = self.values.get('MONGO_PORT_MODE', 'host')
        if self.mode not in ('none', 'host', 'lan'):
            raise ToolError('MONGO_PORT_MODE must be none, host or lan.')
        self.hostname = self.values.get('MONGO_HOSTNAME', self.container)
        self.port = self.values.get('HOST_MONGO_PORT', '27017')
        if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
            raise ToolError('HOST_MONGO_PORT must be between 1 and 65535.')
        self.store = boolean(self.values.get('STORE_CREDENTIALS', self.values.get('storecredentials', 'false')))
        self.credentials = self.root / '.credentials'


def boolean(value):
    value = str(value).lower()
    if value not in ('true', 'false', '1', '0', 'yes', 'no', 'on', 'off'):
        raise ToolError('STORE_CREDENTIALS must be true or false.')
    return value in ('true', '1', 'yes', 'on')


def database_name(value, protect=False):
    if not value or len(value.encode('utf-8')) > 63 or re.search(r'[/\\. "$*<>:|?\x00-\x1f]', value):
        raise ToolError('Database names must be 1-63 UTF-8 bytes without spaces, dots, control characters or /\\"$*<>:|?.')
    if protect and value in SYSTEM_DATABASES:
        raise ToolError('Modifying admin, local or config is not allowed by this tool.')
    return value


def roles_for(value, database):
    aliases = {'r': 'read', 'w': 'readWrite', 'a': 'dbAdmin'}
    if re.fullmatch('[rwa]+', value or ''):
        names = [aliases[letter] for letter in value]
    else:
        names = [item.strip() for item in (value or '').split(',')]
    if not names or any(item not in ('read', 'readWrite', 'dbAdmin', 'dbOwner') for item in names):
        raise ToolError('Use permissions r/w/a (e.g. rwa), or read,readWrite,dbAdmin,dbOwner.')
    # readWrite already includes read.
    names = list(dict.fromkeys(names))
    if 'readWrite' in names and 'read' in names:
        names.remove('read')
    return [{'role': item, 'db': database} for item in names]


class Mongo:
    def __init__(self, config):
        self.config = config
        if not shutil.which('docker'):
            raise ToolError('Docker CLI is required.')
        probe = self.docker('inspect', '--format', '{{.State.Running}}', config.container, check=False)
        if probe.returncode or probe.stdout.strip() != 'true':
            raise ToolError(f'Container {config.container!r} is not running, or Docker is inaccessible.')
        probe = self.docker('exec', config.container, 'sh', '-c',
                            'if command -v mongo >/dev/null 2>&1; then echo mongo; elif command -v mongosh >/dev/null 2>&1; then echo mongosh; else exit 1; fi', check=False)
        if probe.returncode:
            raise ToolError('Neither mongo nor mongosh is installed in the container.')
        self.shell = probe.stdout.strip()

    @staticmethod
    def docker(*args, check=True, **kwargs):
        try:
            result = subprocess.run(['docker', *args], text=True, capture_output=True, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError('Docker could not execute the command.') from error
        if check and result.returncode:
            raise ToolError('Docker command failed: ' + result.stderr.strip()[-1000:])
        return result

    def request(self, payload):
        credentials = {'username': self.config.values['MONGO_INITDB_ROOT_USERNAME'],
                       'password': self.config.values['MONGO_INITDB_ROOT_PASSWORD']}
        code = 'var rootCredentials = ' + json.dumps(credentials) + ';\nvar request = ' + json.dumps(payload) + ';\n'
        code += Path(__file__).with_name('operations.js').read_text()
        result = self.shell_script(self.config.container, self.shell, code, timeout=60)
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(MARKER):
                response = json.loads(line[len(MARKER):])
                if response['ok'] and result.returncode == 0:
                    return response['result']
                raise ToolError(response.get('error', 'MongoDB operation failed.'))
        # Shell diagnostics can include evaluated code containing credentials.
        raise ToolError(f'MongoDB shell failed (exit {result.returncode}). Check root credentials and container health.')

    @classmethod
    def shell_script(cls, container, shell, source, timeout=60):
        # MongoDB 3.x cannot reliably load scripts from pipes; use a private file.
        script = ('file=$(mktemp /tmp/mongo-script.XXXXXXXX.js) || exit 1; '
                  'trap \'rm -f -- "$file"\' EXIT HUP INT TERM; '
                  'cat > "$file" || exit 1; "$@" "$file"')
        return cls.docker('exec', '-i', container, 'sh', '-c', script, 'sh', shell,
                          '--quiet', '--norc', '--nodb', input=source, check=False, timeout=timeout)


def connection_strings(config, database, username=None, password=None):
    credentials = f'{quote(username, safe="")}:{quote(password, safe="")}@' if username else ''
    suffix = f'/{quote(database, safe="")}' + (f'?authSource={quote(database, safe="")}' if username else '')
    def uri(host, port):
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        return f'mongodb://{credentials}{host}:{port}{suffix}'
    lan_host = config.values.get('MONGO_LAN_HOST', '')
    return {'localhost': uri('127.0.0.1', config.port) if config.mode != 'none' else None,
            'lan': uri(lan_host, config.port) if config.mode == 'lan' and lan_host else None,
            'mongo_network': uri(config.hostname, '27017')}


def open_credentials(config):
    """Never follow symlinks; enforce an owner-only regular file."""
    try:
        fd = os.open(config.credentials, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid() or file_stat.st_nlink != 1:
                raise ToolError('.credentials must be an ordinary file owned by the current user, without hard links.')
            os.fchmod(fd, 0o600)
            return fd
        except (OSError, ToolError):
            os.close(fd)
            raise
    except OSError as error:
        raise ToolError('Cannot securely open .credentials; check its path and permissions.') from error


def store_credentials(config, record):
    record['credentials_stored'] = True
    try:
        with os.fdopen(open_credentials(config), 'a', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, ensure_ascii=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except (ToolError, OSError) as error:
        record['credentials_stored'] = False
        raise ToolError('MongoDB operation succeeded but credentials could not be saved.', details=record) from error


def manage(mongo, args):
    action = args.command
    request = {'action': action}
    name = getattr(args, 'database', None)
    if name is not None:
        request['database'] = database_name(name, protect=action.startswith(('create-', 'delete-')))
    if action in ('create-user', 'create-db'):
        user = args.username
        if action == 'create-user' or getattr(args, 'with_user', False):
            user = user or 'user_' + secrets.token_hex(6)
        if not user and (args.password is not None or args.password_stdin or args.permissions):
            raise ToolError('Use --with-user or --username when supplying a password or permissions to create-db.')
        password = None
        if user:
            if '\x00' in user or not user.strip():
                raise ToolError('Username cannot be empty or contain a NUL character.')
            if args.password is not None and args.password_stdin:
                raise ToolError('Use either --password or --password-stdin, not both.')
            password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else args.password
            if password is None:
                password = secrets.token_urlsafe(24)
            if not password or '\x00' in password:
                raise ToolError('Password cannot be empty or contain a NUL character.')
            request.update(username=user, password=password, roles=roles_for(args.permissions or 'w', name))
        if user and mongo.config.store:
            os.close(open_credentials(mongo.config))  # Fail before creating a user if storage is unavailable.
        result = mongo.request(request)
        record = {'ok': True, 'action': action, **result,
                  'password': password, 'connections': connection_strings(mongo.config, name, user, password),
                  'credentials_stored': False, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        if user and mongo.config.store:
            store_credentials(mongo.config, record)
        return record
    if action.startswith('delete-'):
        if not args.yes:
            raise ToolError('Deletion requires --yes. Use tui for a guided confirmation.')
        if action == 'delete-user':
            root_user = mongo.config.values['MONGO_INITDB_ROOT_USERNAME']
            if name == 'admin' and args.username == root_user:
                raise ToolError('Deleting the configured root account is not allowed.')
            request['username'] = args.username
        else:
            request['delete_users'] = args.delete_users
    if action == 'list-dbs':
        request['all'] = args.all
    return {'ok': True, 'action': action, **mongo.request(request)}


def prompt(text, default=None, secret=False):
    suffix = f' [{default}]' if default else ''
    value = getpass.getpass(text + suffix + ': ') if secret else input(text + suffix + ': ')
    return value or default or ''


def tui(mongo):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ToolError('The menu requires a terminal. Use --help for non-interactive commands.')
    entries = {'1': 'list-users', '2': 'list-dbs', '3': 'create-user', '4': 'create-db',
               '5': 'delete-user', '6': 'delete-db'}
    while True:
        print('\n' + '=' * 58)
        message(f'MongoDB manager | {mongo.config.container} | {mongo.shell}', stream=sys.stdout)
        print('-' * 58)
        print('  1  List users              2  List databases\n'
              '  3  Create user             4  Create database\n'
              '  5  Delete user             6  Delete database\n'
              '  0  Exit')
        try:
            choice = prompt('Select an option')
            if choice in ('0', 'q', 'quit'):
                return
            if choice not in entries:
                message('Choose a number from 0 to 6.', 'WARN')
                continue
            action = entries[choice]
            options = argparse.Namespace(command=action, database=None, username=None, password=None,
                                         password_stdin=False, permissions=None, with_user=False,
                                         delete_users=False, yes=False, all=False)
            if action not in ('list-users', 'list-dbs'):
                print('\nExisting databases:')
                databases = mongo.request({'action': 'list-dbs'})['databases']
                print('  ' + ', '.join(databases) if databases else '  (none)')
                options.database = prompt('Database name')
            if action == 'create-db':
                options.with_user = prompt('Create a user too? y/N').lower() == 'y'
            if action == 'create-user' or options.with_user:
                options.username = prompt('Username (blank generates one)') or None
                options.password = prompt('Password (blank generates one)', secret=True) or None
                options.permissions = prompt('Permissions: r=read, w=readWrite, a=dbAdmin', 'w')
            if action == 'delete-user':
                users = mongo.request({'action': 'list-users', 'database': options.database})['users']
                print('Users: ' + ', '.join(item['username'] for item in users))
                options.username = prompt('Username')
            if action == 'delete-db':
                options.delete_users = prompt('Also delete database users? y/N').lower() == 'y'
            if action.startswith('delete-'):
                message('This operation permanently removes the selected data/account.', 'WARN')
                if prompt('Type the database name to confirm') != options.database:
                    message('Cancelled.', 'INFO')
                    continue
                options.yes = True
            if action.startswith('create-') and prompt('Apply this change? y/N').lower() != 'y':
                message('Cancelled.', 'INFO')
                continue
            record = manage(mongo, options)
            message('Operation completed.', 'OK')
            print(json.dumps(record, indent=2, ensure_ascii=True))
            prompt('Press Enter to continue')
        except ToolError as error:
            message(str(error), 'ERROR')
            if error.details:
                print(json.dumps(error.details, indent=2))
        except (EOFError, KeyboardInterrupt):
            print()
            return


def tool_credentials(mongo):
    username = mongo.config.values['MONGO_INITDB_ROOT_USERNAME']
    password = mongo.config.values['MONGO_INITDB_ROOT_PASSWORD']
    if any(char in username + password for char in '\r\n'):
        raise ToolError('Backup tools require root credentials without line breaks.')
    return (username + '\n' + password + '\n').encode()


def run_database_tool(mongo, tool, args, extra_input=None):
    # Send secrets through stdin, not through the host Docker command line.
    script = 'IFS= read -r user; IFS= read -r pass; exec "$@" --username="$user" --password="$pass" --authenticationDatabase=admin --host=127.0.0.1 --port=27017'
    command = ['docker', 'exec', '-i', mongo.config.container, 'sh', '-c', script, 'sh', tool, *args]
    if isinstance(extra_input, Path):
        # Stream large archives; temporary output files prevent pipe deadlocks.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=errors) as process:
                try:
                    process.stdin.write(tool_credentials(mongo))
                    with extra_input.open('rb') as source:
                        shutil.copyfileobj(source, process.stdin)
                    process.stdin.close()
                except BrokenPipeError:
                    process.stdin.close()
                status = process.wait()
            output.seek(0)
            errors.seek(0)
            result = subprocess.CompletedProcess(command, status, output.read(), errors.read())
    else:
        result = subprocess.run(command, input=tool_credentials(mongo) + (extra_input or b''), capture_output=True)
    if result.returncode:
        diagnostic = result.stderr.decode(errors='replace')[-1500:]
        diagnostic = diagnostic.replace(mongo.config.values['MONGO_INITDB_ROOT_PASSWORD'], '<redacted>')
        raise ToolError(f'{tool} failed: {diagnostic.strip()}')
    return result


def validate_tar(path):
    try:
        with tarfile.open(path, 'r:gz') as archive:
            members = archive.getmembers()
            roots = set()
            for member in members:
                parts = Path(member.name).parts
                if not parts or member.name.startswith('/') or '..' in parts or not (member.isdir() or member.isfile()):
                    raise ToolError('Unsafe path or link in backup archive.')
                roots.add(parts[0])
            if len(roots) != 1:
                raise ToolError('Backup archive must contain exactly one database directory.')
            return database_name(roots.pop())
    except (tarfile.TarError, OSError) as error:
        raise ToolError('Cannot read the tar.gz backup archive.') from error


def backup(mongo, args):
    name = database_name(args.database, protect=args.command == 'import')
    if args.command == 'import' and not args.yes:
        raise ToolError('Restore requires --yes. Existing documents may be replaced on matching _id values.')
    if args.command == 'import':
        path = Path(args.file).expanduser().resolve()
        if not path.is_file():
            raise ToolError(f'Backup file does not exist: {path}')
        # Legacy --archive backups require 3.2+; portable tar.gz also supports 3.0.
        if not tarfile.is_tarfile(path):
            help_text = mongo.docker('exec', mongo.config.container, 'mongorestore', '--help').stdout
            if '--archive' not in help_text:
                raise ToolError('This image cannot restore legacy gzip archives. Use a portable tar.gz backup.')
            source = database_name(args.source_database or name)
            if '--nsFrom' in help_text:
                restore_args = ['--archive', '--gzip', '--nsInclude', source + '.*',
                                '--nsFrom', source + '.*', '--nsTo', name + '.*']
            else:
                if source != name:
                    raise ToolError('MongoDB 3.2 archive tools cannot rename databases. Restore to the source name or use a portable tar.gz backup.')
                restore_args = ['--archive', '--gzip', '--db', name]
            if args.drop:
                restore_args.append('--drop')
            run_database_tool(mongo, 'mongorestore', restore_args, path)
            return {'ok': True, 'action': 'import', 'database': name, 'source_database': source, 'file': str(path)}
        source = validate_tar(path)
        if args.source_database and args.source_database != source:
            raise ToolError('--source-database does not match the archive database.')
    else:
        if name not in mongo.request({'action': 'list-dbs', 'all': True})['databases']:
            raise ToolError(f'Database does not exist: {name}')
        directory = Path(args.path or mongo.config.root / 'backups').expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        filename = args.output or f'{name}_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}_{secrets.token_hex(3)}.tar.gz'
        if Path(filename).name != filename:
            raise ToolError('--output must be a filename; use --path for the directory.')
        path = directory / filename
        if path.exists() or path.is_symlink():
            raise ToolError('Backup output already exists. Choose a different filename.')
    temporary = mongo.docker('exec', mongo.config.container, 'mktemp', '-d', '/tmp/mongo-tools.XXXXXXXX').stdout.strip()
    try:
        if args.command == 'export':
            message(f'Exporting {name} to {path}')
            run_database_tool(mongo, 'mongodump', ['--db', name, '--out', temporary])
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.mongo-backup-', delete=False) as handle:
                staging = Path(handle.name)
                try:
                    result = subprocess.run(['docker', 'exec', mongo.config.container, 'tar', '-C', temporary, '-czf', '-', name], stdout=handle, stderr=subprocess.PIPE)
                    if result.returncode:
                        raise ToolError('Failed to compress the database backup.')
                    handle.flush()
                    os.fsync(handle.fileno())
                    # Link publishes the complete file without overwriting a concurrent export.
                    os.link(staging, path)
                finally:
                    staging.unlink(missing_ok=True)
            return {'ok': True, 'action': 'export', 'database': name, 'file': str(path),
                    'format': 'tar.gz', 'bytes': path.stat().st_size}
        message(f'Restoring {source} into {name}')
        with path.open('rb') as handle:
            result = subprocess.run(['docker', 'exec', '-i', mongo.config.container, 'tar', '-C', temporary, '-xzf', '-'], stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise ToolError('Failed to unpack the database backup.')
        restore_args = ['--db', name, '--stopOnError', str(Path(temporary) / source)]
        if args.drop:
            restore_args.insert(0, '--drop')
        run_database_tool(mongo, 'mongorestore', restore_args)
        return {'ok': True, 'action': 'import', 'database': name, 'source_database': source, 'file': str(path)}
    finally:
        # Only the mktemp directory created by this operation is removed.
        mongo.docker('exec', mongo.config.container, 'rm', '-rf', '--', temporary, check=False)


class Parser(argparse.ArgumentParser):
    def error(self, text):
        raise ToolError(text + ' (use --help for usage)')


def parser_for(tool):
    parser = Parser(prog={'manage': 'manage.sh', 'backup': 'backup.sh', 'login': 'login-as-root.sh'}[tool],
                    description={'manage': 'Manage MongoDB users and databases. Commands return JSON; tui opens the menu.',
                                 'backup': 'Portable MongoDB backups and restores. Commands return JSON.',
                                 'login': 'Open the root shell, or execute a root command.'}[tool])
    parser.add_argument('--env-file', help='Configuration file (default: template .env, independent of working directory)')
    parser.add_argument('--no-color', action='store_true', help='Disable terminal colors (also supported: NO_COLOR)')
    if tool == 'manage':
        commands = parser.add_subparsers(dest='command', parser_class=Parser)
        commands.add_parser('tui', aliases=['interactive'], help='Open the interactive menu (default without arguments)')
        commands.add_parser('status', help='Show server version and detected shell')
        commands.add_parser('list-dbs', aliases=['list-databases'], help='List databases').add_argument('--all', action='store_true')
        commands.add_parser('list-users', help='List users and roles').add_argument('-d', '--database')
        for action in ('create-user', 'create-db'):
            command = commands.add_parser(action, help='Create a user/database and return credentials as JSON')
            command.add_argument('-d', '--database', required=True)
            command.add_argument('-u', '--username', help='Omit to generate a username (create-db requires --with-user)')
            command.add_argument('-p', '--password', help='Omit to generate a password; use --password-stdin for sensitive automation')
            command.add_argument('--password-stdin', action='store_true', help='Read password from one line on stdin')
            command.add_argument('-r', '--permissions', '--roles', help='r/w/a or comma-separated read,readWrite,dbAdmin,dbOwner (default: w)')
            if action == 'create-db':
                command.add_argument('--with-user', action='store_true', help='Also create a generated user')
        for action in ('delete-user', 'delete-db'):
            command = commands.add_parser(action, help='Delete a user/database (requires --yes)')
            command.add_argument('-d', '--database', required=True)
            command.add_argument('-y', '--yes', action='store_true')
            if action == 'delete-user':
                command.add_argument('-u', '--username', required=True)
            else:
                command.add_argument('--delete-users', action='store_true', help='Also remove accounts authenticated against this database')
    elif tool == 'backup':
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        export = commands.add_parser('export', help='Write a private tar.gz backup (works from MongoDB 3.0 onward)')
        export.add_argument('-d', '--database', required=True)
        export.add_argument('-o', '--output', help='Output filename; existing files are never overwritten')
        export.add_argument('-p', '--path', help='Output directory (default: template-root/backups)')
        restore = commands.add_parser('import', help='Restore portable backups, or legacy --archive --gzip backups')
        restore.add_argument('-d', '--database', required=True)
        restore.add_argument('-f', '--file', required=True)
        restore.add_argument('--source-database', help='Source name when renaming a legacy archive database')
        restore.add_argument('--drop', action='store_true', help='Drop matching collections before restoring')
        restore.add_argument('-y', '--yes', action='store_true', help='Confirm restore without prompting')
    else:
        parser.add_argument('--eval', dest='code', help='Execute JavaScript without opening an interactive shell')
    return parser


def login(mongo, code):
    if code is not None:
        root = {'username': mongo.config.values['MONGO_INITDB_ROOT_USERNAME'], 'password': mongo.config.values['MONGO_INITDB_ROOT_PASSWORD']}
        source = ('var connection = new Mongo("127.0.0.1:27017"); var db = connection.getDB("admin"); '
                  'if (!db.auth(' + json.dumps(root['username']) + ', ' + json.dumps(root['password']) + ')) { quit(1); }\n' + code)
        result = mongo.shell_script(mongo.config.container, mongo.shell, source)
        sys.stdout.write(result.stdout)
        if result.returncode:
            raise ToolError(f'Root shell exited with status {result.returncode}.')
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ToolError('Interactive root login requires a terminal. Use --eval for automation.')
    message(f'Opening {mongo.shell} as root in {mongo.config.container}')
    command = ['docker', 'exec', '-it', mongo.config.container, mongo.shell, '--norc',
               '--host', '127.0.0.1', '--port', '27017', '--authenticationDatabase', 'admin',
               '--username', mongo.config.values['MONGO_INITDB_ROOT_USERNAME'],
               '--password', mongo.config.values['MONGO_INITDB_ROOT_PASSWORD']]
    result = subprocess.run(command)
    if result.returncode:
        raise ToolError(f'Root shell exited with status {result.returncode}.')


def main():
    tool = sys.argv[1]
    interactive = False
    try:
        if sys.version_info < (3, 9):
            raise ToolError('Python 3.9 or newer is required.')
        arguments = sys.argv[2:]
        if arguments == ['help'] or tool == 'backup' and not arguments:
            arguments = ['--help']
        args = parser_for(tool).parse_args(arguments)
        if args.no_color:
            os.environ['NO_COLOR'] = '1'
        config = Config(args.env_file)
        mongo = Mongo(config)
        if tool == 'manage':
            args.command = {'list-databases': 'list-dbs', 'interactive': 'tui'}.get(args.command, args.command or 'tui')
            interactive = args.command == 'tui' and sys.stdin.isatty() and sys.stdout.isatty()
            if args.command == 'tui':
                tui(mongo)
            else:
                record = manage(mongo, args)
                if args.command == 'status':
                    record['shell'] = mongo.shell
                emit(record)
        elif tool == 'backup':
            emit(backup(mongo, args))
        else:
            login(mongo, args.code)
        return 0
    except (ToolError, OSError) as error:
        message(str(error), 'ERROR')
        if not interactive:
            record = {'ok': False, 'error': str(error)}
            if getattr(error, 'details', None):
                record['result'] = error.details
            emit(record)
        return 1
    except KeyboardInterrupt:
        message('Interrupted.', 'WARN')
        return 130


if __name__ == '__main__':
    sys.exit(main())
