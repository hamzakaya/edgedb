# mypy: implicit-reexport

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


"""IR expression tree node definitions.

The IR expression tree is produced by the EdgeQL compiler
(see :mod:`edgeql.compiler`).  It is a self-contained representation
of an EdgeQL expression, which, together with the accompanying scope tree
(:mod:`ir.scopetree`) is sufficient to produce a backend query (e.g. SQL)
without any other input or context.

The most common part of the IR expression tree is the :class:`~Set` class.
Every expression is encoded as a ``Set`` instance that contains all common
metadata, such as the expression type, its symbolic identity (PathId) and
other useful bits.  The ``Set.expr`` field contains the specific node for
the expression.  The expression nodes usually refer to ``Set`` nodes
rather than other nodes directly.

For example, the EdgeQL expression ``SELECT str_lower('ABC') ++ 'd'``
yields the following IR (roughly):

Set (
  expr = SelectStmt (
    result = Set (
      expr = OperatorCall (
        args = [
          CallArg (
            expr = Set (
              expr = FunctionCall (
                args = [
                  CallArg (
                    expr = Set ( expr = StringConstant ( value = 'ABC' ) ),
                  ),
                  CallArg (
                    expr = Set ( expr = StringConstant ( value = 'd' ) ),
                  )
                ]
              )
            )
          )
        ]
      )
    )
  )
)
"""

from __future__ import annotations

import dataclasses
import typing
import uuid

from edb.common import ast, compiler, parsing, markup, enum as s_enum

from edb.schema import modules as s_mod
from edb.schema import name as sn
from edb.schema import objects as so
from edb.schema import pointers as s_pointers
from edb.schema import schema as s_schema
from edb.schema import types as s_types

from edb.edgeql import ast as qlast
from edb.edgeql import qltypes

from .pathid import PathId, Namespace  # noqa
from .scopetree import ScopeTreeNode  # noqa


def new_scope_tree() -> ScopeTreeNode:
    return ScopeTreeNode(fenced=True)


class Base(ast.AST):
    __abstract_node__ = True
    __ast_hidden__ = {'context'}

    context: typing.Optional[parsing.ParserContext] = None

    def __repr__(self) -> str:
        return (
            f'<ir.{self.__class__.__name__} at 0x{id(self):x}>'
        )


class ImmutableBase(ast.ImmutableASTMixin, Base):
    __abstract_node__ = True


class ViewShapeMetadata(Base):

    has_implicit_id: bool = False


class TypeRef(ImmutableBase):
    # Hide ancestors and descendants from debug spew because they are
    # incredibly noisy.
    __ast_hidden__ = {'ancestors', 'descendants'}

    # The id of the referenced type
    id: uuid.UUID
    # Full name of the type, not necessarily schema-addressable,
    # used for annotations only.
    name_hint: sn.Name
    # The ref of the underlying material type, if this is a view type,
    # else None.
    material_type: typing.Optional[TypeRef] = None
    # If this is a scalar type, base_type would be the highest
    # non-abstract base type.
    base_type: typing.Optional[TypeRef] = None
    # A set of type descendant descriptors, if necessary for
    # this type description.
    descendants: typing.Optional[typing.FrozenSet[TypeRef]] = None
    # A set of type ancestor descriptors, if necessary for
    # this type description.
    ancestors: typing.Optional[typing.FrozenSet[TypeRef]] = None
    # If this is a union type, this would be a set of
    # union elements.
    union: typing.Optional[typing.FrozenSet[TypeRef]] = None
    # Whether the union is specified by an exhaustive list of
    # types, and type inheritance should not be considered.
    union_is_concrete: bool = False
    # If this is an intersection type, this would be a set of
    # intersection elements.
    intersection: typing.Optional[typing.FrozenSet[TypeRef]] = None
    # If this node is an element of a collection, and the
    # collection elements are named, this would be then
    # name of the element.
    element_name: typing.Optional[str] = None
    # The kind of the collection type if this is a collection
    collection: typing.Optional[str] = None
    # Collection subtypes if this is a collection
    subtypes: typing.Tuple[TypeRef, ...] = ()
    # True, if this describes a scalar type
    is_scalar: bool = False
    # True, if this describes a view
    is_view: bool = False
    # True, if this describes an abstract type
    is_abstract: bool = False
    # True, if the collection type is persisted in the schema
    in_schema: bool = False
    # True, if this describes an opaque union type
    is_opaque_union: bool = False

    def __repr__(self) -> str:
        return f'<ir.TypeRef \'{self.name_hint}\' at 0x{id(self):x}>'

    @property
    def real_material_type(self) -> TypeRef:
        return self.material_type or self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class AnyTypeRef(TypeRef):
    pass


class AnyTupleRef(TypeRef):
    pass


class BasePointerRef(ImmutableBase):
    __abstract_node__ = True

    # Hide children to reduce noise
    __ast_hidden__ = {'children'}

    # cardinality fields need to be mutable for lazy cardinality inference.
    # and children because we update pointers with newly derived children
    __ast_mutable_fields__ = frozenset(
        ('in_cardinality', 'out_cardinality', 'children',
         'is_computable')
    )

    # The defaults set here are mostly to try to reduce debug spew output.
    name: sn.QualName
    shortname: sn.QualName
    path_id_name: typing.Optional[sn.QualName]
    std_parent_name: typing.Optional[sn.QualName] = None
    out_source: TypeRef
    out_target: TypeRef
    source_ptr: typing.Optional[PointerRef] = None
    base_ptr: typing.Optional[BasePointerRef] = None
    material_ptr: typing.Optional[BasePointerRef] = None
    children: typing.FrozenSet[BasePointerRef] = frozenset()
    union_components: typing.Optional[typing.Set[BasePointerRef]] = None
    intersection_components: typing.Optional[typing.Set[BasePointerRef]] = None
    union_is_concrete: bool = False
    has_properties: bool = False
    is_derived: bool = False
    is_computable: bool = False
    # Outbound cardinality of the pointer.
    out_cardinality: qltypes.Cardinality
    # Inbound cardinality of the pointer.
    in_cardinality: qltypes.Cardinality = qltypes.Cardinality.MANY

    def dir_target(self, direction: s_pointers.PointerDirection) -> TypeRef:
        if direction is s_pointers.PointerDirection.Outbound:
            return self.out_target
        else:
            return self.out_source

    def dir_source(self, direction: s_pointers.PointerDirection) -> TypeRef:
        if direction is s_pointers.PointerDirection.Outbound:
            return self.out_source
        else:
            return self.out_target

    def dir_cardinality(
        self, direction: s_pointers.PointerDirection
    ) -> qltypes.Cardinality:
        if direction is s_pointers.PointerDirection.Outbound:
            return self.out_cardinality
        else:
            return self.in_cardinality

    @property
    def required(self) -> bool:
        return self.out_cardinality.to_schema_value()[0]

    def descendants(self) -> typing.Set[BasePointerRef]:
        res = set(self.children)
        for child in self.children:
            res.update(child.descendants())
        return res

    def __repr__(self) -> str:
        return f'<ir.{type(self).__name__} \'{self.name}\' at 0x{id(self):x}>'


class PointerRef(BasePointerRef):
    id: uuid.UUID


class ConstraintRef(ImmutableBase):
    # The id of the constraint
    id: uuid.UUID


class TupleIndirectionLink(s_pointers.PseudoPointer):
    """A Link-alike that can be used in tuple indirection path ids."""

    def __init__(
        self,
        source: so.Object,
        target: s_types.Type,
        *,
        element_name: str,
    ) -> None:
        self._source = source
        self._target = target
        self._name = sn.QualName(
            module='__tuple__', name=str(element_name))

    def __hash__(self) -> int:
        return hash((self.__class__, self._name))

    def __eq__(self, other: typing.Any) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self._name == other._name

    def get_name(self, schema: s_schema.Schema) -> sn.QualName:
        return self._name

    def get_cardinality(
        self,
        schema: s_schema.Schema
    ) -> qltypes.SchemaCardinality:
        return qltypes.SchemaCardinality.One

    def singular(
        self,
        schema: s_schema.Schema,
        direction: s_pointers.PointerDirection =
            s_pointers.PointerDirection.Outbound
    ) -> bool:
        return True

    def scalar(self) -> bool:
        return self._target.is_scalar()

    def get_source(self, schema: s_schema.Schema) -> so.Object:
        return self._source

    def get_target(self, schema: s_schema.Schema) -> s_types.Type:
        return self._target

    def is_tuple_indirection(self) -> bool:
        return True

    def get_computable(self, schema: s_schema.Schema) -> bool:
        return False


class TupleIndirectionPointerRef(BasePointerRef):
    pass


class TypeIntersectionLink(s_pointers.PseudoPointer):
    """A Link-alike that can be used in type intersection path ids."""

    def __init__(
        self,
        source: so.Object,
        target: s_types.Type,
        *,
        optional: bool,
        is_empty: bool,
        is_subtype: bool,
        rptr_specialization: typing.Iterable[PointerRef] = (),
        cardinality: qltypes.SchemaCardinality,
    ) -> None:
        name = 'optindirection' if optional else 'indirection'
        self._name = sn.QualName(module='__type__', name=name)
        self._source = source
        self._target = target
        self._cardinality = cardinality
        self._optional = optional
        self._is_empty = is_empty
        self._is_subtype = is_subtype
        self._rptr_specialization = frozenset(rptr_specialization)

    def get_name(self, schema: s_schema.Schema) -> sn.QualName:
        return self._name

    def get_cardinality(
        self,
        schema: s_schema.Schema
    ) -> qltypes.SchemaCardinality:
        return self._cardinality

    def get_computable(self, schema: s_schema.Schema) -> bool:
        return False

    def is_type_intersection(self) -> bool:
        return True

    def is_optional(self) -> bool:
        return self._optional

    def is_empty(self) -> bool:
        return self._is_empty

    def is_subtype(self) -> bool:
        return self._is_subtype

    def get_rptr_specialization(self) -> typing.FrozenSet[PointerRef]:
        return self._rptr_specialization

    def get_source(self, schema: s_schema.Schema) -> so.Object:
        return self._source

    def get_target(self, schema: s_schema.Schema) -> s_types.Type:
        return self._target

    def singular(
        self,
        schema: s_schema.Schema,
        direction: s_pointers.PointerDirection =
            s_pointers.PointerDirection.Outbound
    ) -> bool:
        if direction is s_pointers.PointerDirection.Outbound:
            return (self.get_cardinality(schema) is
                    qltypes.SchemaCardinality.One)
        else:
            return True

    def scalar(self) -> bool:
        return self._target.is_scalar()


class TypeIntersectionPointerRef(BasePointerRef):

    optional: bool
    is_empty: bool
    is_subtype: bool
    rptr_specialization: typing.FrozenSet[PointerRef]


class Pointer(Base):

    source: Set
    target: Set
    ptrref: BasePointerRef
    direction: s_pointers.PointerDirection
    is_definition: bool
    anchor: typing.Optional[str] = None
    show_as_anchor: typing.Optional[str] = None

    @property
    def is_inbound(self) -> bool:
        return self.direction == s_pointers.PointerDirection.Inbound

    @property
    def dir_cardinality(self) -> qltypes.Cardinality:
        return self.ptrref.dir_cardinality(self.direction)


class TypeIntersectionPointer(Pointer):

    optional: bool
    ptrref: TypeIntersectionPointerRef
    is_definition: bool = False


class TupleIndirectionPointer(Pointer):

    ptrref: TupleIndirectionPointerRef
    is_definition: bool = False


class Expr(Base):
    __abstract_node__ = True

    # Sets to materialize at this point, keyed by the type/ptr id.
    materialized_sets: typing.Optional[
        typing.Dict[uuid.UUID, MaterializedSet]] = None


class ImmutableExpr(Expr, ImmutableBase):
    __abstract_node__ = True


class BindingKind(s_enum.StrEnum):
    With = 'With'
    For = 'For'
    Select = 'Select'


class Set(Base):

    __ast_frozen_fields__ = frozenset({'typeref'})

    path_id: PathId
    path_scope_id: typing.Optional[int] = None
    typeref: TypeRef
    expr: typing.Optional[Expr] = None
    rptr: typing.Optional[Pointer] = None
    anchor: typing.Optional[str] = None
    show_as_anchor: typing.Optional[str] = None
    shape: typing.Tuple[typing.Tuple[Set, qlast.ShapeOp], ...] = ()
    # A pointer to a set nested within this one has a shape and the same
    # typeref, if such a set exists.
    shape_source: typing.Optional[Set] = None
    is_binding: typing.Optional[BindingKind] = None

    def __repr__(self) -> str:
        return f'<ir.Set \'{self.path_id}\' at 0x{id(self):x}>'


class Command(Base):
    __abstract_node__ = True


@dataclasses.dataclass(frozen=True)
class Param:
    """Query parameter with it's schema type and IR type"""

    name: str
    """Parameter name"""

    required: bool
    """Whether parameter is OPTIONAL or REQUIRED"""

    schema_type: s_types.Type
    """Schema type"""

    ir_type: TypeRef
    """IR type reference"""


class MaterializeVolatile(typing.NamedTuple):
    pass


class MaterializeVisible(typing.NamedTuple):
    sets: typing.Set[typing.Tuple[PathId, Set]]


MaterializeReason = typing.Union[MaterializeVolatile, MaterializeVisible]


class ComputableInfo(typing.NamedTuple):

    qlexpr: qlast.Expr
    irexpr: typing.Optional[typing.Union[Set, Expr]]
    context: compiler.ContextLevel
    path_id: PathId
    path_id_ns: typing.Optional[Namespace]
    shape_op: qlast.ShapeOp
    should_materialize: typing.Sequence[MaterializeReason]


class Statement(Command):

    expr: typing.Union[Set, Expr]
    views: typing.Dict[sn.Name, s_types.Type]
    params: typing.List[Param]
    cardinality: qltypes.Cardinality
    volatility: qltypes.Volatility
    multiplicity: typing.Optional[qltypes.Multiplicity]
    stype: s_types.Type
    view_shapes: typing.Dict[so.Object, typing.List[s_pointers.Pointer]]
    view_shapes_metadata: typing.Dict[s_types.Type, ViewShapeMetadata]
    schema: s_schema.Schema
    schema_refs: typing.FrozenSet[so.Object]
    schema_ref_exprs: typing.Optional[
        typing.Dict[so.Object, typing.Set[qlast.Base]]]
    new_coll_types: typing.FrozenSet[s_types.Collection]
    scope_tree: ScopeTreeNode
    source_map: typing.Dict[s_pointers.Pointer, ComputableInfo]
    dml_exprs: typing.List[qlast.Base]
    type_rewrites: typing.Dict[uuid.UUID, Set]


class TypeIntrospection(ImmutableExpr):

    typeref: TypeRef


class ConstExpr(Expr):
    __abstract_node__ = True
    typeref: TypeRef


class EmptySet(Set, ConstExpr):
    pass


class BaseConstant(ConstExpr, ImmutableExpr):
    __abstract_node__ = True
    value: typing.Any

    def __init__(
        self,
        *args: typing.Any,
        typeref: TypeRef,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(*args, typeref=typeref, **kwargs)
        if self.typeref is None:
            raise ValueError('cannot create irast.Constant without a type')
        if self.value is None:
            raise ValueError('cannot create irast.Constant without a value')


class BaseStrConstant(BaseConstant):
    __abstract_node__ = True
    value: str


class StringConstant(BaseStrConstant):
    pass


class IntegerConstant(BaseStrConstant):
    pass


class FloatConstant(BaseStrConstant):
    pass


class DecimalConstant(BaseStrConstant):
    pass


class BigintConstant(BaseStrConstant):
    pass


class BooleanConstant(BaseStrConstant):
    pass


class BytesConstant(BaseConstant):

    value: bytes


class ConstantSet(ConstExpr, ImmutableExpr):

    elements: typing.Tuple[BaseConstant, ...]


class Parameter(ImmutableExpr):

    name: str
    required: bool
    typeref: TypeRef


class TupleElement(ImmutableBase):

    name: str
    val: Set
    path_id: typing.Optional[PathId] = None


class Tuple(ImmutableExpr):

    named: bool = False
    elements: typing.List[TupleElement]
    typeref: TypeRef


class Array(ImmutableExpr):

    elements: typing.Sequence[Set]
    typeref: TypeRef


class TypeCheckOp(ImmutableExpr):

    left: Set
    right: TypeRef
    op: str
    result: typing.Optional[bool] = None


class SortExpr(Base):

    expr: Set
    direction: typing.Optional[qlast.SortOrder]
    nones_order: typing.Optional[qlast.NonesOrder]


class CallArg(ImmutableBase):
    """Call argument."""

    # cardinality fields need to be mutable for lazy cardinality inference.
    __ast_mutable_fields__ = frozenset(('cardinality', 'multiplicity'))

    expr: Set
    """PathId for the __type__ link of object type arguments."""
    expr_type_path_id: typing.Optional[PathId] = None
    cardinality: qltypes.Cardinality = qltypes.Cardinality.UNKNOWN
    multiplicity: qltypes.Multiplicity = qltypes.Multiplicity.UNKNOWN


class Call(ImmutableExpr):
    """Operator or a function call."""
    __abstract_node__ = True

    # Bound callable has polymorphic parameters and
    # a polymorphic return type.
    func_polymorphic: bool

    # Bound callable's name.
    func_shortname: sn.QualName

    # Whether the bound callable is a "USING SQL EXPRESSION" callable.
    func_sql_expr: bool = False

    # Whether the return value of the function should be
    # explicitly cast into the declared function return type.
    force_return_cast: bool

    # Bound arguments.
    args: typing.List[CallArg]

    # Typemods of parameters.  This list corresponds to ".args"
    # (so `zip(args, params_typemods)` is valid.)
    params_typemods: typing.List[qltypes.TypeModifier]

    # Return type and typemod.  In bodies of polymorphic functions
    # the return type can be polymorphic; in queries the return
    # type will be a concrete schema type.
    typeref: TypeRef
    typemod: qltypes.TypeModifier

    # If the return type is a tuple, this will contain a list
    # of tuple element path ids relative to the call set.
    tuple_path_ids: typing.List[PathId]

    # Volatility of the function or operator.
    volatility: qltypes.Volatility


class FunctionCall(Call):

    # If the bound callable is a "USING SQL" callable, this
    # attribute will be set to the name of the SQL function.
    func_sql_function: typing.Optional[str]

    # initial value needed for aggregate function calls to correctly
    # handle empty set
    func_initial_value: typing.Optional[Set] = None

    # True if the bound function has a variadic parameter and
    # there are no arguments that are bound to it.
    has_empty_variadic: bool = False

    # The underlying SQL function has OUT parameters.
    sql_func_has_out_params: bool = False

    # backend_name for the underlying function
    backend_name: typing.Optional[uuid.UUID] = None

    # Error to raise if the underlying SQL function returns NULL.
    error_on_null_result: typing.Optional[str] = None

    # Whether the generic function preserves optionality of the generic
    # argument(s).
    preserves_optionality: bool = False

    # Whether the generic function preserves upper cardinality of the generic
    # argument(s).
    preserves_upper_cardinality: bool = False

    # Set to the type of the variadic parameter of the bound function
    # (or None, if the function has no variadic parameters.)
    variadic_param_type: typing.Optional[TypeRef] = None


class OperatorCall(Call):

    # The kind of the bound operator (INFIX, PREFIX, etc.).
    operator_kind: qltypes.OperatorKind

    # If the bound callable is a "USING SQL FUNCTION" callable, this
    # attribute will be set to the name of the SQL function.
    sql_function: typing.Optional[typing.Tuple[str, ...]] = None

    # If this operator maps directly onto an SQL operator, this
    # will contain the operator name, and, optionally, backend
    # operand types.
    sql_operator: typing.Optional[typing.Tuple[str, ...]] = None

    # The name of the origin operator if this is a derivative operator.
    origin_name: typing.Optional[sn.QualName] = None

    # The module id of the origin operator if this is a derivative operator.
    origin_module_id: typing.Optional[uuid.UUID] = None


class IndexIndirection(ImmutableExpr):

    expr: Base
    index: Base


class SliceIndirection(ImmutableExpr):

    expr: Base
    start: typing.Optional[Base]
    stop: typing.Optional[Base]


class TypeCast(ImmutableExpr):
    """<Type>ImmutableExpr"""

    expr: Set
    cast_name: typing.Optional[sn.QualName] = None
    from_type: TypeRef
    to_type: TypeRef
    cardinality_mod: typing.Optional[qlast.CardinalityModifier] = None
    sql_function: typing.Optional[str] = None
    sql_cast: bool
    sql_expr: bool


class MaterializedSet(Base):
    # Hide uses to reduce spew; we produce our own simpler uses
    __ast_hidden__ = {'use_sets'}
    materialized: Set
    reason: typing.Sequence[MaterializeReason]

    # We really only want the *paths* of all the places it is used,
    # but we need to store the sets to take advantage of weak
    # namespace rewriting.
    use_sets: typing.List[Set]
    cardinality: qltypes.Cardinality = qltypes.Cardinality.UNKNOWN

    # Whether this has been "finalized" by stmtctx; just for supporting some
    # assertions
    finalized: bool = False

    @property
    def uses(self) -> typing.List[PathId]:
        return [x.path_id for x in self.use_sets]


@markup.serializer.serializer.register(MaterializedSet)
def _serialize_to_markup(
        ir: MaterializedSet, *, ctx: typing.Any) -> typing.Any:
    # We want to show the path_ids but *not* to show the full uses
    node = ast.serialize_to_markup(ir, ctx=ctx)
    node.add_child(label='uses', node=markup.serialize(ir.uses, ctx=ctx))
    return node


class Stmt(Expr):
    __abstract_node__ = True
    # Hide parent_stmt to reduce debug spew and to hide it from find_children
    __ast_hidden__ = {'parent_stmt'}

    name: typing.Optional[str] = None
    # Parts of the edgeql->IR compiler need to create statements and fill in
    # the result later, but making it Optional would cause lots of errors,
    # so we stick a bogus Empty set in.
    result: Set = EmptySet()  # type: ignore
    parent_stmt: typing.Optional[Stmt] = None
    iterator_stmt: typing.Optional[Set] = None
    bindings: typing.Optional[typing.List[Set]] = None


class FilteredStmt(Stmt):
    __abstract_node__ = True
    where: typing.Optional[Set] = None
    where_card: qltypes.Cardinality = qltypes.Cardinality.UNKNOWN


class SelectStmt(FilteredStmt):

    orderby: typing.Optional[typing.List[SortExpr]] = None
    offset: typing.Optional[Set] = None
    limit: typing.Optional[Set] = None
    implicit_wrapper: bool = False


class GroupStmt(Stmt):
    subject: Set
    groupby: typing.List[Set]
    result: Set
    group_path_id: PathId


class MutatingStmt(Stmt):
    __abstract_node__ = True
    # Parts of the edgeql->IR compiler need to create statements and fill in
    # the subject later, but making it Optional would cause lots of errors,
    # so we stick a bogus Empty set in.
    subject: Set = EmptySet()  # type: ignore
    # Conflict checks that we should manually raise constraint violations
    # for.
    conflict_checks: typing.Optional[typing.List[OnConflictClause]] = None


class OnConflictClause(Base):
    constraint: typing.Optional[ConstraintRef]
    select_ir: Set
    always_check: bool
    else_ir: typing.Optional[Set]
    update_query_set: typing.Optional[Set] = None
    else_fail: typing.Optional[MutatingStmt] = None


class InsertStmt(MutatingStmt):
    on_conflict: typing.Optional[OnConflictClause] = None


class UpdateStmt(MutatingStmt, FilteredStmt):
    # The pgsql DML compilation needs to be able to access __type__
    # fields on link fields for doing covariant assignment checking.
    # To enable this, we just make sure that update has access to
    # BaseObject's __type__, from which we can derive whatever we need.
    # This is at least a bit of a hack.
    dunder_type_ptrref: BasePointerRef


class DeleteStmt(MutatingStmt, FilteredStmt):
    pass


class SessionStateCmd(Command):

    modaliases: typing.Dict[typing.Optional[str], s_mod.Module]
    testmode: bool


class ConfigCommand(Command, Expr):
    __abstract_node__ = True
    name: str
    scope: qltypes.ConfigScope
    cardinality: qltypes.SchemaCardinality
    requires_restart: bool
    backend_setting: str
    scope_tree: typing.Optional[ScopeTreeNode] = None


class ConfigSet(ConfigCommand):

    expr: Set


class ConfigReset(ConfigCommand):

    selector: typing.Optional[Set] = None


class ConfigInsert(ConfigCommand):

    expr: Set
