#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2019-present MagicStack Inc. and the EdgeDB authors.
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


from __future__ import annotations
from typing import *

import asyncio
import http.client
import json
import os.path
import random
import subprocess
import ssl
import sys
import tempfile
import time

import edgedb
from edgedb import errors

from edb import protocol
from edb.common import devmode
from edb.common import taskgroup
from edb.protocol import protocol as edb_protocol  # type: ignore
from edb.server import pgcluster, pgconnparams
from edb.testbase import server as tb


class TestServerOps(tb.TestCase):

    async def kill_process(self, proc: asyncio.subprocess.Process):
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=20)
        except TimeoutError:
            proc.kill()

    async def test_server_ops_temp_dir(self):
        # Test that "edgedb-server" works as expected with the
        # following arguments:
        #
        # * "--port=auto"
        # * "--temp-dir"
        # * "--auto-shutdown-after=0"
        # * "--emit-server-status"

        async with tb.start_edgedb_server(
            auto_shutdown=True,
        ) as sd:

            con1 = await sd.connect()
            self.assertEqual(await con1.query_single('SELECT 1'), 1)

            con2 = await sd.connect()
            self.assertEqual(await con2.query_single('SELECT 1'), 1)

            await con1.aclose()

            self.assertEqual(await con2.query_single('SELECT 42'), 42)
            await con2.aclose()

            with self.assertRaises(
                    (ConnectionError, edgedb.ClientConnectionError)):
                # Since both con1 and con2 are now disconnected and
                # the cluster was started with an "--auto-shutdown-after=0"
                # option, we expect this connection to be rejected
                # and the cluster to be shutdown soon.
                await edgedb.async_connect(
                    user='edgedb',
                    host=sd.host,
                    port=sd.port,
                    tls_ca_file=sd.tls_cert_file,
                    wait_until_available=0,
                )

    async def test_server_ops_bootstrap_script(self):
        # Test that "edgedb-server" works as expected with the
        # following arguments:
        #
        # * "--bootstrap-only"
        # * "--bootstrap-command"

        cmd = [
            sys.executable, '-m', 'edb.server.main',
            '--port', 'auto',
            '--testmode',
            '--temp-dir',
            '--bootstrap-command=CREATE SUPERUSER ROLE test_bootstrap;',
            '--bootstrap-only',
            '--log-level=error',
            '--max-backend-connections', '10',
            '--generate-self-signed-cert',
        ]

        # Note: for debug comment "stderr=subprocess.PIPE".
        proc: asyncio.Process = await asyncio.create_subprocess_exec(
            *cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.terminate()
            raise
        else:
            self.assertTrue(
                proc.returncode == 0,
                f'server exited with code {proc.returncode}:\n'
                f'STDERR: {stderr.decode()}',
            )

    async def test_server_ops_bootstrap_script_server(self):
        # Test that "edgedb-server" works as expected with the
        # following arguments:
        #
        # * "--bootstrap-command"

        async with tb.start_edgedb_server(
            bootstrap_command='CREATE SUPERUSER ROLE test_bootstrap2 '
                              '{ SET password := "tbs2" };'
        ) as sd:
            con = await sd.connect(user='test_bootstrap2', password='tbs2')
            try:
                self.assertEqual(await con.query_single('SELECT 1'), 1)
            finally:
                await con.aclose()

    async def test_server_ops_emit_server_status_to_file(self):
        debug = False

        status_fd, status_file = tempfile.mkstemp()
        os.close(status_fd)

        cmd = [
            sys.executable, '-m', 'edb.server.main',
            '--port', 'auto',
            '--testmode',
            '--temp-dir',
            '--log-level=debug',
            '--max-backend-connections', '10',
            '--emit-server-status', status_file,
            '--generate-self-signed-cert',
        ]

        proc: Optional[asyncio.Process] = None

        def _read():
            with open(status_file, 'r') as f:
                while True:
                    result = f.readline()
                    if not result:
                        time.sleep(0.1)
                    else:
                        return result

        async def _waiter() -> Tuple[str, Mapping[str, Any]]:
            loop = asyncio.get_running_loop()
            line = await loop.run_in_executor(None, _read)
            status, _, dataline = line.partition('=')
            return status, json.loads(dataline)

        try:
            if debug:
                print(*cmd)
            proc: asyncio.Process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=None if debug else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )

            status, data = await asyncio.wait_for(_waiter(), timeout=240)
            self.assertEqual(status, 'READY')
            self.assertIsNotNone(data.get('socket_dir'))
            self.assertIsNotNone(data.get('port'))
            self.assertIsNotNone(data.get('main_pid'))

        finally:
            await self.kill_process(proc)
            os.unlink(status_file)

    async def test_server_ops_set_pg_max_connections(self):
        actual = random.randint(50, 100)
        async with tb.start_edgedb_server(
            max_allowed_connections=actual,
        ) as sd:
            con = await sd.connect()
            try:
                max_connections = await con.query_single(
                    'SELECT cfg::InstanceConfig.__pg_max_connections LIMIT 1'
                )  # TODO: remove LIMIT 1 after #2402
                self.assertEqual(int(max_connections), actual + 2)
            finally:
                await con.aclose()

    async def test_server_ops_detect_postgres_pool_size(self):
        actual = random.randint(50, 100)

        async def test(pgdata_path):
            async with tb.start_edgedb_server(
                max_allowed_connections=None,
                backend_dsn=f'postgres:///?user=postgres&host={pgdata_path}',
                reset_auth=True,
                runstate_dir=None if devmode.is_in_dev_mode() else pgdata_path,
            ) as sd:
                con = await sd.connect()
                try:
                    max_connections = await con.query_single(
                        '''
                        SELECT cfg::InstanceConfig.__pg_max_connections
                        LIMIT 1
                        '''
                    )  # TODO: remove LIMIT 1 after #2402
                    self.assertEqual(int(max_connections), actual)
                finally:
                    await con.aclose()

        with tempfile.TemporaryDirectory() as td:
            cluster = await pgcluster.get_local_pg_cluster(
                td, max_connections=actual, log_level='s')
            cluster.set_connection_params(
                pgconnparams.ConnectionParameters(
                    user='postgres',
                    database='template1',
                ),
            )
            self.assertTrue(await cluster.ensure_initialized())
            await cluster.start()
            try:
                await test(td)
            finally:
                await cluster.stop()

    async def test_server_ops_postgres_multitenant(self):
        async def test(pgdata_path, tenant):
            async with tb.start_edgedb_server(
                tenant_id=tenant,
                reset_auth=True,
                backend_dsn=f'postgres:///?user=postgres&host={pgdata_path}',
                runstate_dir=None if devmode.is_in_dev_mode() else pgdata_path,
            ) as sd:
                con = await sd.connect()
                try:
                    await con.execute(f'CREATE DATABASE {tenant}')
                    await con.execute(f'CREATE SUPERUSER ROLE {tenant}')
                    databases = await con.query('SELECT sys::Database.name')
                    self.assertEqual(set(databases), {'edgedb', tenant})
                    roles = await con.query('SELECT sys::Role.name')
                    self.assertEqual(set(roles), {'edgedb', tenant})
                finally:
                    await con.aclose()

        with tempfile.TemporaryDirectory() as td:
            cluster = await pgcluster.get_local_pg_cluster(td, log_level='s')
            cluster.set_connection_params(
                pgconnparams.ConnectionParameters(
                    user='postgres',
                    database='template1',
                ),
            )
            self.assertTrue(await cluster.ensure_initialized())

            await cluster.start()
            try:
                async with taskgroup.TaskGroup() as tg:
                    tg.create_task(test(td, 'tenant1'))
                    tg.create_task(test(td, 'tenant2'))
            finally:
                await cluster.stop()

    async def test_server_ops_postgres_recovery(self):
        async def test(pgdata_path):
            async with tb.start_edgedb_server(
                backend_dsn=f'postgres:///?user=postgres&host={pgdata_path}',
                reset_auth=True,
                runstate_dir=None if devmode.is_in_dev_mode() else pgdata_path,
            ) as sd:
                con = await sd.connect()
                try:
                    val = await con.query_single('SELECT 123')
                    self.assertEqual(int(val), 123)

                    # stop the postgres
                    await cluster.stop()
                    with self.assertRaisesRegex(
                        errors.BackendUnavailableError,
                        'Postgres is not available',
                    ):
                        await con.query_single('SELECT 123+456')

                    # bring postgres back
                    await cluster.start()

                    # give the EdgeDB server some time to recover
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            val = await con.query_single('SELECT 123+456')
                            break
                        except errors.BackendUnavailableError:
                            pass
                    self.assertEqual(int(val), 579)
                finally:
                    await con.aclose()

        with tempfile.TemporaryDirectory() as td:
            cluster = await pgcluster.get_local_pg_cluster(td, log_level='s')
            cluster.set_connection_params(
                pgconnparams.ConnectionParameters(
                    user='postgres',
                    database='template1',
                ),
            )
            self.assertTrue(await cluster.ensure_initialized())
            await cluster.start()
            try:
                await test(td)
            finally:
                await cluster.stop()

    async def _test_connection(self, con):
        await con.connect()

        await con.send(
            protocol.ExecuteScript(
                headers=[],
                script='SELECT 1'
            )
        )
        await con.recv_match(
            protocol.CommandComplete,
            status='SELECT'
        )
        await con.recv_match(
            protocol.ReadyForCommand,
            transaction_state=protocol.TransactionState.NOT_IN_TRANSACTION,
        )

    async def test_server_ops_downgrade_to_cleartext(self):
        async with tb.start_edgedb_server(
            allow_insecure_binary_clients=True,
        ) as sd:
            con = await edb_protocol.new_connection(
                user='edgedb',
                password=sd.password,
                host=sd.host,
                port=sd.port,
                use_tls=False,
            )
            try:
                await self._test_connection(con)
            finally:
                await con.aclose()

    async def test_server_ops_no_cleartext(self):
        async with tb.start_edgedb_server(
            allow_insecure_binary_clients=False,
            allow_insecure_http_clients=False,
        ) as sd:
            con = http.client.HTTPConnection(sd.host, sd.port)
            con.connect()
            try:
                con.request(
                    'GET',
                    f'http://{sd.host}:{sd.port}/blah404'
                )
                resp = con.getresponse()
                self.assertEqual(resp.status, 301)
                resp_headers = {k.lower(): v.lower()
                                for k, v in resp.getheaders()}
                self.assertIn('location', resp_headers)
                self.assertTrue(
                    resp_headers['location'].startswith('https://'))

                self.assertIn('strict-transport-security', resp_headers)
                # By default we enforce HTTPS via HSTS on all routes.
                self.assertEqual(
                    resp_headers['strict-transport-security'],
                    'max-age=31536000')
            finally:
                con.close()

            con = await edb_protocol.new_connection(
                user='edgedb',
                password=sd.password,
                host=sd.host,
                port=sd.port,
                use_tls=False,
            )
            try:
                with self.assertRaisesRegex(
                    errors.BinaryProtocolError, "TLS Required"
                ):
                    await con.connect()
            finally:
                await con.aclose()

            con = await edb_protocol.new_connection(
                user='edgedb',
                password=sd.password,
                host=sd.host,
                port=sd.port,
                tls_ca_file=sd.tls_cert_file,
            )
            try:
                await self._test_connection(con)
            finally:
                await con.aclose()

    async def test_server_ops_cleartext_http_allowed(self):
        async with tb.start_edgedb_server(
            allow_insecure_http_clients=True,
        ) as sd:

            con = http.client.HTTPConnection(sd.host, sd.port)
            con.connect()
            try:
                con.request(
                    'GET',
                    f'http://{sd.host}:{sd.port}/blah404'
                )
                resp = con.getresponse()
                self.assertEqual(resp.status, 404)
            finally:
                con.close()

            tls_context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=sd.tls_cert_file,
            )
            tls_context.check_hostname = False
            con = http.client.HTTPSConnection(
                sd.host, sd.port, context=tls_context)
            con.connect()
            try:
                con.request(
                    'GET',
                    f'http://{sd.host}:{sd.port}/blah404'
                )
                resp = con.getresponse()
                self.assertEqual(resp.status, 404)
                resp_headers = {k.lower(): v.lower()
                                for k, v in resp.getheaders()}

                self.assertIn('strict-transport-security', resp_headers)
                # When --allow-insecure-http-clients is passed, we set
                # max-age to 0, to let browsers know that it's safe
                # for the user to open http://
                self.assertEqual(
                    resp_headers['strict-transport-security'], 'max-age=0')
            finally:
                con.close()

            # Connect to let it autoshutdown; also test that
            # --allow-insecure-http-clients doesn't break binary
            # connections.
            con = await edb_protocol.new_connection(
                user='edgedb',
                password=sd.password,
                host=sd.host,
                port=sd.port,
                tls_ca_file=sd.tls_cert_file,
            )
            try:
                await self._test_connection(con)
            finally:
                await con.aclose()
