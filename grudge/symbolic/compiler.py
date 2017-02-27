"""Compiler to turn operator expression tree into (imperative) bytecode."""

from __future__ import division, absolute_import, print_function

__copyright__ = "Copyright (C) 2008-15 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import six  # noqa
from six.moves import zip, reduce
from pytools import Record, memoize_method, memoize
from grudge import sym
import grudge.symbolic.mappers as mappers
from pymbolic.primitives import Variable, Subscript


# {{{ instructions

class Instruction(Record):
    __slots__ = []
    priority = 0

    def get_assignees(self):
        raise NotImplementedError("no get_assignees in %s" % self.__class__)

    def get_dependencies(self):
        raise NotImplementedError("no get_dependencies in %s" % self.__class__)

    def __str__(self):
        raise NotImplementedError

    def get_execution_method(self, exec_mapper):
        raise NotImplementedError

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other

    def __ne__(self, other):
        return not self.__eq__(other)


@memoize
def _make_dep_mapper(include_subscripts):
    return mappers.DependencyMapper(
            include_operator_bindings=False,
            include_subscripts=include_subscripts,
            include_calls="descend_args")


class AssignBase(Instruction):
    comment = ""
    scope_indicator = ""

    def __str__(self):
        comment = self.comment
        if len(self.names) == 1:
            if comment:
                comment = "/* %s */ " % comment

            return "%s <-%s %s%s" % (
                    self.names[0], self.scope_indicator, comment,
                    self.exprs[0])
        else:
            if comment:
                comment = " /* %s */" % comment

            lines = []
            lines.append("{" + comment)
            for n, e, dnr in zip(self.names, self.exprs, self.do_not_return):
                if dnr:
                    dnr_indicator = "-#"
                else:
                    dnr_indicator = ""

                lines.append("  %s <%s-%s %s" % (
                    n, dnr_indicator, self.scope_indicator, e))
            lines.append("}")
            return "\n".join(lines)


class Assign(AssignBase):
    """
    .. attribute:: names
    .. attribute:: exprs
    .. attribute:: do_not_return

        a list of bools indicating whether the corresponding entry in names and
        exprs describes an expression that is not needed beyond this assignment

    .. attribute:: priority
    .. attribute:: is_scalar_valued
    """

    def __init__(self, names, exprs, **kwargs):
        Instruction.__init__(self, names=names, exprs=exprs, **kwargs)

        if not hasattr(self, "do_not_return"):
            self.do_not_return = [False] * len(names)

    @memoize_method
    def flop_count(self):
        return sum(mappers.FlopCounter()(expr) for expr in self.exprs)

    def get_assignees(self):
        return set(self.names)

    @memoize_method
    def get_dependencies(self, each_vector=False):
        dep_mapper = _make_dep_mapper(include_subscripts=False)

        from operator import or_
        deps = reduce(
                or_, (dep_mapper(expr)
                for expr in self.exprs))

        from pymbolic.primitives import Variable
        deps -= set(Variable(name) for name in self.names)

        if not each_vector:
            self._dependencies = deps

        return deps

    def get_execution_method(self, exec_mapper):
        return exec_mapper.exec_assign


class ToDiscretizationScopedAssign(Assign):
    scope_indicator = "(to discr)-"

    def get_execution_method(self, exec_mapper):
        return exec_mapper.exec_assign_to_discr_scoped


class FromDiscretizationScopedAssign(AssignBase):
    scope_indicator = "(discr)-"

    def __init__(self, name, **kwargs):
        super(FromDiscretizationScopedAssign, self).__init__(name=name, **kwargs)

    @memoize_method
    def flop_count(self):
        return 0

    def get_assignees(self):
        return frozenset([self.name])

    def get_dependencies(self):
        return frozenset()

    def __str__(self):
        return "%s <-(from discr)" % self.name

    def get_execution_method(self, exec_mapper):
        return exec_mapper.exec_assign_from_discr_scoped


class DiffBatchAssign(Instruction):
    """
    :ivar names:
    :ivar operators:

        .. note ::

            All operators here are guaranteed to satisfy
            :meth:`grudge.symbolic.operators.DiffOperatorBase.
            equal_except_for_axis`.

    :ivar field:
    """

    def get_assignees(self):
        return frozenset(self.names)

    @memoize_method
    def get_dependencies(self):
        return _make_dep_mapper(include_subscripts=False)(self.field)

    def __str__(self):
        lines = []

        if len(self.names) > 1:
            lines.append("{")
            for n, d in zip(self.names, self.operators):
                lines.append("  %s <- %s(%s)" % (n, d, self.field))
            lines.append("}")
        else:
            for n, d in zip(self.names, self.operators):
                lines.append("%s <- %s(%s)" % (n, d, self.field))

        return "\n".join(lines)

    def get_execution_method(self, exec_mapper):
        return exec_mapper.exec_diff_batch_assign


class FluxExchangeBatchAssign(Instruction):
    """
    .. attribute:: names
    .. attribute:: indices_and_ranks
    .. attribute:: rank_to_index_and_name
    .. attribute:: arg_fields
    """

    priority = 1

    def __init__(self, names, indices_and_ranks, arg_fields):
        rank_to_index_and_name = {}
        for name, (index, rank) in zip(
                names, indices_and_ranks):
            rank_to_index_and_name.setdefault(rank, []).append(
                (index, name))

        Instruction.__init__(self,
                names=names,
                indices_and_ranks=indices_and_ranks,
                rank_to_index_and_name=rank_to_index_and_name,
                arg_fields=arg_fields)

    def get_assignees(self):
        return set(self.names)

    @memoize_method
    def get_dependencies(self):
        dep_mapper = _make_dep_mapper()
        result = set()
        for fld in self.arg_fields:
            result |= dep_mapper(fld)
        return result

    def __str__(self):
        lines = []

        lines.append("{")
        for n, (index, rank) in zip(self.names, self.indices_and_ranks):
            lines.append("  %s <- receive index %s from rank %d [%s]" % (
                n, index, rank, self.arg_fields))
        lines.append("}")

        return "\n".join(lines)

    def get_execution_method(self, exec_mapper):
        return exec_mapper.exec_flux_exchange_batch_assign

# }}}


# {{{ graphviz/dot dataflow graph drawing

def dot_dataflow_graph(code, max_node_label_length=30,
        label_wrap_width=50):
    origins = {}
    node_names = {}

    result = [
            "initial [label=\"initial\"]"
            "result [label=\"result\"]"]

    for num, insn in enumerate(code.instructions):
        node_name = "node%d" % num
        node_names[insn] = node_name
        node_label = str(insn)

        if max_node_label_length is not None:
            node_label = node_label[:max_node_label_length]

        if label_wrap_width is not None:
            from pytools import word_wrap
            node_label = word_wrap(node_label, label_wrap_width,
                    wrap_using="\n      ")

        node_label = node_label.replace("\n", "\\l") + "\\l"

        result.append("%s [ label=\"p%d: %s\" shape=box ];" % (
            node_name, insn.priority, node_label))

        for assignee in insn.get_assignees():
            origins[assignee] = node_name

    def get_orig_node(expr):
        from pymbolic.primitives import Variable
        if isinstance(expr, Variable):
            return origins.get(expr.name, "initial")
        else:
            return "initial"

    def gen_expr_arrow(expr, target_node):
        result.append("%s -> %s [label=\"%s\"];"
                % (get_orig_node(expr), target_node, expr))

    for insn in code.instructions:
        for dep in insn.get_dependencies():
            gen_expr_arrow(dep, node_names[insn])

    from pytools.obj_array import is_obj_array

    if is_obj_array(code.result):
        for subexp in code.result:
            gen_expr_arrow(subexp, "result")
    else:
        gen_expr_arrow(code.result, "result")

    return "digraph dataflow {\n%s\n}\n" % "\n".join(result)

# }}}


# {{{ code representation

class Code(object):
    def __init__(self, instructions, result):
        self.instructions = instructions
        self.result = result
        self.last_schedule = None
        self.static_schedule_attempts = 5

    def dump_dataflow_graph(self):
        from grudge.tools import open_unique_debug_file

        open_unique_debug_file("dataflow", ".dot")\
                .write(dot_dataflow_graph(self, max_node_label_length=None))

    def __str__(self):
        var_to_writer = dict(
                (var_name, insn)
                for insn in self.instructions
                for var_name in insn.get_assignees())

        # {{{ topological sort

        added_insns = set()
        ordered_insns = []

        def insert_insn(insn):
            if insn in added_insns:
                return

            for dep in insn.get_dependencies():
                try:
                    writer = var_to_writer[dep.name]
                except KeyError:
                    # input variables won't be found
                    pass
                else:
                    insert_insn(writer)

            ordered_insns.append(insn)
            added_insns.add(insn)

        for insn in self.instructions:
            insert_insn(insn)

        assert len(ordered_insns) == len(self.instructions)
        assert len(added_insns) == len(self.instructions)

        # }}}

        lines = []
        for insn in ordered_insns:
            lines.extend(str(insn).split("\n"))
        lines.append("RESULT: " + str(self.result))

        return "\n".join(lines)

    # {{{ dynamic scheduler (generates static schedules by self-observation)

    class NoInstructionAvailable(Exception):
        pass

    @memoize_method
    def get_next_step(self, available_names, done_insns):
        from pytools import all, argmax2
        available_insns = [
                (insn, insn.priority) for insn in self.instructions
                if insn not in done_insns
                and all(dep.name in available_names
                    for dep in insn.get_dependencies())]

        if not available_insns:
            raise self.NoInstructionAvailable

        from pytools import flatten
        discardable_vars = set(available_names) - set(flatten(
            [dep.name for dep in insn.get_dependencies()]
            for insn in self.instructions
            if insn not in done_insns))

        # {{{ make sure results do not get discarded

        from pytools.obj_array import with_object_array_or_scalar

        dm = mappers.DependencyMapper(composite_leaves=False)

        def remove_result_variable(result_expr):
            # The extra dependency mapper run is necessary
            # because, for instance, subscripts can make it
            # into the result expression, which then does
            # not consist of just variables.

            for var in dm(result_expr):
                assert isinstance(var, Variable)
                discardable_vars.discard(var.name)

        with_object_array_or_scalar(remove_result_variable, self.result)

        # }}}

        return argmax2(available_insns), discardable_vars

    def execute_dynamic(self, exec_mapper, pre_assign_check=None):
        """Execute the instruction stream, make all scheduling decisions
        dynamically. Record the schedule in *self.last_schedule*.
        """
        schedule = []

        context = exec_mapper.context

        next_future_id = 0
        futures = []
        done_insns = set()

        force_future = False

        while True:
            insn = None
            discardable_vars = []

            # check futures for completion

            i = 0
            while i < len(futures):
                future = futures[i]
                if force_future or future.is_ready():
                    futures.pop(i)

                    insn = self.EvaluateFuture(future.id)

                    assignments, new_futures = future()
                    force_future = False
                    break
                else:
                    i += 1

                del future

            # if no future got processed, pick the next insn
            if insn is None:
                try:
                    insn, discardable_vars = self.get_next_step(
                            frozenset(list(context.keys())),
                            frozenset(done_insns))

                except self.NoInstructionAvailable:
                    if futures:
                        # no insn ready: we need a future to complete to continue
                        force_future = True
                    else:
                        # no futures, no available instructions: we're done
                        break
                else:
                    for name in discardable_vars:
                        del context[name]

                    done_insns.add(insn)
                    assignments, new_futures = \
                            insn.get_execution_method(exec_mapper)(insn)

            if insn is not None:
                for target, value in assignments:
                    if pre_assign_check is not None:
                        pre_assign_check(target, value)

                    context[target] = value

                futures.extend(new_futures)

                schedule.append((discardable_vars, insn, len(new_futures)))

                for future in new_futures:
                    future.id = next_future_id
                    next_future_id += 1

        if len(done_insns) < len(self.instructions):
            print("Unreachable instructions:")
            for insn in set(self.instructions) - done_insns:
                print("    ", insn)

            raise RuntimeError("not all instructions are reachable"
                    "--did you forget to pass a value for a placeholder?")

        if self.static_schedule_attempts:
            self.last_schedule = schedule

        from pytools.obj_array import with_object_array_or_scalar
        return with_object_array_or_scalar(exec_mapper, self.result)

    # }}}

    # {{{ static schedule execution
    class EvaluateFuture(object):
        """A fake 'instruction' that represents evaluation of a future."""
        def __init__(self, future_id):
            self.future_id = future_id

    def execute(self, exec_mapper, pre_assign_check=None):
        """If we have a saved, static schedule for this instruction stream,
        execute it. Otherwise, punt to the dynamic scheduler below.
        """

        if self.last_schedule is None:
            return self.execute_dynamic(exec_mapper, pre_assign_check)

        context = exec_mapper.context
        id_to_future = {}
        next_future_id = 0

        schedule_is_delay_free = True

        for discardable_vars, insn, new_future_count in self.last_schedule:
            for name in discardable_vars:
                del context[name]

            if isinstance(insn, self.EvaluateFuture):
                future = id_to_future.pop(insn.future_id)
                if not future.is_ready():
                    schedule_is_delay_free = False
                assignments, new_futures = future()
                del future
            else:
                assignments, new_futures = \
                        insn.get_execution_method(exec_mapper)(insn)

            for target, value in assignments:
                if pre_assign_check is not None:
                    pre_assign_check(target, value)

                context[target] = value

            if len(new_futures) != new_future_count:
                raise RuntimeError("static schedule got an unexpected number "
                        "of futures")

            for future in new_futures:
                id_to_future[next_future_id] = future
                next_future_id += 1

        if not schedule_is_delay_free:
            self.last_schedule = None
            self.static_schedule_attempts -= 1

        from pytools.obj_array import with_object_array_or_scalar
        return with_object_array_or_scalar(exec_mapper, self.result)

    # }}}

# }}}


# {{{ compiler

class CodeGenerationState(Record):
    """
    .. attribute:: generating_discr_code
    """

    def get_code_list(self, compiler):
        if self.generating_discr_code:
            return compiler.discr_code
        else:
            return compiler.eval_code


class OperatorCompiler(mappers.IdentityMapper):
    def __init__(self, discr, prefix="_expr", max_vectors_in_batch_expr=None):
        super(OperatorCompiler, self).__init__()
        self.prefix = prefix

        self.max_vectors_in_batch_expr = max_vectors_in_batch_expr

        self.discr_code = []
        self.discr_scope_names_created = set()
        self.discr_scope_names_copied_to_eval = set()

        self.eval_code = []
        self.expr_to_var = {}

        self.assigned_names = set()

        self.discr = discr

        from pytools import UniqueNameGenerator
        self.name_gen = UniqueNameGenerator()

    # {{{ collect various optemplate components

    def collect_diff_ops(self, expr):
        return mappers.BoundOperatorCollector(sym.RefDiffOperatorBase)(expr)

    # }}}

    # {{{ top-level driver

    def __call__(self, expr):
        # Put the result expressions into variables as well.
        expr = sym.cse(expr, "_result")

        # from grudge.symbolic.mappers.type_inference import TypeInferrer
        # self.typedict = TypeInferrer()(expr)

        # Used for diff batching
        self.diff_ops = self.collect_diff_ops(expr)

        codegen_state = CodeGenerationState(generating_discr_code=False)
        # Finally, walk the expression and build the code.
        result = super(OperatorCompiler, self).__call__(expr, codegen_state)

        from pytools.obj_array import make_obj_array
        return (
                Code(self.discr_code,
                    make_obj_array(
                        [Variable(name)
                            for name in self.discr_scope_names_copied_to_eval])),
                Code(
                    # FIXME: Enable
                    #self.aggregate_assignments(self.eval_code, result),
                    self.eval_code,
                    result))

    # }}}

    # {{{ variables and names

    def assign_to_new_var(self, codegen_state, expr, priority=0, prefix=None):
        # Observe that the only things that can be legally subscripted in
        # grudge are variables. All other expressions are broken down into
        # their scalar components.
        if isinstance(expr, (Variable, Subscript)):
            return expr

        new_name = self.name_gen(prefix if prefix is not None else "expr")
        codegen_state.get_code_list(self).append(Assign(
            (new_name,), (expr,), priority=priority))

        return Variable(new_name)

    # }}}

    # {{{ map_xxx routines

    def map_common_subexpression(self, expr, codegen_state):
        def get_rec_child(codegen_state):
            if isinstance(expr.child, sym.OperatorBinding):
                # We need to catch operator bindings here and
                # treat them specially. They get assigned to their
                # own variable by default, which would mean the
                # CSE prefix would be omitted, making the resulting
                # code less readable.
                return self.map_operator_binding(
                        expr.child, codegen_state, name_hint=expr.prefix)
            else:
                return self.rec(expr.child, codegen_state)

        if expr.scope == sym.cse_scope.DISCRETIZATION:
            from pymbolic import var
            try:
                expr_name = self.discr._discr_scoped_subexpr_to_name[expr.child]
            except KeyError:
                expr_name = "discr." + self.discr._discr_scoped_name_gen(
                        expr.prefix if expr.prefix is not None else "expr")
                self.discr._discr_scoped_subexpr_to_name[expr.child] = expr_name

            assert expr_name.startswith("discr.")

            priority = getattr(expr, "priority", 0)

            if expr_name not in self.discr_scope_names_created:
                new_codegen_state = codegen_state.copy(generating_discr_code=True)
                rec_child = get_rec_child(new_codegen_state)

                new_codegen_state.get_code_list(self).append(
                        ToDiscretizationScopedAssign(
                            (expr_name,), (rec_child,), priority=priority))

                self.discr_scope_names_created.add(expr_name)

            if codegen_state.generating_discr_code:
                return var(expr_name)
            else:
                if expr_name in self.discr_scope_names_copied_to_eval:
                    return var(expr_name)

                self.eval_code.append(
                        FromDiscretizationScopedAssign(
                            expr_name, priority=priority))

                self.discr_scope_names_copied_to_eval.add(expr_name)

                return var(expr_name)

        else:
            try:
                return self.expr_to_var[expr.child]
            except KeyError:
                priority = getattr(expr, "priority", 0)

                rec_child = get_rec_child(codegen_state)

                cse_var = self.assign_to_new_var(
                        codegen_state, rec_child,
                        priority=priority, prefix=expr.prefix)

                self.expr_to_var[expr.child] = cse_var
                return cse_var

    def map_operator_binding(self, expr, codegen_state, name_hint=None):
        if isinstance(expr.op, sym.RefDiffOperatorBase):
            return self.map_ref_diff_op_binding(expr, codegen_state)
        else:
            # make sure operator assignments stand alone and don't get muddled
            # up in vector math
            field_var = self.assign_to_new_var(
                    codegen_state,
                    self.rec(expr.field, codegen_state))
            result_var = self.assign_to_new_var(
                    codegen_state,
                    expr.op(field_var),
                    prefix=name_hint)
            return result_var

    def map_call(self, expr, codegen_state):
        from grudge.symbolic.primitives import CFunction
        if isinstance(expr.function, CFunction):
            return super(OperatorCompiler, self).map_call(expr, codegen_state)
        else:
            # If it's not a C-level function, it shouldn't get muddled up into
            # a vector math expression.

            return self.assign_to_new_var(
                    codegen_state,
                    type(expr)(
                        expr.function,
                        [self.assign_to_new_var(
                            codegen_state,
                            self.rec(par, codegen_state))
                            for par in expr.parameters]))

    def map_ref_diff_op_binding(self, expr, codegen_state):
        try:
            return self.expr_to_var[expr]
        except KeyError:
            all_diffs = [diff
                    for diff in self.diff_ops
                    if diff.op.equal_except_for_axis(expr.op)
                    and diff.field == expr.field]

            names = [self.name_gen("expr") for d in all_diffs]

            from pytools import single_valued
            op_class = single_valued(type(d.op) for d in all_diffs)

            codegen_state.get_code_list(self).append(
                    DiffBatchAssign(
                        names=names,
                        op_class=op_class,
                        operators=[d.op for d in all_diffs],
                        field=self.rec(
                            single_valued(d.field for d in all_diffs),
                            codegen_state)))

            from pymbolic import var
            for n, d in zip(names, all_diffs):
                self.expr_to_var[d] = var(n)

            return self.expr_to_var[expr]

    # }}}

    # {{{ assignment aggregration pass

    def aggregate_assignments(self, instructions, result):
        from pymbolic.primitives import Variable

        # {{{ aggregation helpers

        def get_complete_origins_set(insn, skip_levels=0):
            if skip_levels < 0:
                skip_levels = 0

            result = set()
            for dep in insn.get_dependencies():
                if isinstance(dep, Variable):
                    dep_origin = origins_map.get(dep.name, None)
                    if dep_origin is not None:
                        if skip_levels <= 0:
                            result.add(dep_origin)
                        result |= get_complete_origins_set(
                                dep_origin, skip_levels-1)

            return result

        var_assignees_cache = {}

        def get_var_assignees(insn):
            try:
                return var_assignees_cache[insn]
            except KeyError:
                result = set(Variable(assignee)
                        for assignee in insn.get_assignees())
                var_assignees_cache[insn] = result
                return result

        def aggregate_two_assignments(ass_1, ass_2):
            names = ass_1.names + ass_2.names

            from pymbolic.primitives import Variable
            deps = (ass_1.get_dependencies() | ass_2.get_dependencies()) \
                    - set(Variable(name) for name in names)

            return Assign(
                    names=names, exprs=ass_1.exprs + ass_2.exprs,
                    _dependencies=deps,
                    priority=max(ass_1.priority, ass_2.priority))

        # }}}

        # {{{ main aggregation pass

        origins_map = dict(
                    (assignee, insn)
                    for insn in instructions
                    for assignee in insn.get_assignees())

        from pytools import partition
        unprocessed_assigns, other_insns = partition(
                # FIXME: Re-add check for scalar result, exclude
                lambda insn: isinstance(insn, Assign),
                instructions)

        # filter out zero-flop-count assigns--no need to bother with those
        processed_assigns, unprocessed_assigns = partition(
                lambda ass: ass.flop_count() == 0,
                unprocessed_assigns)

        # filter out zero assignments
        from pytools import any
        from grudge.tools import is_zero

        i = 0

        while i < len(unprocessed_assigns):
            my_assign = unprocessed_assigns[i]
            if any(is_zero(expr) for expr in my_assign.exprs):
                processed_assigns.append(unprocessed_assigns.pop())
            else:
                i += 1

        # greedy aggregation
        while unprocessed_assigns:
            my_assign = unprocessed_assigns.pop()

            my_deps = my_assign.get_dependencies()
            my_assignees = get_var_assignees(my_assign)

            agg_candidates = []
            for i, other_assign in enumerate(unprocessed_assigns):
                other_deps = other_assign.get_dependencies()
                other_assignees = get_var_assignees(other_assign)

                if ((my_deps & other_deps
                        or my_deps & other_assignees
                        or other_deps & my_assignees)
                        and my_assign.priority == other_assign.priority):
                    agg_candidates.append((i, other_assign))

            did_work = False

            if agg_candidates:
                my_indirect_origins = get_complete_origins_set(
                        my_assign, skip_levels=1)

                for other_assign_index, other_assign in agg_candidates:
                    if self.max_vectors_in_batch_expr is not None:
                        new_assignee_count = len(
                                set(my_assign.get_assignees())
                                | set(other_assign.get_assignees()))
                        new_dep_count = len(
                                my_assign.get_dependencies(
                                    each_vector=True)
                                | other_assign.get_dependencies(
                                    each_vector=True))

                        if (new_assignee_count + new_dep_count
                                > self.max_vectors_in_batch_expr):
                            continue

                    other_indirect_origins = get_complete_origins_set(
                            other_assign, skip_levels=1)

                    if (my_assign not in other_indirect_origins and
                            other_assign not in my_indirect_origins):
                        did_work = True

                        # aggregate the two assignments
                        new_assignment = aggregate_two_assignments(
                                my_assign, other_assign)
                        del unprocessed_assigns[other_assign_index]
                        unprocessed_assigns.append(new_assignment)
                        for assignee in new_assignment.get_assignees():
                            origins_map[assignee] = new_assignment

                        break

            if not did_work:
                processed_assigns.append(my_assign)

        externally_used_names = set(
                expr
                for insn in processed_assigns + other_insns
                for expr in insn.get_dependencies())

        from pytools.obj_array import is_obj_array
        if is_obj_array(result):
            externally_used_names |= set(expr for expr in result)
        else:
            externally_used_names |= set([result])

        def schedule_and_finalize_assignment(ass):
            dep_mapper = _make_dep_mapper(include_subscripts=False)

            names_exprs = list(zip(ass.names, ass.exprs))

            my_assignees = set(name for name, expr in names_exprs)
            names_exprs_deps = [
                    (name, expr,
                        set(dep.name for dep in dep_mapper(expr) if
                            isinstance(dep, Variable)) & my_assignees)
                    for name, expr in names_exprs]

            ordered_names_exprs = []
            available_names = set()

            while names_exprs_deps:
                schedulable = []

                i = 0
                while i < len(names_exprs_deps):
                    name, expr, deps = names_exprs_deps[i]

                    unsatisfied_deps = deps - available_names

                    if not unsatisfied_deps:
                        schedulable.append((str(expr), name, expr))
                        del names_exprs_deps[i]
                    else:
                        i += 1

                # make sure these come out in a constant order
                schedulable.sort()

                if schedulable:
                    for key, name, expr in schedulable:
                        ordered_names_exprs.append((name, expr))
                        available_names.add(name)
                else:
                    raise RuntimeError("aggregation resulted in an "
                            "impossible assignment")

            return self.finalize_multi_assign(
                    names=[name for name, expr in ordered_names_exprs],
                    exprs=[expr for name, expr in ordered_names_exprs],
                    do_not_return=[Variable(name) not in externally_used_names
                        for name, expr in ordered_names_exprs],
                    priority=ass.priority)

        return [schedule_and_finalize_assignment(ass)
            for ass in processed_assigns] + other_insns

        # }}}

    # }}}

    def finalize_multi_assign(self, names, exprs, do_not_return, priority):
        return Assign(names=names, exprs=exprs,
                do_not_return=do_not_return,
                priority=priority)

# }}}


# vim: foldmethod=marker
