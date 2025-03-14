#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2018-present MagicStack Inc. and the EdgeDB authors.
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


CREATE MODULE cfg;

CREATE ABSTRACT INHERITABLE ANNOTATION cfg::backend_setting;
CREATE ABSTRACT INHERITABLE ANNOTATION cfg::internal;
CREATE ABSTRACT INHERITABLE ANNOTATION cfg::requires_restart;
CREATE ABSTRACT INHERITABLE ANNOTATION cfg::system;
CREATE ABSTRACT INHERITABLE ANNOTATION cfg::affects_compilation;

CREATE ABSTRACT TYPE cfg::ConfigObject EXTENDING std::BaseObject;

CREATE ABSTRACT TYPE cfg::AuthMethod EXTENDING cfg::ConfigObject;
CREATE TYPE cfg::Trust EXTENDING cfg::AuthMethod;
CREATE TYPE cfg::SCRAM EXTENDING cfg::AuthMethod;


CREATE TYPE cfg::Auth EXTENDING cfg::ConfigObject {
    CREATE REQUIRED PROPERTY priority -> std::int64 {
        CREATE CONSTRAINT std::exclusive;
        SET readonly := true;
    };

    CREATE MULTI PROPERTY user -> std::str {
        SET readonly := true;
        SET default := {'*'};
    };

    CREATE SINGLE LINK method -> cfg::AuthMethod {
        CREATE CONSTRAINT std::exclusive;
    };

    CREATE PROPERTY comment -> std::str {
        SET readonly := true;
    };
};


CREATE ABSTRACT TYPE cfg::AbstractConfig extending cfg::ConfigObject {
    CREATE REQUIRED PROPERTY client_idle_timeout -> std::int16 {
        CREATE ANNOTATION cfg::system := 'true';
        SET default := 30;  # 30 seconds
    };

    CREATE REQUIRED PROPERTY listen_port -> std::int16 {
        CREATE ANNOTATION cfg::system := 'true';
        SET default := 5656;
    };

    CREATE MULTI PROPERTY listen_addresses -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
    };

    CREATE MULTI LINK auth -> cfg::Auth {
        CREATE ANNOTATION cfg::system := 'true';
    };

    CREATE PROPERTY allow_dml_in_functions -> std::bool {
        SET default := false;
        CREATE ANNOTATION cfg::affects_compilation := 'true';
        CREATE ANNOTATION cfg::internal := 'true';
    };

    # Exposed backend settings follow.
    # When exposing a new setting, remember to modify
    # the _read_sys_config function to select the value
    # from pg_settings in the config_backend CTE.
    CREATE PROPERTY shared_buffers -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
        CREATE ANNOTATION cfg::backend_setting := '"shared_buffers"';
        CREATE ANNOTATION cfg::requires_restart := 'true';
        SET default := '-1';
    };

    CREATE PROPERTY query_work_mem -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
        CREATE ANNOTATION cfg::backend_setting := '"work_mem"';
        SET default := '-1';
    };

    CREATE PROPERTY effective_cache_size -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
        CREATE ANNOTATION cfg::backend_setting := '"effective_cache_size"';
        SET default := '-1';
    };

    CREATE PROPERTY effective_io_concurrency -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
        CREATE ANNOTATION cfg::backend_setting := '"effective_io_concurrency"';
        SET default := '50';
    };

    CREATE PROPERTY default_statistics_target -> std::str {
        CREATE ANNOTATION cfg::system := 'true';
        CREATE ANNOTATION cfg::backend_setting := '"default_statistics_target"';
        SET default := '100';
    };
};


CREATE TYPE cfg::Config EXTENDING cfg::AbstractConfig;
CREATE TYPE cfg::InstanceConfig EXTENDING cfg::AbstractConfig;
CREATE TYPE cfg::DatabaseConfig EXTENDING cfg::AbstractConfig;


CREATE FUNCTION
cfg::get_config_json(
    NAMED ONLY sources: OPTIONAL array<std::str> = {},
    NAMED ONLY max_source: OPTIONAL std::str = {}
) -> std::json
{
    USING SQL $$
    SELECT
        coalesce(jsonb_object_agg(cfg.name, cfg), '{}'::jsonb)
    FROM
        edgedb._read_sys_config(
            sources::edgedb._sys_config_source_t[],
            max_source::edgedb._sys_config_source_t
        ) AS cfg
    $$;
};

CREATE FUNCTION
cfg::_quote(text: std::str) -> std::str
{
    SET volatility := 'Stable';
    SET internal := true;
    USING SQL $$
        SELECT replace(quote_literal(text), '''''', '\\''')
    $$
};

CREATE FUNCTION
cfg::_describe_system_config_as_ddl() -> str
{
    # The results won't change within a single statement.
    SET volatility := 'Stable';
    SET internal := true;
    USING SQL FUNCTION 'edgedb._describe_system_config_as_ddl';
};


CREATE FUNCTION
cfg::_describe_database_config_as_ddl() -> str
{
    # The results won't change within a single statement.
    SET volatility := 'Stable';
    SET internal := true;
    USING SQL FUNCTION 'edgedb._describe_database_config_as_ddl';
};
