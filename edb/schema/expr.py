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
from typing import *

import copy
import uuid

from edb.common import checked
from edb.common import struct

from edb.edgeql import ast as qlast_
from edb.edgeql import codegen as qlcodegen
from edb.edgeql import compiler as qlcompiler
from edb.edgeql import parser as qlparser
from edb.edgeql import qltypes

from . import abc as s_abc
from . import objects as so


if TYPE_CHECKING:
    from edb.schema import schema as s_schema
    from edb.schema import types as s_types

    from edb.ir import ast as irast_


class Expression(struct.MixedRTStruct, so.ObjectContainer, s_abc.Expression):

    text = struct.Field(str, frozen=True)
    # mypy wants an argument to the ObjectSet generic, but
    # that wouldn't work for struct.Field, since subscripted
    # generics are not types.
    refs = struct.Field(
        so.ObjectSet,  # type: ignore
        coerce=True,
        default=None,
        frozen=True,
    )

    def __init__(
        self,
        *args: Any,
        _qlast: Optional[qlast_.Expr] = None,
        _irast: Optional[irast_.Command] = None,
        **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._qlast = _qlast
        self._irast = _irast

    def __getstate__(self) -> Dict[str, Any]:
        return {
            'text': self.text,
            'refs': self.refs,
            '_qlast': None,
            '_irast': None,
        }

    @property
    def qlast(self) -> qlast_.Expr:
        if self._qlast is None:
            self._qlast = qlparser.parse_fragment(self.text)
        return self._qlast

    @property
    def irast(self) -> Optional[irast_.Command]:
        return self._irast

    def is_compiled(self) -> bool:
        return self.refs is not None

    @classmethod
    def compare_values(cls: Type[Expression],
                       ours: Expression,
                       theirs: Expression,
                       *,
                       our_schema: s_schema.Schema,
                       their_schema: s_schema.Schema,
                       context: so.ComparisonContext,
                       compcoef: float) -> float:
        if not ours and not theirs:
            return 1.0
        elif not ours or not theirs:
            return compcoef
        elif ours.text == theirs.text:
            return 1.0
        else:
            return compcoef

    @classmethod
    def from_ast(
        cls: Type[Expression],
        qltree: qlast_.Expr,
        schema: s_schema.Schema,
        modaliases: Optional[Mapping[Optional[str], str]] = None,
        localnames: AbstractSet[str] = frozenset(),
        *,
        as_fragment: bool = False,
        orig_text: Optional[str] = None,
    ) -> Expression:
        if modaliases is None:
            modaliases = {}
        if orig_text is None:
            orig_text = qlcodegen.generate_source(qltree, pretty=False)
        if not as_fragment:
            qlcompiler.normalize(
                qltree,
                schema=schema,
                modaliases=modaliases,
                localnames=localnames
            )

        norm_text = qlcodegen.generate_source(qltree, pretty=False)

        return cls(
            text=norm_text,
            _qlast=qltree,
        )

    @classmethod
    def not_compiled(cls: Type[Expression], expr: Expression) -> Expression:
        return Expression(text=expr.text)

    @classmethod
    def compiled(
        cls: Type[Expression],
        expr: Expression,
        schema: s_schema.Schema,
        *,
        options: Optional[qlcompiler.CompilerOptions] = None,
        as_fragment: bool = False,
    ) -> Expression:

        from edb.ir import ast as irast_

        if as_fragment:
            ir: irast_.Command = qlcompiler.compile_ast_fragment_to_ir(
                expr.qlast,
                schema=schema,
                options=options,
            )
        else:
            ir = qlcompiler.compile_ast_to_ir(
                expr.qlast,
                schema=schema,
                options=options,
            )

        assert isinstance(ir, irast_.Statement)

        return cls(
            text=expr.text,
            refs=so.ObjectSet.create(schema, ir.schema_refs),
            _qlast=expr.qlast,
            _irast=ir,
        )

    @classmethod
    def from_ir(cls: Type[Expression],
                expr: Expression,
                ir: irast_.Statement,
                schema: s_schema.Schema) -> Expression:
        return cls(
            text=expr.text,
            refs=so.ObjectSet.create(schema, ir.schema_refs),
            _qlast=expr.qlast,
            _irast=ir,
        )

    @classmethod
    def from_expr(cls: Type[Expression],
                  expr: Expression,
                  schema: s_schema.Schema) -> Expression:
        return cls(
            text=expr.text,
            refs=(
                so.ObjectSet.create(schema, expr.refs.objects(schema))
                if expr.refs is not None else None
            ),
            _qlast=expr._qlast,
            _irast=expr._irast,
        )

    def as_shell(self, schema: s_schema.Schema) -> ExpressionShell:
        return ExpressionShell(
            text=self.text,
            refs=(
                r.as_shell(schema) for r in self.refs.objects(schema)
            ) if self.refs is not None else None,
            _qlast=self._qlast,
        )

    def schema_reduce(
        self,
    ) -> Tuple[
        str,
        Tuple[
            str,
            Optional[Union[Tuple[type, ...], type]],
            Tuple[uuid.UUID, ...],
            Tuple[Tuple[str, Any], ...],
        ],
    ]:
        assert self.refs is not None, 'expected expression to be compiled'
        return (
            self.text,
            self.refs.schema_reduce(),
        )

    @classmethod
    def schema_restore(
        cls,
        data: Tuple[
            str,
            Tuple[
                str,
                Optional[Union[Tuple[type, ...], type]],
                Tuple[uuid.UUID, ...],
                Tuple[Tuple[str, Any], ...],
            ],
        ],
    ) -> Expression:
        text, refs_data = data
        return Expression(
            text=text,
            refs=so.ObjectCollection.schema_restore(refs_data),
        )

    @classmethod
    def schema_refs_from_data(
        cls,
        data: Tuple[
            str,
            Tuple[
                str,
                Optional[Union[Tuple[type, ...], type]],
                Tuple[uuid.UUID, ...],
                Tuple[Tuple[str, Any], ...],
            ],
        ],
    ) -> FrozenSet[uuid.UUID]:
        return so.ObjectCollection.schema_refs_from_data(data[1])

    @property
    def ir_statement(self) -> irast_.Statement:
        """Assert this expr is a compiled EdgeQL statement and return its IR"""
        from edb.ir import ast as irast_

        if not self.is_compiled():
            raise AssertionError('expected a compiled expression')
        ir = self.irast
        if not isinstance(ir, irast_.Statement):
            raise AssertionError(
                'expected the result of an expression to be a Statement')
        return ir

    @property
    def stype(self) -> s_types.Type:
        return self.ir_statement.stype

    @property
    def cardinality(self) -> qltypes.Cardinality:
        return self.ir_statement.cardinality

    @property
    def schema(self) -> s_schema.Schema:
        return self.ir_statement.schema


class ExpressionShell(so.Shell):

    def __init__(
        self,
        *,
        text: str,
        refs: Optional[Iterable[so.ObjectShell[so.Object]]],
        _qlast: Optional[qlast_.Expr] = None,
        _irast: Optional[irast_.Command] = None,
    ) -> None:
        self.text = text
        self.refs = tuple(refs) if refs is not None else None
        self._qlast = _qlast
        self._irast = _irast

    def resolve(self, schema: s_schema.Schema) -> Expression:
        return Expression(
            text=self.text,
            refs=so.ObjectSet.create(
                schema,
                (s.resolve(schema) for s in self.refs),
            ) if self.refs is not None else None,
            _qlast=self._qlast,
            _irast=self._irast,
        )

    @property
    def qlast(self) -> qlast_.Expr:
        if self._qlast is None:
            self._qlast = qlparser.parse_fragment(self.text)
        return self._qlast

    def __repr__(self) -> str:
        if self.refs is None:
            refs = 'N/A'
        else:
            refs = ', '.join(repr(obj) for obj in self.refs)
        return f'<ExpressionShell {self.text} refs=({refs})>'


class ExpressionList(checked.FrozenCheckedList[Expression]):

    @staticmethod
    def merge_values(target: so.Object,
                     sources: Sequence[so.Object],
                     field_name: str,
                     *,
                     ignore_local: bool = False,
                     schema: s_schema.Schema) -> Any:
        if not ignore_local:
            result = target.get_explicit_field_value(schema, field_name, None)
        else:
            result = None
        for source in sources:
            theirs = source.get_explicit_field_value(schema, field_name, None)
            if theirs:
                if result is None:
                    result = theirs[:]
                else:
                    result.extend(theirs)

        return result

    @classmethod
    def compare_values(cls: Type[ExpressionList],
                       ours: Optional[ExpressionList],
                       theirs: Optional[ExpressionList],
                       *,
                       our_schema: s_schema.Schema,
                       their_schema: s_schema.Schema,
                       context: so.ComparisonContext,
                       compcoef: float) -> float:
        """See the comment in Object.compare_values"""
        if not ours and not theirs:
            basecoef = 1.0
        elif (not ours or not theirs) or (len(ours) != len(theirs)):
            basecoef = 0.2
        else:
            similarity = []

            for expr1, expr2 in zip(ours, theirs):
                similarity.append(
                    Expression.compare_values(
                        expr1, expr2, our_schema=our_schema,
                        their_schema=their_schema, context=context,
                        compcoef=compcoef))

            basecoef = sum(similarity) / len(similarity)

        return basecoef + (1 - basecoef) * compcoef


def imprint_expr_context(
    qltree: qlast_.Base,
    modaliases: Mapping[Optional[str], str],
) -> qlast_.Base:
    # Imprint current module aliases as explicit
    # alias declarations in the expression.

    if (isinstance(qltree, qlast_.BaseConstant)
            or qltree is None
            or (isinstance(qltree, qlast_.Set)
                and not qltree.elements)
            or (isinstance(qltree, qlast_.Array)
                and all(isinstance(el, qlast_.BaseConstant)
                        for el in qltree.elements))):
        # Leave constants alone.
        return qltree

    if not isinstance(qltree, qlast_.Command):
        assert isinstance(qltree, qlast_.Expr)
        qltree = qlast_.SelectQuery(result=qltree, implicit=True)
    else:
        qltree = copy.copy(qltree)
        qltree.aliases = (
            list(qltree.aliases) if qltree.aliases is not None else None)

    existing_aliases: Dict[Optional[str], str] = {}
    for alias in (qltree.aliases or ()):
        if isinstance(alias, qlast_.ModuleAliasDecl):
            existing_aliases[alias.alias] = alias.module

    aliases_to_add = set(modaliases) - set(existing_aliases)
    for alias_name in aliases_to_add:
        if qltree.aliases is None:
            qltree.aliases = []
        qltree.aliases.append(
            qlast_.ModuleAliasDecl(
                alias=alias_name,
                module=modaliases[alias_name],
            )
        )

    return qltree


def get_expr_referrers(schema: s_schema.Schema,
                       obj: so.Object) -> Dict[so.Object, List[str]]:
    """Return schema referrers with refs in expressions."""

    refs: Dict[Tuple[Type[so.Object], str], FrozenSet[so.Object]] = (
        schema.get_referrers_ex(obj))
    result: Dict[so.Object, List[str]] = {}

    for (mcls, fn), referrers in refs.items():
        field = mcls.get_field(fn)
        if issubclass(field.type, (Expression, ExpressionList)):
            for ref in referrers:
                result.setdefault(ref, []).append(fn)

    return result
