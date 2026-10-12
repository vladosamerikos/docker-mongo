#!/usr/bin/env python3
"""Configure and start the MongoDB Docker boilerplate."""
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import time

from mongo_cli import Config, Mongo, ToolError, boolean, load_dotenv, message

DEFAULTS = {
    'MONGO_VERSION': '6', 'MONGO_HOSTNAME': 'mongo6', 'HOST_MONGO_PORT': '27006',
    'MONGO_PORT_MODE': 'host', 'STORE_CREDENTIALS': 'false', 'MONGO_LAN_HOST': '',
    'MONGO_NETWORK_NAME': 'mongo_network',
    'MONGO_INITDB_ROOT_USERNAME': 'root', 'MONGO_INITDB_ROOT_PASSWORD': '',
    'CONTAINER_NAME': 'mongo6', 'COMPOSE_PROJECT_NAME': 'mongo6',
    'MONGO_LIMIT_CPUS': '2', 'MONGO_LIMIT_MEMORY': '4G',
    'MONGO_RESERVATION_CPUS': '0.5', 'MONGO_RESERVATION_MEMORY': '200M',
}
SERVICES = {'mongo', 'mongo-none', 'mongo-host', 'mongo-lan'}


def major_version(tag):
    match = re.match(r'^([3-9])(?:[.\-]|$)', tag)
    if not match:
        raise ToolError('Use a MongoDB image tag from major version 3 through 9; latest is not supported.')
    return match[1]


def major_choice(value):
    if not re.fullmatch('[3-9]', value):
        raise ToolError('Enter a major version between 3 and 9.')
    return value


def ask(label, default, validate=lambda value: value):
    while True:
        try:
            value = input(f'{label} [{default}]: ').strip() or str(default)
        except EOFError as error:
            raise ToolError('Input closed. Configuration has not been applied.') from error
        try:
            return validate(value)
        except (ToolError, ValueError) as error:
            message(str(error), 'ERROR')


def name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', value):
        raise ToolError('Use 1-63 letters, digits, dots, underscores or hyphens, starting with a letter or digit.')
    return value


def hostname(value):
    if len(value) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                   for label in value.split('.')):
        raise ToolError('Enter a valid hostname or IPv4 address (no URL or port).')
    return value


def cpu(value, maximum=None):
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0 or number.as_tuple().exponent < -9:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ToolError('Enter a positive CPU count, for example 0.5 or 2.')
    if maximum is not None and number > Decimal(maximum):
        raise ToolError(f'The CPU limit cannot exceed the Docker host CPU count ({maximum}).')
    return format(number, 'f')


def memory_bytes(value):
    match = re.fullmatch(r'([1-9][0-9]*)([bkmg]|[kmg]b)?', value.lower())
    if not match:
        raise ToolError('Enter a positive memory size, for example 200M, 4G or 4096MB.')
    amount, unit = match.groups()
    return int(amount) * 1024 ** {'b': 0, 'k': 1, 'm': 2, 'g': 3}.get((unit or 'b')[0], 0)


def memory(value):
    if memory_bytes(value) < 6 * 1024 * 1024:
        raise ToolError('Docker requires at least 6M of memory.')
    return value.upper()


def reservation(value, limit, validator, size):
    value = validator(value)
    if size(value) > size(limit):
        raise ToolError('The reservation cannot exceed the limit.')
    return value


def capped_default(value, limit, size):
    try:
        return value if size(value) <= size(limit) else limit
    except (ToolError, InvalidOperation, ValueError):
        return limit


def atomic_write(path, content):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ToolError(f'{path.name} must be an ordinary file, not a symlink.')
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def dotenv_literal(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def render_env(original, values, preserved_keys=()):
    """Keep custom settings/comments and untouched credential literals."""
    updates = dict(values)
    updates['COMPOSE_PROFILES'] = '${MONGO_PORT_MODE}'
    updates['COMPOSE_FILE'] = 'docker-compose.yml'
    for key in preserved_keys:
        updates.pop(key, None)
    assignments = {key: f'{key}=' + (value if key == 'COMPOSE_PROFILES' else dotenv_literal(value))
                   for key, value in updates.items()}
    lines = []
    for line in original.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match and match[1] in updates:
            key = match[1]
            if key in assignments:
                lines.append(assignments.pop(key))
        else:
            lines.append(line)
    lines.extend(assignments.values())
    return '\n'.join(lines) + '\n'


class Installer:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.env_path = self.root / '.env'
        self.state_path = self.root / '.setup-state.json'
        if self.env_path.is_symlink() or self.state_path.is_symlink():
            raise ToolError('.env and .setup-state.json cannot be symlinks.')
        self.original = self.env_path.read_text() if self.env_path.exists() else ''
        self.old_state = self.state_path.read_text() if self.state_path.exists() else None
        self.values = dict(DEFAULTS)
        file_keys = set(re.findall(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', self.original, re.MULTILINE))
        file_environ = {key: value for key, value in os.environ.items() if key not in file_keys}
        self.loaded = load_dotenv(self.env_path, file_environ) if self.original else {}
        self.values.update(self.loaded)
        self.state = json.loads(self.old_state) if self.old_state else {}
        self.owned = []
        self.secrets = [self.values.get('MONGO_INITDB_ROOT_PASSWORD', '')]
        self.docker_env = dict(os.environ)
        # Explicit .env and project selection must win over the invoking shell.
        for key in set(self.values) | {key for key in os.environ if key.startswith('COMPOSE_')} | {'MONGO_ENV_FILE'}:
            self.docker_env.pop(key, None)

    def run(self, *args, check=True, timeout=180):
        try:
            result = subprocess.run(args, cwd=self.root, env=self.docker_env,
                                    text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ToolError('Command timed out. Check Docker/network access and rerun setup to retry.') from error
        except OSError as error:
            raise ToolError(f'Cannot execute {args[0]}; check its installation and retry setup.') from error
        if check and result.returncode:
            diagnostic = (result.stderr or result.stdout).strip()[-2500:]
            for secret in self.secrets:
                if secret:
                    diagnostic = diagnostic.replace(secret, '[redacted]')
            raise ToolError(diagnostic or f'{args[0]} failed with exit code {result.returncode}.')
        return result

    def compose(self, *args, check=True, timeout=180):
        return self.run('docker', 'compose', '--project-directory', str(self.root),
                        '--env-file', str(self.env_path), '-f', str(self.root / 'docker-compose.yml'),
                        '-p', self.values['COMPOSE_PROJECT_NAME'], '--profile', self.values['MONGO_PORT_MODE'],
                        *args, check=check, timeout=timeout)

    def inspect(self, check_name=True):
        ids = self.run('docker', 'ps', '-aq').stdout.split()
        self.containers = []
        self.owned = []
        if ids:
            probe = self.run('docker', 'inspect', *ids, check=False)
            # Unrelated containers may disappear between listing and inspection.
            if probe.returncode and (not probe.stderr.strip() or
                    any('no such object' not in line.lower() for line in probe.stderr.splitlines() if line.strip())):
                raise ToolError('Cannot inspect Docker containers. Check Docker access and retry setup.')
            self.containers = json.loads(probe.stdout)
        expected_data = self.root / 'data'
        for container in self.containers:
            labels = container['Config'].get('Labels') or {}
            matching_data = any(mount.get('Destination') == '/data/db' and
                                Path(mount.get('Source', '/')).resolve() == expected_data
                                for mount in container.get('Mounts', []))
            matching_project = labels.get('com.docker.compose.project') == self.values['COMPOSE_PROJECT_NAME']
            matching_directory = labels.get('com.docker.compose.project.working_dir') == str(self.root)
            if matching_data and (matching_project or matching_directory) and labels.get('com.docker.compose.service') in SERVICES:
                tag = container['Config']['Image'].rsplit(':', 1)[-1]
                if tag != self.values['MONGO_VERSION']:
                    raise ToolError(f'Existing container uses MongoDB {tag}. Restore MONGO_VERSION={tag}; setup does not migrate versions.')
                self.owned.append(container)
            elif check_name and container['Name'].lstrip('/') == self.values['CONTAINER_NAME']:
                raise ToolError('The configured container name belongs to another installation. It will not be replaced. Change CONTAINER_NAME in .env first.')
        if len(self.owned) > 1:
            raise ToolError('Multiple Mongo containers use this data directory. Stop the extra containers before running setup.')

    def port(self, value):
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ToolError('Enter a port from 1 to 65535.')
        address = '127.0.0.1' if self.values['MONGO_PORT_MODE'] == 'host' else '0.0.0.0'
        own_ids = {container['Id'] for container in self.owned}
        own_binding = False
        owned_addresses = set()
        for container in self.containers:
            if not container['State'].get('Running'):
                continue
            for bindings in (container.get('NetworkSettings', {}).get('Ports') or {}).values():
                for binding in bindings or []:
                    host = binding.get('HostIp', '0.0.0.0')
                    overlap = address == '0.0.0.0' or host in ('0.0.0.0', '::', address)
                    if binding.get('HostPort') == str(int(value)) and overlap:
                        if container['Id'] not in own_ids:
                            raise ToolError(f'Port {value} is already published by another Docker container. Choose another port.')
                        own_binding = True
                        owned_addresses.add(host)
        if own_binding and address == '0.0.0.0' and shutil.which('ss'):
            listeners = self.run('ss', '-H', '-ltn', 'sport', '=', f':{int(value)}', check=False)
            for line in listeners.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 4:
                    host = fields[3].rsplit(':', 1)[0].strip('[]')
                    if host not in owned_addresses and host not in ('*', '::'):
                        raise ToolError(f'Port {value} has another host listener on {host}. Choose another port.')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, int(value)))
        except OSError:
            if not own_binding:
                raise ToolError(f'Port {value} is unavailable on {address}. Choose another port.')
        return str(int(value))

    def configure(self):
        has_data = (self.root / 'data').exists() and any((self.root / 'data').iterdir())
        if has_data and not self.loaded and not self.state:
            raise ToolError('Existing database files were found without .env or setup state. Restore the original .env; setup cannot safely guess their MongoDB version.')
        if self.state and not self.loaded:
            raise ToolError('The .env from this installation is missing. Restore it before running setup; its container identity and credentials must be preserved.')
        initial = not self.loaded and not self.state
        if initial:
            version = major_choice(os.environ.get('MONGO_SETUP_VERSION') or ask('MongoDB major version (3-9)', '6', major_choice))
            # The bootstrap only supplies a major version. Pin the Linux tag for 9.
            self.values['MONGO_VERSION'] = '9.0-noble' if version == '9' else version
            for key in ('CONTAINER_NAME', 'COMPOSE_PROJECT_NAME', 'MONGO_HOSTNAME'):
                self.values[key] = f'mongo{version}'
            self.values['HOST_MONGO_PORT'] = f'2700{version}'
        major_version(self.values['MONGO_VERSION'])
        if self.state and self.state['version'] != self.values['MONGO_VERSION']:
            raise ToolError(f"This installation is pinned to MongoDB {self.state['version']}. Version migration is not supported.")
        self.inspect(check_name=False)
        initialized = bool(self.owned or has_data or self.state.get('initialized'))
        if initialized and self.state and self.state.get('root_username') != self.values['MONGO_INITDB_ROOT_USERNAME']:
            raise ToolError('Restore the administrator username from .setup-state.json; editing .env cannot rename an initialized MongoDB user.')
        message(f"Configuring {self.root} (MongoDB {self.values['MONGO_VERSION']}).")
        if initialized:
            message('Existing data: version and administrator account are preserved.')
        else:
            self.values['MONGO_INITDB_ROOT_USERNAME'] = ask('Administrator username', self.values['MONGO_INITDB_ROOT_USERNAME'], name)
        if not self.values['MONGO_INITDB_ROOT_PASSWORD']:
            if initialized:
                import getpass
                while True:
                    password = getpass.getpass('Existing administrator password (will be stored in .env): ')
                    if password and not any(character in password for character in '\n\r\x00'):
                        break
                    message('Enter the existing password without control characters.', 'ERROR')
            else:
                password = self.run('openssl', 'rand', '-hex', '32').stdout.strip()
                message('Generated a 256-bit administrator password using OpenSSL. It will be saved in .env.')
            self.values['MONGO_INITDB_ROOT_PASSWORD'] = password
            self.secrets.append(password)
        def available_hostname(value):
            value = hostname(value)
            if initial:
                owned_ids = {container['Id'] for container in self.owned}
                if any(container['Name'].lstrip('/') == value and container['Id'] not in owned_ids
                       for container in self.containers):
                    raise ToolError('That container name is already used by another installation. Choose another hostname.')
            return value
        self.values['MONGO_HOSTNAME'] = ask('Docker hostname', self.values['MONGO_HOSTNAME'], available_hostname)
        if initial:
            self.values['CONTAINER_NAME'] = self.values['MONGO_HOSTNAME']
            self.values['COMPOSE_PROJECT_NAME'] = re.sub(r'[^a-z0-9_-]', '-', self.values['MONGO_HOSTNAME'].lower())
            self.values['MONGO_NETWORK_NAME'] = self.values['COMPOSE_PROJECT_NAME'] + '_network'
        self.inspect()
        def mode(value):
            value = {'127': 'host', '127.0.0.1': 'host', '000': 'lan', '0.0.0.0': 'lan'}.get(value, value)
            if value not in ('host', 'lan', 'none'):
                raise ToolError('Choose host (127.0.0.1), lan (0.0.0.0) or none (Docker network only).')
            return value
        self.values['MONGO_PORT_MODE'] = ask('Network mode: host / lan / none', self.values['MONGO_PORT_MODE'], mode)
        if self.values['MONGO_PORT_MODE'] != 'none':
            self.values['HOST_MONGO_PORT'] = ask('Published host port', self.values['HOST_MONGO_PORT'], self.port)
        if self.values['MONGO_PORT_MODE'] == 'lan':
            self.values['MONGO_LAN_HOST'] = ask('LAN IP/hostname for connection strings (optional)', self.values['MONGO_LAN_HOST'],
                                                lambda value: hostname(value) if value else '')
        self.values['STORE_CREDENTIALS'] = ask('Store generated user credentials in .credentials? true / false',
                                               self.values['STORE_CREDENTIALS'], lambda value: str(boolean(value)).lower())
        available_cpus = self.run('docker', 'info', '--format', '{{.NCPU}}', check=False).stdout.strip()
        available_cpus = available_cpus if available_cpus.isdigit() and int(available_cpus) > 0 else None
        cpu_default = capped_default(self.values['MONGO_LIMIT_CPUS'], available_cpus, Decimal) if available_cpus else self.values['MONGO_LIMIT_CPUS']
        self.values['MONGO_LIMIT_CPUS'] = ask('CPU limit', cpu_default, lambda value: cpu(value, available_cpus))
        self.values['MONGO_LIMIT_MEMORY'] = ask('Memory limit', self.values['MONGO_LIMIT_MEMORY'], memory)
        cpu_reservation = capped_default(self.values['MONGO_RESERVATION_CPUS'], self.values['MONGO_LIMIT_CPUS'], Decimal)
        memory_reservation = capped_default(self.values['MONGO_RESERVATION_MEMORY'], self.values['MONGO_LIMIT_MEMORY'], memory_bytes)
        self.values['MONGO_RESERVATION_CPUS'] = ask('Reserved CPUs', cpu_reservation,
            lambda value: reservation(value, self.values['MONGO_LIMIT_CPUS'], cpu, Decimal))
        self.values['MONGO_RESERVATION_MEMORY'] = ask('Reserved memory', memory_reservation,
            lambda value: reservation(value, self.values['MONGO_LIMIT_MEMORY'], memory, memory_bytes))
        # Authenticate before recreating an initialized container.
        if self.owned and self.owned[0]['State'].get('Running'):
            with tempfile.NamedTemporaryFile(mode='w', prefix='.setup-auth.', dir=self.root) as handle:
                handle.write(render_env(self.original, self.values))
                handle.flush()
                Mongo(Config(handle.name, self.docker_env)).request({'action': 'status'})

    def state_content(self, initialized):
        return json.dumps({'version': self.values['MONGO_VERSION'],
                           'root_username': self.values['MONGO_INITDB_ROOT_USERNAME'],
                           'initialized': initialized}, indent=2) + '\n'

    def restore_configuration(self):
        if self.original:
            atomic_write(self.env_path, self.original)
        else:
            self.env_path.unlink(missing_ok=True)
        if self.old_state is not None:
            atomic_write(self.state_path, self.old_state)
        else:
            self.state_path.unlink(missing_ok=True)

    def deploy(self):
        # Compose sees a real .env for env_file resolution. Restore it if validation fails.
        preserve = [key for key in ('MONGO_INITDB_ROOT_USERNAME', 'MONGO_INITDB_ROOT_PASSWORD')
                    if self.loaded.get(key) and self.loaded[key] == self.values[key]]
        atomic_write(self.env_path, render_env(self.original, self.values, preserve))
        try:
            self.compose('config', '--quiet')
            # Pull while the old instance is still running. A download failure never stops it.
            message('Checking the Docker image before recreating the container...')
            self.compose('pull', f"mongo-{self.values['MONGO_PORT_MODE']}", timeout=900)
        except (ToolError, KeyboardInterrupt):
            self.restore_configuration()
            raise
        atomic_write(self.state_path, self.state_content(bool(self.owned or self.state.get('initialized'))))
        old = self.owned[0] if self.owned else None
        desired_service = f"mongo-{self.values['MONGO_PORT_MODE']}"
        different_service = old and ((old['Config'].get('Labels') or {}).get('com.docker.compose.service') != desired_service)
        try:
            if different_service:
                # Only this installation's verified container. Bind-mounted data stays intact.
                message('Replacing the container to apply the selected network profile...')
                self.run('docker', 'stop', '--time', '30', old['Id'])
                self.run('docker', 'rm', old['Id'])
            message('Starting MongoDB...')
            self.compose('up', '-d', '--force-recreate', desired_service)
            self.wait_ready()
        except (ToolError, OSError, ValueError, KeyboardInterrupt) as error:
            if old:
                message('Deployment failed. Restoring the previous configuration and container.', 'WARN')
                # Remove only a new container owned by this project / data path.
                try:
                    self.inspect()
                    for container in self.owned:
                        self.run('docker', 'rm', '-f', container['Id'], check=False)
                except ToolError:
                    message('Could not inspect/remove the replacement container. Restoring .env before retrying the old service.', 'WARN')
                self.restore_configuration()
                self.values.update(self.loaded)
                # The old profile may come from a container whose .env was previously edited.
                old_service = (old['Config'].get('Labels') or {}).get('com.docker.compose.service')
                if old_service in ('mongo-none', 'mongo-host', 'mongo-lan'):
                    self.values['MONGO_PORT_MODE'] = old_service.split('-', 1)[1]
                else:
                    old_service = f"mongo-{self.values['MONGO_PORT_MODE']}"
                try:
                    restored = self.compose('up', '-d', old_service, check=False)
                    restarted = restored.returncode == 0
                except ToolError:
                    restarted = False
                if not restarted:
                    message('Automatic restart failed. Previous .env was restored; inspect Docker and rerun setup.', 'ERROR')
            else:
                message('Configuration and generated password are saved. Fix Docker/server startup and rerun setup.', 'WARN')
            raise error
        atomic_write(self.state_path, self.state_content(True))
        config = Config(self.env_path, self.docker_env)
        message(f"MongoDB is ready. Container: {config.container}; network mode: {config.mode}.", 'OK')
        message(f'Configuration: {self.env_path} (permissions 600). Administrator password is stored there.')
        message(f'Next: {self.root / "scripts/manage.sh"} status')

    def wait_ready(self):
        deadline = time.monotonic() + 120
        last_error = None
        config = Config(self.env_path, self.docker_env)
        while time.monotonic() < deadline:
            try:
                Mongo(config).request({'action': 'status'})
                return
            except ToolError as error:
                last_error = error
            status = self.run('docker', 'inspect', '--format', '{{.State.Status}} {{.RestartCount}}',
                              config.container, check=False, timeout=10)
            if status.returncode == 0 and (status.stdout.startswith(('exited', 'dead')) or
                                           int(status.stdout.split()[-1]) >= 2):
                break
            time.sleep(2)
        raise ToolError(f'MongoDB did not become ready: {last_error}. Check docker logs {config.container}.')


def main():
    try:
        if len(sys.argv) != 2 or not sys.stdin.isatty():
            raise ToolError('Run setup.sh from an interactive terminal.')
        root = Path(sys.argv[1]).resolve()
        # Lock the installer, not the database. Concurrent setup runs must not race .env.
        fd = os.open(root / '.setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolError('Another setup is already running in this directory.')
            installer = Installer(root)
            installer.configure()
            installer.deploy()
        return 0
    except KeyboardInterrupt:
        message('Setup interrupted. Rerun it to continue.', 'WARN')
        return 130
    except (ToolError, OSError, ValueError, KeyError, EOFError) as error:
        message(str(error) or 'Input closed. Rerun setup to continue.', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
