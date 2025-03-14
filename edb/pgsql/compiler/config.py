#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
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

from edb import errors
from edb.ir import ast as irast

from edb.edgeql import qltypes

from edb.pgsql import ast as pgast

from . import astutils
from . import clauses
from . import context
from . import dispatch
from . import pathctx
from . import relctx
from . import output


@dispatch.compile.register
def compile_ConfigSet(
    op: irast.ConfigSet,
    *,
    ctx: context.CompilerContextLevel,
) -> pgast.BaseExpr:

    val: pgast.BaseExpr

    with ctx.new() as subctx:
        if op.backend_setting:
            output_format = context.OutputFormat.NATIVE
        else:
            output_format = context.OutputFormat.JSONB

        with context.output_format(ctx, output_format):
            if isinstance(op.expr, irast.EmptySet):
                # Special handling for empty sets, because we want a
                # singleton representation of the value and not an empty rel
                # in this context.
                if op.cardinality is qltypes.SchemaCardinality.One:
                    val = pgast.NullConstant()
                elif subctx.env.output_format is context.OutputFormat.JSONB:
                    val = pgast.TypeCast(
                        arg=pgast.StringConstant(val='[]'),
                        type_name=pgast.TypeName(
                            name=('jsonb',),
                        ),
                    )
                else:
                    val = pgast.TypeCast(
                        arg=pgast.ArrayExpr(elements=[]),
                        type_name=pgast.TypeName(
                            name=('text[]',),
                        ),
                    )
            else:
                val = dispatch.compile(op.expr, ctx=subctx)
                assert isinstance(val, pgast.SelectStmt), "expected SelectStmt"

                pathctx.get_path_serialized_output(
                    val, op.expr.path_id, env=ctx.env)

                if op.cardinality is qltypes.SchemaCardinality.Many:
                    val = output.aggregate_json_output(
                        val, op.expr, env=ctx.env)

    result: pgast.BaseExpr

    if op.scope is qltypes.ConfigScope.INSTANCE and op.backend_setting:
        assert isinstance(val, pgast.SelectStmt) and len(val.target_list) == 1
        valval = val.target_list[0].val
        if isinstance(valval, pgast.TypeCast):
            valval = valval.arg
        if not isinstance(valval, pgast.BaseConstant):
            raise AssertionError('value is not a constant in ConfigSet')
        result = pgast.AlterSystem(
            name=op.backend_setting,
            value=valval,
        )

    elif op.scope is qltypes.ConfigScope.DATABASE and op.backend_setting:
        fcall = pgast.FuncCall(
            name=('edgedb', '_alter_current_database_set'),
            args=[pgast.StringConstant(val=op.backend_setting), val],
        )

        result = output.wrap_script_stmt(
            pgast.SelectStmt(target_list=[pgast.ResTarget(val=fcall)]),
            suppress_all_output=True,
            env=ctx.env,
        )

    elif op.scope is qltypes.ConfigScope.SESSION and op.backend_setting:
        fcall = pgast.FuncCall(
            name=('pg_catalog', 'set_config'),
            args=[
                pgast.StringConstant(val=op.backend_setting),
                pgast.TypeCast(
                    arg=val,
                    type_name=pgast.TypeName(name=('text',)),
                ),
                pgast.BooleanConstant(val='false'),
            ],
        )

        result = output.wrap_script_stmt(
            pgast.SelectStmt(target_list=[pgast.ResTarget(val=fcall)]),
            suppress_all_output=True,
            env=ctx.env,
        )

    elif op.scope is qltypes.ConfigScope.INSTANCE:
        result_row = pgast.RowExpr(
            args=[
                pgast.StringConstant(val='SET'),
                pgast.StringConstant(val=str(op.scope)),
                pgast.StringConstant(val=op.name),
                val,
            ]
        )

        result = pgast.FuncCall(
            name=('jsonb_build_array',),
            args=result_row.args,
            null_safe=True,
            ser_safe=True,
        )

        result = pgast.SelectStmt(
            target_list=[
                pgast.ResTarget(
                    val=result,
                ),
            ],
        )
    elif op.scope is qltypes.ConfigScope.SESSION:
        result = pgast.InsertStmt(
            relation=pgast.RelRangeVar(
                relation=pgast.Relation(
                    name='_edgecon_state',
                ),
            ),
            select_stmt=pgast.SelectStmt(
                values=[
                    pgast.ImplicitRowExpr(
                        args=[
                            pgast.StringConstant(
                                val=op.name,
                            ),
                            val,
                            pgast.StringConstant(
                                val='C',
                            ),
                        ]
                    )
                ]
            ),
            cols=[
                pgast.ColumnRef(name=['name']),
                pgast.ColumnRef(name=['value']),
                pgast.ColumnRef(name=['type']),
            ],
            on_conflict=pgast.OnConflictClause(
                action='update',
                infer=pgast.InferClause(
                    index_elems=[
                        pgast.ColumnRef(name=['name']),
                        pgast.ColumnRef(name=['type']),
                    ],
                ),
                target_list=[
                    pgast.MultiAssignRef(
                        columns=[pgast.ColumnRef(name=['value'])],
                        source=pgast.RowExpr(
                            args=[
                                val,
                            ],
                        ),
                    ),
                ],
            ),
        )
    elif op.scope is qltypes.ConfigScope.DATABASE:
        result = pgast.InsertStmt(
            relation=pgast.RelRangeVar(
                relation=pgast.Relation(
                    name='_db_config',
                    schemaname='edgedb',
                ),
            ),
            select_stmt=pgast.SelectStmt(
                values=[
                    pgast.ImplicitRowExpr(
                        args=[
                            pgast.StringConstant(
                                val=op.name,
                            ),
                            val,
                        ]
                    )
                ]
            ),
            cols=[
                pgast.ColumnRef(name=['name']),
                pgast.ColumnRef(name=['value']),
            ],
            on_conflict=pgast.OnConflictClause(
                action='update',
                infer=pgast.InferClause(
                    index_elems=[
                        pgast.ColumnRef(name=['name']),
                    ],
                ),
                target_list=[
                    pgast.MultiAssignRef(
                        columns=[pgast.ColumnRef(name=['value'])],
                        source=pgast.RowExpr(
                            args=[
                                val,
                            ],
                        ),
                    ),
                ],
            ),
        )
    else:
        raise AssertionError(f'unexpected configuration scope: {op.scope}')

    return result


@dispatch.compile.register
def compile_ConfigReset(
    op: irast.ConfigReset,
    *,
    ctx: context.CompilerContextLevel,
) -> pgast.BaseExpr:

    stmt: pgast.BaseExpr

    if op.scope is qltypes.ConfigScope.INSTANCE and op.backend_setting:
        stmt = pgast.AlterSystem(
            name=op.backend_setting,
            value=None,
        )

    elif op.scope is qltypes.ConfigScope.DATABASE and op.backend_setting:
        fcall = pgast.FuncCall(
            name=('edgedb', '_alter_current_database_set'),
            args=[
                pgast.StringConstant(val=op.backend_setting),
                pgast.NullConstant(),
            ],
        )

        stmt = output.wrap_script_stmt(
            pgast.SelectStmt(target_list=[pgast.ResTarget(val=fcall)]),
            suppress_all_output=True,
            env=ctx.env,
        )

    elif op.scope is qltypes.ConfigScope.SESSION and op.backend_setting:
        fcall = pgast.FuncCall(
            name=('pg_catalog', 'set_config'),
            args=[
                pgast.StringConstant(val=op.backend_setting),
                pgast.NullConstant(),
                pgast.BooleanConstant(val='false'),
            ],
        )

        stmt = output.wrap_script_stmt(
            pgast.SelectStmt(target_list=[pgast.ResTarget(val=fcall)]),
            suppress_all_output=True,
            env=ctx.env,
        )

    elif op.scope is qltypes.ConfigScope.INSTANCE:

        if op.selector is None:
            # Scalar reset
            result_row = pgast.RowExpr(
                args=[
                    pgast.StringConstant(val='RESET'),
                    pgast.StringConstant(val=str(op.scope)),
                    pgast.StringConstant(val=op.name),
                    pgast.NullConstant(),
                ]
            )

            rvar = None
        else:
            with context.output_format(ctx, context.OutputFormat.JSONB):
                selector = dispatch.compile(op.selector, ctx=ctx)

            assert isinstance(selector, pgast.SelectStmt), \
                "expected ast.SelectStmt"
            target = selector.target_list[0]
            if not target.name:
                target = selector.target_list[0] = pgast.ResTarget(
                    name=ctx.env.aliases.get('res'),
                    val=target.val,
                )
                assert target.name is not None

            rvar = relctx.rvar_for_rel(selector, ctx=ctx)

            result_row = pgast.RowExpr(
                args=[
                    pgast.StringConstant(val='REM'),
                    pgast.StringConstant(val=str(op.scope)),
                    pgast.StringConstant(val=op.name),
                    astutils.get_column(rvar, target.name),
                ]
            )

        result = pgast.FuncCall(
            name=('jsonb_build_array',),
            args=result_row.args,
            null_safe=True,
            ser_safe=True,
        )

        stmt = pgast.SelectStmt(
            target_list=[
                pgast.ResTarget(
                    val=result,
                ),
            ],
        )

        if rvar is not None:
            stmt.from_clause = [rvar]

    elif op.scope is qltypes.ConfigScope.DATABASE:
        stmt = pgast.DeleteStmt(
            relation=pgast.RelRangeVar(
                relation=pgast.Relation(
                    name='_db_config',
                    schemaname='edgedb',
                ),
            ),

            where_clause=astutils.new_binop(
                lexpr=pgast.ColumnRef(name=['name']),
                rexpr=pgast.StringConstant(val=op.name),
                op='=',
            ),
        )

    elif op.scope is qltypes.ConfigScope.SESSION:
        stmt = pgast.DeleteStmt(
            relation=pgast.RelRangeVar(
                relation=pgast.Relation(
                    name='_edgecon_state',
                ),
            ),

            where_clause=astutils.new_binop(
                lexpr=astutils.new_binop(
                    lexpr=pgast.ColumnRef(name=['name']),
                    rexpr=pgast.StringConstant(val=op.name),
                    op='=',
                ),
                rexpr=astutils.new_binop(
                    lexpr=pgast.ColumnRef(name=['type']),
                    rexpr=pgast.StringConstant(val='C'),
                    op='=',
                ),
                op='AND',
            )
        )

    else:
        raise AssertionError(f'unexpected configuration scope: {op.scope}')

    return stmt


@dispatch.compile.register
def compile_ConfigInsert(
        stmt: irast.ConfigInsert, *,
        ctx: context.CompilerContextLevel) -> pgast.BaseExpr:

    with ctx.new() as subctx:
        with context.output_format(ctx, context.OutputFormat.JSONB):
            subctx.expr_exposed = True
            rewritten = _rewrite_config_insert(stmt.expr, ctx=subctx)
            dispatch.compile(rewritten, ctx=subctx)
            clauses.fini_stmt(ctx.rel, ctx=subctx, parent_ctx=ctx)

            return pathctx.get_path_serialized_output(
                ctx.rel, stmt.expr.path_id, env=ctx.env)


def _rewrite_config_insert(
        ir_set: irast.Set, *,
        ctx: context.CompilerContextLevel) -> irast.Set:

    overwrite_query = pgast.SelectStmt()
    id_expr = pgast.FuncCall(
        name=('edgedbext', 'uuid_generate_v1mc',),
        args=[],
    )
    pathctx.put_path_identity_var(
        overwrite_query, ir_set.path_id, id_expr, force=True, env=ctx.env)
    pathctx.put_path_value_var(
        overwrite_query, ir_set.path_id, id_expr, force=True, env=ctx.env)

    relctx.add_type_rel_overlay(
        ir_set.typeref,
        'replace',
        overwrite_query,
        path_id=ir_set.path_id,
        ctx=ctx,
    )

    # Config objects have derived computed ids,
    # so the autogenerated id must not be returned.
    ir_set.shape = tuple(filter(
        lambda el: (
            (rptr := el[0].rptr) is not None
            and rptr.ptrref.shortname.name != 'id'
        ),
        ir_set.shape,
    ))

    for el, _ in ir_set.shape:
        if isinstance(el.expr, irast.InsertStmt):
            el.shape = tuple(filter(
                lambda e: (
                    (rptr := e[0].rptr) is not None
                    and rptr.ptrref.shortname.name != 'id'
                ),
                el.shape,
            ))

            result = _rewrite_config_insert(el.expr.subject, ctx=ctx)
            el.expr = irast.SelectStmt(
                result=result,
                parent_stmt=el.expr.parent_stmt,
            )

    return ir_set


def top_output_as_config_op(
        ir_set: irast.Set,
        stmt: pgast.SelectStmt, *,
        env: context.Environment) -> pgast.Query:

    assert isinstance(ir_set.expr, irast.ConfigCommand)

    if ir_set.expr.scope is qltypes.ConfigScope.INSTANCE:
        alias = env.aliases.get('cfg')
        subrvar = pgast.RangeSubselect(
            subquery=stmt,
            alias=pgast.Alias(
                aliasname=alias,
            )
        )

        stmt_res = stmt.target_list[0]

        if stmt_res.name is None:
            stmt_res = stmt.target_list[0] = pgast.ResTarget(
                name=env.aliases.get('v'),
                val=stmt_res.val,
            )
            assert stmt_res.name is not None

        result_row = pgast.RowExpr(
            args=[
                pgast.StringConstant(val='ADD'),
                pgast.StringConstant(val=str(ir_set.expr.scope)),
                pgast.StringConstant(val=ir_set.expr.name),
                pgast.ColumnRef(name=[stmt_res.name]),
            ]
        )

        array = pgast.FuncCall(
            name=('jsonb_build_array',),
            args=result_row.args,
            null_safe=True,
            ser_safe=True,
        )

        result = pgast.SelectStmt(
            target_list=[
                pgast.ResTarget(
                    val=array,
                ),
            ],
            from_clause=[
                subrvar,
            ],
        )

        result.ctes = stmt.ctes
        result.argnames = stmt.argnames
        stmt.ctes = []

        return result
    else:
        raise errors.InternalServerError(
            f'CONFIGURE {ir_set.expr.scope} INSERT is not supported')
