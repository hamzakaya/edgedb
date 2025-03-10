# Copyright (C) 2016-present MagicStack Inc. and the EdgeDB authors.
# Copyright (C) 2016-present the asyncpg authors and contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""PostgreSQL cluster management."""

from __future__ import annotations
from typing import *

import asyncio
import enum
import functools
import locale
import logging
import os
import os.path
import pathlib
import re
import shlex
import shutil
import textwrap
import time
import urllib.parse

import asyncpg

from edb import buildmeta
from edb.common import supervisor
from edb.common import uuidgen

from edb.server import defines
from edb.server.ha import base as ha_base
from edb.pgsql import common as pgcommon

from . import pgconnparams


logger = logging.getLogger('edb.pgcluster')
pg_dump_logger = logging.getLogger('pg_dump')
pg_ctl_logger = logging.getLogger('pg_ctl')
pg_config_logger = logging.getLogger('pg_config')
initdb_logger = logging.getLogger('initdb')
postgres_logger = logging.getLogger('postgres')

get_database_backend_name = pgcommon.get_database_backend_name
get_role_backend_name = pgcommon.get_role_backend_name


def _is_c_utf8_locale_present() -> bool:
    try:
        locale.setlocale(locale.LC_CTYPE, 'C.UTF-8')
    except Exception:
        return False
    else:
        # We specifically don't use locale.getlocale(), because
        # it can lie and return a non-existent locale due to PEP 538.
        locale.setlocale(locale.LC_CTYPE, '')
        return True


class ClusterError(Exception):
    pass


class PostgresPidFileNotReadyError(Exception):
    """Raised on an attempt to read non-existent or bad Postgres PID file"""


class BackendCapabilities(enum.IntFlag):

    NONE = 0
    #: Whether CREATE ROLE .. SUPERUSER is allowed
    SUPERUSER_ACCESS = 1 << 0
    #: Whether reading PostgreSQL configuration files
    #: via pg_file_settings is allowed
    CONFIGFILE_ACCESS = 1 << 1
    #: Whether the PostgreSQL server supports the C.UTF-8 locale
    C_UTF8_LOCALE = 1 << 2


ALL_BACKEND_CAPABILITIES = (
    BackendCapabilities.SUPERUSER_ACCESS
    | BackendCapabilities.CONFIGFILE_ACCESS
    | BackendCapabilities.C_UTF8_LOCALE
)


class BackendInstanceParams(NamedTuple):

    capabilities: BackendCapabilities
    tenant_id: str
    base_superuser: Optional[str] = None
    max_connections: int = 500
    reserved_connections: int = 0


class BackendRuntimeParams(NamedTuple):

    instance_params: BackendInstanceParams
    session_authorization_role: Optional[str] = None


@functools.lru_cache
def get_default_runtime_params(**instance_params: Any) -> BackendRuntimeParams:
    capabilities = ALL_BACKEND_CAPABILITIES
    if not _is_c_utf8_locale_present():
        capabilities &= ~BackendCapabilities.C_UTF8_LOCALE
    instance_params.setdefault('capabilities', capabilities)
    if 'tenant_id' not in instance_params:
        instance_params = dict(
            tenant_id=buildmeta.get_default_tenant_id(),
            **instance_params,
        )

    return BackendRuntimeParams(
        instance_params=BackendInstanceParams(**instance_params),
    )


class BaseCluster:

    def __init__(
        self,
        *,
        instance_params: Optional[BackendInstanceParams] = None,
    ) -> None:
        self._connection_addr: Optional[Tuple[str, int]] = None
        self._connection_params: Optional[
            pgconnparams.ConnectionParameters
        ] = None
        self._default_session_auth: Optional[str] = None
        self._pg_config_data: Dict[str, str] = {}
        self._pg_bin_dir: Optional[pathlib.Path] = None
        if instance_params is None:
            self._instance_params = (
                get_default_runtime_params().instance_params)
        else:
            self._instance_params = instance_params

    def get_db_name(self, db_name: str) -> str:
        return get_database_backend_name(
            db_name,
            tenant_id=self._instance_params.tenant_id,
        )

    def get_role_name(self, role_name: str) -> str:
        return get_database_backend_name(
            role_name,
            tenant_id=self._instance_params.tenant_id,
        )

    async def start(
        self,
        wait: int = 60,
        *,
        server_settings: Optional[Mapping[str, str]] = None,
        **opts: Any,
    ) -> None:
        raise NotImplementedError

    async def stop(self, wait: int = 60) -> None:
        raise NotImplementedError

    def destroy(self) -> None:
        raise NotImplementedError

    async def connect(self, **kwargs: Any) -> asyncpg.Connection:
        conn_info = self.get_connection_spec()
        conn_info.update(kwargs)
        if 'sslmode' in conn_info:
            conn_info['ssl'] = conn_info.pop('sslmode').name
        conn = await asyncpg.connect(**conn_info)

        if (not kwargs.get('user')
                and self._default_session_auth
                and conn_info.get('user') != self._default_session_auth):
            # No explicit user given, and the default
            # SESSION AUTHORIZATION is different from the user
            # used to connect.
            await conn.execute(
                f'SET ROLE {pgcommon.quote_ident(self._default_session_auth)}'
            )

        return conn

    async def start_watching(
        self, cluster_protocol: Optional[ha_base.ClusterProtocol] = None
    ) -> None:
        pass

    def stop_watching(self) -> None:
        pass

    def get_runtime_params(self) -> BackendRuntimeParams:
        params = self.get_connection_params()
        login_role: Optional[str] = params.user
        sup_role = self.get_role_name(defines.EDGEDB_SUPERUSER)
        return BackendRuntimeParams(
            instance_params=self._instance_params,
            session_authorization_role=(
                None if login_role == sup_role else login_role
            ),
        )

    def get_connection_addr(self) -> Optional[Tuple[str, int]]:
        return self._get_connection_addr()

    def set_default_session_authorization(self, rolename: str) -> None:
        self._default_session_auth = rolename

    def set_connection_params(
        self,
        params: pgconnparams.ConnectionParameters,
    ) -> None:
        self._connection_params = params

    def get_connection_params(
        self,
    ) -> pgconnparams.ConnectionParameters:
        assert self._connection_params is not None
        return self._connection_params

    def get_connection_spec(self) -> Dict[str, Any]:
        conn_dict: Dict[str, Any] = {}
        addr = self.get_connection_addr()
        assert addr is not None
        conn_dict['host'] = addr[0]
        conn_dict['port'] = addr[1]
        params = self.get_connection_params()
        for k in (
            'user',
            'password',
            'database',
            'ssl',
            'sslmode',
            'server_settings',
        ):
            v = getattr(params, k)
            if v is not None:
                conn_dict[k] = v

        cluster_settings = conn_dict.get('server_settings', {})

        edgedb_settings = {
            'client_encoding': 'utf-8',
            'search_path': 'edgedb',
            'timezone': 'UTC',
            'intervalstyle': 'iso_8601',
            'jit': 'off',
        }

        conn_dict['server_settings'] = {**cluster_settings, **edgedb_settings}

        return conn_dict

    def _get_connection_addr(self) -> Optional[Tuple[str, int]]:
        return self._connection_addr

    def is_managed(self) -> bool:
        raise NotImplementedError

    async def get_status(self) -> str:
        raise NotImplementedError

    async def dump_database(
        self,
        dbname: str,
        *,
        exclude_schemas: Iterable[str] = (),
        dump_object_owners: bool = True,
    ) -> bytes:
        status = await self.get_status()
        if status != 'running':
            raise ClusterError('cannot dump: cluster is not running')

        if self._pg_bin_dir is None:
            await self.lookup_postgres()
        pg_dump = self._find_pg_binary('pg_dump')
        conn_spec = self.get_connection_spec()

        args = [
            pg_dump,
            '--inserts',
            f'--dbname={dbname}',
            f'--host={conn_spec["host"]}',
            f'--port={conn_spec["port"]}',
            f'--username={conn_spec["user"]}',
        ]

        if not dump_object_owners:
            args.append('--no-owner')

        env = os.environ.copy()
        if conn_spec.get("password"):
            env['PGPASSWORD'] = conn_spec["password"]

        if exclude_schemas:
            for exclude_schema in exclude_schemas:
                args.append(f'--exclude-schema={exclude_schema}')

        stdout_lines, _, _ = await _run_logged_subprocess(
            args,
            logger=pg_dump_logger,
            log_stdout=False,
            env=env,
        )
        return b'\n'.join(stdout_lines)

    def _find_pg_binary(self, binary: str) -> str:
        assert self._pg_bin_dir is not None
        bpath = self._pg_bin_dir / binary
        if not bpath.is_file():
            raise ClusterError(
                'could not find {} executable: '.format(binary) +
                '{!r} does not exist or is not a file'.format(bpath))

        return str(bpath)

    def _subprocess_error(
        self,
        name: str,
        exitcode: int,
        stderr: Optional[bytes],
    ) -> ClusterError:
        if stderr:
            return ClusterError(
                f'{name} exited with status {exitcode}:\n'
                + textwrap.indent(stderr.decode(), ' ' * 4),
            )
        else:
            return ClusterError(
                f'{name} exited with status {exitcode}',
            )

    async def lookup_postgres(self) -> None:
        self._pg_bin_dir = await get_pg_bin_dir()


class Cluster(BaseCluster):
    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        runstate_dir: Optional[pathlib.Path] = None,
        instance_params: Optional[BackendInstanceParams] = None,
        log_level: str = 'i',
    ):
        super().__init__(instance_params=instance_params)
        self._data_dir = data_dir
        self._runstate_dir = (
            runstate_dir if runstate_dir is not None else data_dir)
        self._daemon_pid: Optional[int] = None
        self._daemon_process: Optional[asyncio.subprocess.Process] = None
        self._daemon_supervisor: Optional[supervisor.Supervisor] = None
        self._log_level = log_level

    def is_managed(self) -> bool:
        return True

    def get_data_dir(self) -> pathlib.Path:
        return self._data_dir

    async def get_status(self) -> str:
        stdout_lines, stderr_lines, exit_code = (
            await _run_logged_text_subprocess(
                [self._pg_ctl, 'status', '-D', str(self._data_dir)],
                logger=pg_ctl_logger,
                check=False,
            )
        )

        if (
            exit_code == 4
            or not os.path.exists(self._data_dir)
            or not os.listdir(self._data_dir)
        ):
            return 'not-initialized'
        elif exit_code == 3:
            return 'stopped'
        elif exit_code == 0:
            output = '\n'.join(stdout_lines)
            r = re.match(r'.*PID\s?:\s+(\d+).*', output)
            if not r:
                raise ClusterError(
                    f'could not parse pg_ctl status output: {output}')
            self._daemon_pid = int(r.group(1))
            if self._connection_addr is None:
                self._connection_addr = self._connection_addr_from_pidfile()
            return 'running'
        else:
            stderr_text = '\n'.join(stderr_lines)
            raise ClusterError(
                f'`pg_ctl status` exited with status {exit_code}:\n'
                + textwrap.indent(stderr_text, ' ' * 4),
            )

    async def ensure_initialized(self, **settings: Any) -> bool:
        cluster_status = await self.get_status()

        if cluster_status == 'not-initialized':
            logger.info(
                'Initializing database cluster in %s', self._data_dir)

            instance_params = self.get_runtime_params().instance_params
            capabilities = instance_params.capabilities
            have_c_utf8 = (
                capabilities & BackendCapabilities.C_UTF8_LOCALE)
            await self.init(
                username='postgres',
                locale='C.UTF-8' if have_c_utf8 else 'en_US.UTF-8',
                lc_collate='C',
                encoding='UTF8',
            )
            self.reset_hba()
            self.add_hba_entry(
                type='local',
                database='all',
                user='postgres',
                auth_method='trust'
            )
            return True
        else:
            return False

    async def init(self, **settings: str) -> None:
        """Initialize cluster."""
        if await self.get_status() != 'not-initialized':
            raise ClusterError(
                'cluster in {!r} has already been initialized'.format(
                    self._data_dir))

        if settings:
            settings_args = ['--{}={}'.format(k.replace('_', '-'), v)
                             for k, v in settings.items()]
            extra_args = ['-o'] + [' '.join(settings_args)]
        else:
            extra_args = []

        await _run_logged_subprocess(
            [self._pg_ctl, 'init', '-D', str(self._data_dir)] + extra_args,
            logger=initdb_logger,
        )

    async def start(
        self,
        wait: int = 60,
        *,
        server_settings: Optional[Mapping[str, str]] = None,
        **opts: str,
    ) -> None:
        """Start the cluster."""
        status = await self.get_status()
        if status == 'running':
            return
        elif status == 'not-initialized':
            raise ClusterError(
                'cluster in {!r} has not been initialized'.format(
                    self._data_dir))

        extra_args = ['--{}={}'.format(k, v) for k, v in opts.items()]

        start_settings = {
            'listen_addresses': '',  # we use Unix sockets
            'unix_socket_permissions': '0700',
            'unix_socket_directories': str(self._runstate_dir),
            # here we are not setting superuser_reserved_connections because
            # we're using superuser only now (so all connections available),
            # and we don't support reserving connections for now
            'max_connections': str(self._instance_params.max_connections),
            # From Postgres docs:
            #
            #   You might need to raise this value if you have queries that
            #   touch many different tables in a single transaction, e.g.,
            #   query of a parent table with many children.
            #
            # EdgeDB queries might touch _lots_ of tables, especially in deep
            # inheritance hierarchies.  This is especially important in low
            # `max_connections` scenarios.
            'max_locks_per_transaction': 256,
        }

        if os.getenv('EDGEDB_DEBUG_PGSERVER'):
            start_settings['log_min_messages'] = 'info'
            start_settings['log_statement'] = 'all'
        else:
            log_level_map = {
                'd': 'INFO',
                'i': 'NOTICE',
                'w': 'WARNING',
                'e': 'ERROR',
                's': 'PANIC',
            }
            start_settings['log_min_messages'] = log_level_map[self._log_level]
            start_settings['log_statement'] = 'none'
            start_settings['log_line_prefix'] = ''

        if server_settings:
            start_settings.update(server_settings)

        ssl_key = start_settings.get('ssl_key_file')
        if ssl_key:
            # Make sure server certificate key file has correct permissions.
            keyfile = os.path.join(self._data_dir, 'srvkey.pem')
            assert isinstance(ssl_key, str)
            shutil.copy(ssl_key, keyfile)
            os.chmod(keyfile, 0o600)
            start_settings['ssl_key_file'] = keyfile

        for k, v in start_settings.items():
            extra_args.extend(['-c', '{}={}'.format(k, v)])

        self._daemon_process, *loggers = await _start_logged_subprocess(
            [self._postgres, '-D', str(self._data_dir), *extra_args],
            capture_stdout=False,
            capture_stderr=False,
            logger=postgres_logger,
            log_processor=postgres_log_processor,
        )
        self._daemon_pid = self._daemon_process.pid

        sup = await supervisor.Supervisor.create(name="postgres loggers")
        for logger_coro in loggers:
            sup.create_task(logger_coro)
        self._daemon_supervisor = sup

        await self._test_connection(timeout=wait)

    async def reload(self) -> None:
        """Reload server configuration."""
        status = await self.get_status()
        if status != 'running':
            raise ClusterError('cannot reload: cluster is not running')

        await _run_logged_subprocess(
            [self._pg_ctl, 'reload', '-D', str(self._data_dir)],
            logger=pg_ctl_logger,
        )

    async def stop(self, wait: int = 60) -> None:
        await _run_logged_subprocess(
            [
                self._pg_ctl,
                'stop', '-D', str(self._data_dir),
                '-t', str(wait), '-m', 'fast'
            ],
            logger=pg_ctl_logger,
        )

        if (
            self._daemon_process is not None and
            self._daemon_process.returncode is None
        ):
            self._daemon_process.terminate()
            await asyncio.wait_for(self._daemon_process.wait(), timeout=wait)

        if self._daemon_supervisor is not None:
            await self._daemon_supervisor.cancel()
            self._daemon_supervisor = None

    def destroy(self) -> None:
        shutil.rmtree(self._data_dir)

    def reset_hba(self) -> None:
        """Remove all records from pg_hba.conf."""
        pg_hba = os.path.join(self._data_dir, 'pg_hba.conf')

        try:
            with open(pg_hba, 'w'):
                pass
        except IOError as e:
            raise ClusterError(
                'cannot modify HBA records: {}'.format(e)) from e

    def add_hba_entry(
        self,
        *,
        type: str = 'host',
        database: str,
        user: str,
        address: Optional[str] = None,
        auth_method: str,
        auth_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Add a record to pg_hba.conf."""
        if type not in {'local', 'host', 'hostssl', 'hostnossl'}:
            raise ValueError('invalid HBA record type: {!r}'.format(type))

        pg_hba = os.path.join(self._data_dir, 'pg_hba.conf')

        record = '{} {} {}'.format(type, database, user)

        if type != 'local':
            if address is None:
                raise ValueError(
                    '{!r} entry requires a valid address'.format(type))
            else:
                record += ' {}'.format(address)

        record += ' {}'.format(auth_method)

        if auth_options is not None:
            record += ' ' + ' '.join(
                '{}={}'.format(k, v) for k, v in auth_options.items())

        try:
            with open(pg_hba, 'a') as f:
                print(record, file=f)
        except IOError as e:
            raise ClusterError(
                'cannot modify HBA records: {}'.format(e)) from e

    async def trust_local_connections(self) -> None:
        self.reset_hba()

        self.add_hba_entry(type='local', database='all',
                           user='all', auth_method='trust')
        self.add_hba_entry(type='host', address='127.0.0.1/32',
                           database='all', user='all',
                           auth_method='trust')
        self.add_hba_entry(type='host', address='::1/128',
                           database='all', user='all',
                           auth_method='trust')
        status = await self.get_status()
        if status == 'running':
            await self.reload()

    async def lookup_postgres(self) -> None:
        await super().lookup_postgres()
        self._pg_ctl = self._find_pg_binary('pg_ctl')
        self._postgres = self._find_pg_binary('postgres')

    def _get_connection_addr(self) -> Tuple[str, int]:
        if self._connection_addr is None:
            self._connection_addr = self._connection_addr_from_pidfile()

        return self._connection_addr

    def _connection_addr_from_pidfile(self) -> Tuple[str, int]:
        pidfile = os.path.join(self._data_dir, 'postmaster.pid')

        try:
            with open(pidfile, 'rt') as f:
                piddata = f.read()
        except FileNotFoundError:
            raise PostgresPidFileNotReadyError

        lines = piddata.splitlines()

        if len(lines) < 6:
            # A complete postgres pidfile is at least 6 lines
            raise PostgresPidFileNotReadyError

        pmpid = int(lines[0])
        if self._daemon_pid and pmpid != self._daemon_pid:
            # This might be an old pidfile left from previous postgres
            # daemon run.
            raise PostgresPidFileNotReadyError

        portnum = int(lines[3])
        sockdir = lines[4]
        hostaddr = lines[5]

        if sockdir:
            if sockdir[0] != '/':
                # Relative sockdir
                sockdir = os.path.normpath(
                    os.path.join(self._data_dir, sockdir))
            host_str = sockdir
        elif hostaddr:
            host_str = hostaddr
        else:
            raise PostgresPidFileNotReadyError

        if host_str == '*':
            host_str = 'localhost'
        elif host_str == '0.0.0.0':
            host_str = '127.0.0.1'
        elif host_str == '::':
            host_str = '::1'

        return (host_str, portnum)

    async def _test_connection(self, timeout: int = 60) -> str:
        self._connection_addr = None
        connected = False

        for n in range(timeout + 1):
            # pg usually comes up pretty quickly, but not so
            # quickly that we don't hit the wait case. Make our
            # first sleep pretty short, to shave almost a second
            # off the happy case.
            sleep_time = 1 if n else 0.10

            try:
                conn_addr = self._get_connection_addr()
            except PostgresPidFileNotReadyError:
                time.sleep(sleep_time)
                continue

            try:
                con = await asyncpg.connect(
                    database='postgres',
                    user='postgres',
                    timeout=5,
                    host=conn_addr[0],
                    port=conn_addr[1],
                )
            except (
                OSError,
                asyncio.TimeoutError,
                asyncpg.CannotConnectNowError,
                asyncpg.PostgresConnectionError,
            ):
                time.sleep(sleep_time)
                continue
            except asyncpg.PostgresError:
                # Any other error other than ServerNotReadyError or
                # ConnectionError is interpreted to indicate the server is
                # up.
                break
            else:
                connected = True
                await con.close()
                break

        if connected:
            return 'running'
        else:
            return 'not-initialized'


class RemoteCluster(BaseCluster):
    def __init__(
        self,
        addr: Tuple[str, int],
        params: pgconnparams.ConnectionParameters,
        *,
        instance_params: Optional[BackendInstanceParams] = None,
        ha_backend: Optional[ha_base.HABackend] = None,
    ):
        super().__init__(instance_params=instance_params)
        self._connection_addr = addr
        self._connection_params = params
        self._ha_backend = ha_backend

    def _get_connection_addr(self) -> Optional[Tuple[str, int]]:
        if self._ha_backend is not None:
            return self._ha_backend.get_master_addr()
        return self._connection_addr

    async def ensure_initialized(self, **settings: Any) -> bool:
        return False

    def is_managed(self) -> bool:
        return False

    async def get_status(self) -> str:
        return 'running'

    def init(self, **settings: str) -> str:
        pass

    async def start(
        self,
        wait: int = 60,
        *,
        server_settings: Optional[Mapping[str, str]] = None,
        **opts: Any,
    ) -> None:
        pass

    async def stop(self, wait: int = 60) -> None:
        pass

    def destroy(self) -> None:
        pass

    def reset_hba(self) -> None:
        raise ClusterError('cannot modify HBA records of unmanaged cluster')

    def add_hba_entry(
        self,
        *,
        type: str = 'host',
        database: str,
        user: str,
        address: Optional[str] = None,
        auth_method: str,
        auth_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise ClusterError('cannot modify HBA records of unmanaged cluster')

    async def start_watching(
        self, cluster_protocol: Optional[ha_base.ClusterProtocol] = None
    ) -> None:
        if self._ha_backend is not None:
            await self._ha_backend.start_watching(cluster_protocol)

    def stop_watching(self) -> None:
        if self._ha_backend is not None:
            self._ha_backend.stop_watching()


async def get_pg_bin_dir() -> pathlib.Path:
    pg_config_data = await get_pg_config()
    pg_bin_dir = pg_config_data.get('bindir')
    if not pg_bin_dir:
        raise ClusterError(
            'pg_config output did not provide the BINDIR value')
    return pathlib.Path(pg_bin_dir)


async def get_pg_config() -> Dict[str, str]:
    stdout_lines, _, _ = await _run_logged_text_subprocess(
        [str(buildmeta.get_pg_config_path())],
        logger=pg_config_logger,
    )

    config = {}
    for line in stdout_lines:
        k, eq, v = line.partition('=')
        if eq:
            config[k.strip().lower()] = v.strip()

    return config


async def get_local_pg_cluster(
    data_dir: pathlib.Path,
    *,
    runstate_dir: Optional[pathlib.Path] = None,
    max_connections: Optional[int] = None,
    tenant_id: Optional[str] = None,
    log_level: Optional[str] = None,
) -> Cluster:
    if log_level is None:
        log_level = 'i'
    if tenant_id is None:
        tenant_id = buildmeta.get_default_tenant_id()
    instance_params = None
    if max_connections is not None:
        instance_params = get_default_runtime_params(
            max_connections=max_connections,
            tenant_id=tenant_id,
        ).instance_params
    cluster = Cluster(
        data_dir=data_dir,
        runstate_dir=runstate_dir,
        instance_params=instance_params,
        log_level=log_level,
    )
    await cluster.lookup_postgres()
    return cluster


async def get_remote_pg_cluster(
    dsn: str,
    *,
    tenant_id: Optional[str] = None,
) -> RemoteCluster:
    parsed = urllib.parse.urlparse(dsn)
    ha_backend = None

    if parsed.scheme not in {'postgresql', 'postgres'}:
        ha_backend = ha_base.get_backend(parsed)
        if ha_backend is None:
            raise ValueError(
                'invalid DSN: scheme is expected to be "postgresql", '
                '"postgres" or one of the supported HA backend, '
                'got {!r}'.format(parsed.scheme))

        addr = await ha_backend.get_cluster_consensus()
        dsn = 'postgresql://{}:{}'.format(*addr)

    addrs, params = pgconnparams.parse_dsn(dsn)
    if len(addrs) > 1:
        raise ValueError('multiple hosts in Postgres DSN are not supported')
    if tenant_id is None:
        t_id = buildmeta.get_default_tenant_id()
    else:
        t_id = tenant_id
    rcluster = RemoteCluster(addrs[0], params)

    async def _get_cluster_type(
        conn: asyncpg.Connection,
    ) -> Tuple[Type[RemoteCluster], Optional[str]]:
        managed_clouds = {
            'rds_superuser': RemoteCluster,    # Amazon RDS
            'cloudsqlsuperuser': RemoteCluster,    # GCP Cloud SQL
        }

        managed_cloud_super = await conn.fetchval(
            """
                SELECT
                    rolname
                FROM
                    pg_roles
                WHERE
                    rolname = any($1::text[])
                LIMIT
                    1
            """,
            list(managed_clouds),
        )

        if managed_cloud_super is not None:
            return managed_clouds[managed_cloud_super], managed_cloud_super
        else:
            return RemoteCluster, None

    async def _detect_capabilities(
        conn: asyncpg.Connection,
    ) -> BackendCapabilities:
        caps = BackendCapabilities.NONE

        try:
            await conn.execute(f'ALTER SYSTEM SET foo = 10')
        except asyncpg.InsufficientPrivilegeError:
            configfile_access = False
        except asyncpg.UndefinedObjectError:
            configfile_access = True
        else:
            configfile_access = True

        if configfile_access:
            caps |= BackendCapabilities.CONFIGFILE_ACCESS

        tx = conn.transaction()
        await tx.start()
        rname = str(uuidgen.uuid1mc())

        try:
            await conn.execute(f'CREATE ROLE "{rname}" WITH SUPERUSER')
        except asyncpg.InsufficientPrivilegeError:
            can_make_superusers = False
        else:
            can_make_superusers = True
        finally:
            await tx.rollback()

        if can_make_superusers:
            caps |= BackendCapabilities.SUPERUSER_ACCESS

        coll = await conn.fetchval('''
            SELECT collname FROM pg_collation
            WHERE lower(replace(collname, '-', '')) = 'c.utf8' LIMIT 1;
        ''')

        if coll is not None:
            caps |= BackendCapabilities.C_UTF8_LOCALE

        return caps

    async def _get_pg_settings(
        conn: asyncpg.Connection,
        name: str,
    ) -> str:
        return await conn.fetchval(  # type: ignore
            'SELECT setting FROM pg_settings WHERE name = $1', name
        )

    async def _get_reserved_connections(
        conn: asyncpg.Connection,
    ) -> int:
        rv = int(
            await _get_pg_settings(conn, 'superuser_reserved_connections')
        )
        for name in [
            'rds.rds_superuser_reserved_connections',
        ]:
            value = await _get_pg_settings(conn, name)
            if value:
                rv += int(value)
        return rv

    conn = await rcluster.connect()
    try:
        cluster_type, superuser_name = await _get_cluster_type(conn)
        max_connections = await _get_pg_settings(conn, 'max_connections')
        instance_params = BackendInstanceParams(
            capabilities=await _detect_capabilities(conn),
            base_superuser=superuser_name,
            max_connections=int(max_connections),
            reserved_connections=await _get_reserved_connections(conn),
            tenant_id=t_id,
        )
    finally:
        await conn.close()

    return cluster_type(
        addrs[0],
        params,
        instance_params=instance_params,
        ha_backend=ha_backend,
    )


async def _run_logged_text_subprocess(
    args: Sequence[str],
    logger: logging.Logger,
    level: int = logging.DEBUG,
    check: bool = True,
    log_stdout: bool = True,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> Tuple[List[str], List[str], int]:
    stdout_lines, stderr_lines, exit_code = await _run_logged_subprocess(
        args,
        logger=logger,
        level=level,
        check=check,
        log_stdout=log_stdout,
        timeout=timeout,
        **kwargs,
    )

    return (
        [line.decode() for line in stdout_lines],
        [line.decode() for line in stderr_lines],
        exit_code,
    )


async def _run_logged_subprocess(
    args: Sequence[str],
    logger: logging.Logger,
    level: int = logging.DEBUG,
    check: bool = True,
    log_stdout: bool = True,
    log_stderr: bool = True,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> Tuple[List[bytes], List[bytes], int]:
    process, stdout_reader, stderr_reader = await _start_logged_subprocess(
        args,
        logger=logger,
        level=level,
        log_stdout=log_stdout,
        log_stderr=log_stderr,
        capture_stdout=capture_stdout,
        capture_stderr=capture_stderr,
        **kwargs,
    )

    exit_code, stdout_lines, stderr_lines = await asyncio.wait_for(
        asyncio.gather(process.wait(), stdout_reader, stderr_reader),
        timeout=timeout,
    )

    if exit_code != 0 and check:
        stderr_text = b'\n'.join(stderr_lines).decode()
        raise ClusterError(
            f'{args[0]} exited with status {exit_code}:\n'
            + textwrap.indent(stderr_text, ' ' * 4),
        )
    else:
        return stdout_lines, stderr_lines, exit_code


async def _start_logged_subprocess(
    args: Sequence[str],
    *,
    logger: logging.Logger,
    level: int = logging.DEBUG,
    log_stdout: bool = True,
    log_stderr: bool = True,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    log_processor: Optional[Callable[[str], Tuple[str, int]]] = None,
    **kwargs: Any,
) -> Tuple[
    asyncio.subprocess.Process,
    Coroutine[Any, Any, List[bytes]],
    Coroutine[Any, Any, List[bytes]],
]:
    logger.log(
        level,
        f'running `{" ".join(shlex.quote(arg) for arg in args)}`'
    )

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=(
            asyncio.subprocess.PIPE if log_stdout or capture_stdout
            else asyncio.subprocess.DEVNULL
        ),
        stderr=(
            asyncio.subprocess.PIPE if log_stderr or capture_stderr
            else asyncio.subprocess.DEVNULL
        ),
        **kwargs,
    )

    assert process.stderr is not None
    assert process.stdout is not None

    if log_stderr and capture_stderr:
        stderr_reader = _capture_and_log_subprocess_output(
            process.pid,
            process.stderr,
            logger,
            level,
            log_processor,
        )
    elif capture_stderr:
        stderr_reader = _capture_subprocess_output(process.stderr)
    elif log_stderr:
        stderr_reader = _log_subprocess_output(
            process.pid, process.stderr, logger, level, log_processor)
    else:
        stderr_reader = _dummy()

    if log_stdout and capture_stdout:
        stdout_reader = _capture_and_log_subprocess_output(
            process.pid,
            process.stdout,
            logger,
            level,
            log_processor,
        )
    elif capture_stdout:
        stdout_reader = _capture_subprocess_output(process.stdout)
    elif log_stdout:
        stdout_reader = _log_subprocess_output(
            process.pid, process.stdout, logger, level, log_processor)
    else:
        stdout_reader = _dummy()

    return process, stdout_reader, stderr_reader


async def _capture_subprocess_output(
    stream: asyncio.StreamReader,
) -> List[bytes]:
    lines = []
    while not stream.at_eof():
        line = await stream.readline()
        if line or not stream.at_eof():
            lines.append(line.rstrip(b'\n'))
    return lines


async def _capture_and_log_subprocess_output(
    pid: int,
    stream: asyncio.StreamReader,
    logger: logging.Logger,
    level: int,
    log_processor: Optional[Callable[[str], Tuple[str, int]]] = None,
) -> List[bytes]:
    lines = []
    while not stream.at_eof():
        line = await stream.readline()
        if line or not stream.at_eof():
            line = line.rstrip(b'\n')
            lines.append(line)
            log_line = line.decode()
            if log_processor is not None:
                log_line, level = log_processor(log_line)
            logger.log(level, log_line, extra={"process": pid})
    return lines


async def _log_subprocess_output(
    pid: int,
    stream: asyncio.StreamReader,
    logger: logging.Logger,
    level: int,
    log_processor: Optional[Callable[[str], Tuple[str, int]]] = None,
) -> List[bytes]:
    while not stream.at_eof():
        line = await stream.readline()
        if line or not stream.at_eof():
            log_line = line.rstrip(b'\n').decode()
            if log_processor is not None:
                log_line, level = log_processor(log_line)
            logger.log(level, log_line, extra={"process": pid})
    return []


async def _dummy() -> List[bytes]:
    return []


postgres_to_python_level_map = {
    "DEBUG5": logging.DEBUG,
    "DEBUG4": logging.DEBUG,
    "DEBUG3": logging.DEBUG,
    "DEBUG2": logging.DEBUG,
    "DEBUG1": logging.DEBUG,
    "INFO": logging.INFO,
    "NOTICE": logging.INFO,
    "LOG": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.CRITICAL,
    "PANIC": logging.CRITICAL,
}

postgres_log_re = re.compile(r'^(\w+):\s*(.*)$')

postgres_specific_msg_level_map = {
    "terminating connection due to administrator command": logging.INFO,
    "the database system is shutting down": logging.INFO,
}


def postgres_log_processor(msg: str) -> Tuple[str, int]:
    if m := postgres_log_re.match(msg):
        postgres_level = m.group(1)
        msg = m.group(2)
        level = postgres_specific_msg_level_map.get(
            msg,
            postgres_to_python_level_map.get(postgres_level, logging.INFO),
        )
    else:
        level = logging.INFO

    return msg, level
