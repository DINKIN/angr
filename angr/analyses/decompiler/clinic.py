from collections import defaultdict
import logging

import networkx

import ailment

from ...knowledge_base import KnowledgeBase
from ...codenode import BlockNode
from ...utils import timethis
from .. import Analysis, register_analysis
from ..reaching_definitions.constants import OP_BEFORE, OP_AFTER
from .ailgraph_walker import AILGraphWalker
from .optimization_passes import get_default_optimization_passes


l = logging.getLogger(name=__name__)


class Clinic(Analysis):
    """
    A Clinic deals with AILments.
    """
    def __init__(self, func,
                 remove_dead_memdefs=True,
                 sp_tracker_track_memory=True,
                 optimization_passes=None,
                 ):

        # Delayed import
        import ailment.analyses  # pylint:disable=redefined-outer-name,unused-import,import-outside-toplevel

        if not func.normalized:
            raise ValueError("Decompilation must work on normalized function graphs.")

        self.function = func

        self.graph = None

        self._ail_manager = None
        self._blocks_by_addr_and_size = { }

        self._remove_dead_memdefs = remove_dead_memdefs
        self._sp_tracker_track_memory = sp_tracker_track_memory

        # sanity checks
        if not self.kb.functions:
            l.warning('No function is available in kb.functions. It will lead to a suboptimal conversion result.')

        if optimization_passes is not None:
            self._optimization_passes = optimization_passes
        else:
            self._optimization_passes = get_default_optimization_passes(self.project.arch, self.project.simos.name)
            l.debug("Get %d optimization passes for the current binary.", len(self._optimization_passes))

        self._analyze()

    #
    # Public methods
    #

    def block(self, addr, size):
        """
        Get the converted block at the given specific address with the given size.

        :param int addr:
        :param int size:
        :return:
        """

        try:
            return self._blocks_by_addr_and_size[(addr, size)]
        except KeyError:
            return None

    def dbg_repr(self):
        """

        :return:
        """

        s = ""

        for block in sorted(self.graph.nodes(), key=lambda x: x.addr):
            s += str(block) + "\n\n"

        return s

    #
    # Private methods
    #

    def _analyze(self):

        # Make sure calling conventions of all functions have been recovered
        self._recover_calling_conventions()

        # initialize the AIL conversion manager
        self._ail_manager = ailment.Manager(arch=self.project.arch)

        # Track stack pointers
        spt = self._track_stack_pointers()

        # Convert VEX blocks to AIL blocks and then simplify them

        self._convert_all()
        ail_graph = self._simplify_blocks(stack_pointer_tracker=spt)

        # Recover variables on AIL blocks
        self._recover_and_link_variables()  # it does not change the AIL graph

        # clear _blocks_by_addr_and_size so no one can use it again
        # TODO: Totally remove this dict
        self._blocks_by_addr_and_size = None

        # Make call-sites
        self._make_callsites(ail_graph, stack_pointer_tracker=spt)

        # Simplify the entire function
        self._simplify_function(ail_graph)

        # Run simplification passes
        ail_graph = self._run_simplification_passes(ail_graph)

        self.graph = ail_graph

    @timethis
    def _recover_calling_conventions(self):
        self.project.analyses.CompleteCallingConventions()

    @timethis
    def _track_stack_pointers(self):
        """
        For each instruction, track its stack pointer offset and stack base pointer offset.

        :return: None
        """

        regs = {self.project.arch.sp_offset}
        if hasattr(self.project.arch, 'bp_offset') and self.project.arch.bp_offset is not None:
            regs.add(self.project.arch.bp_offset)
        spt = self.project.analyses.StackPointerTracker(self.function, regs, track_memory=self._sp_tracker_track_memory)
        if spt.inconsistent_for(self.project.arch.sp_offset):
            l.warning("Inconsistency found during stack pointer tracking. Decompilation results might be incorrect.")
        return spt

    @timethis
    def _convert_all(self):
        """
        Convert all VEX blocks in the function graph to AIL blocks, and fill self._blocks.

        :return:    None
        """

        for block_node in self.function.graph.nodes():
            ail_block = self._convert(block_node)

            if type(ail_block) is ailment.Block:
                self._blocks_by_addr_and_size[(block_node.addr, block_node.size)] = ail_block

    def _convert(self, block_node):
        """
        Convert a VEX block to an AIL block.

        :param block_node:  A BlockNode instance.
        :return:            An converted AIL block.
        :rtype:             ailment.Block
        """

        if not type(block_node) is BlockNode:
            return block_node

        block = self.project.factory.block(block_node.addr, block_node.size)

        ail_block = ailment.IRSBConverter.convert(block.vex, self._ail_manager)
        return ail_block

    @timethis
    def _simplify_blocks(self, stack_pointer_tracker=None):
        """
        Simplify all blocks in self._blocks.

        :param stack_pointer_tracker:   The RegisterDeltaTracker analysis instance.
        :return:                        None
        """

        # First of all, let's simplify blocks one by one

        for key in self._blocks_by_addr_and_size:
            ail_block = self._blocks_by_addr_and_size[key]
            simplified = self._simplify_block(ail_block, stack_pointer_tracker=stack_pointer_tracker)
            self._blocks_by_addr_and_size[key] = simplified

        # Update the function graph so that we can use reaching definitions
        graph = self._function_graph_to_ail_graph(self.function.graph)
        return graph

    def _simplify_block(self, ail_block, stack_pointer_tracker=None):
        """
        Simplify a single AIL block.

        :param ailment.Block ail_block: The AIL block to simplify.
        :param stack_pointer_tracker:   The RegisterDeltaTracker analysis instance.
        :return:                        A simplified AIL block.
        """

        simp = self.project.analyses.AILBlockSimplifier(
            ail_block,
            remove_dead_memdefs=self._remove_dead_memdefs,
            stack_pointer_tracker=stack_pointer_tracker,
        )
        return simp.result_block

    @timethis
    def _simplify_function(self, ail_graph):
        """
        Simplify the entire function.

        :return:    None
        """

        # Computing reaching definitions
        rd = self.project.analyses.ReachingDefinitions(subject=self.function, func_graph=ail_graph,
                                                       observe_callback=self._simplify_function_rd_observe_callback)

        simp = self.project.analyses.AILSimplifier(
            self.function,
            func_graph=ail_graph,
            remove_dead_memdefs=self._remove_dead_memdefs,
            reaching_definitions=rd
        )

        def _handler(node):
            return simp.blocks.get(node, None)

        AILGraphWalker(ail_graph, _handler, replace_nodes=True).walk()

    @timethis
    def _run_simplification_passes(self, ail_graph):

        blocks_map = defaultdict(set)

        # update blocks_map to allow node_addr to node lookup
        def _updatedict_handler(node):
            blocks_map[node.addr].add(node)
        AILGraphWalker(ail_graph, _updatedict_handler).walk()

        # Run each pass
        for pass_ in self._optimization_passes:

            analysis = getattr(self.project.analyses, pass_.__name__)

            a = analysis(self.function, blocks=blocks_map, graph=ail_graph)
            if a.out_graph:
                # use the new graph
                ail_graph = a.out_graph

        return ail_graph

    @timethis
    def _make_callsites(self, ail_graph, stack_pointer_tracker=None):
        """
        Simplify all function call statements.

        :return:    None
        """

        # Computing reaching definitions
        rd = self.project.analyses.ReachingDefinitions(subject=self.function, func_graph=ail_graph,
                                                       observe_callback=self._make_callsites_rd_observe_callback)

        def _handler(block):
            csm = self.project.analyses.AILCallSiteMaker(block, reaching_definitions=rd)
            if csm.result_block:
                ail_block = csm.result_block
                simp = self.project.analyses.AILBlockSimplifier(ail_block, stack_pointer_tracker=stack_pointer_tracker)
                return simp.result_block
            return None

        AILGraphWalker(ail_graph, _handler, replace_nodes=True).walk()

        return ail_graph

    @timethis
    def _recover_and_link_variables(self):

        # variable recovery
        tmp_kb = KnowledgeBase(self.project)
        # stack pointers have been removed at this point
        vr = self.project.analyses.VariableRecoveryFast(self.function, clinic=self, kb=tmp_kb, track_sp=False)  # pylint:disable=unused-variable

        # TODO: The current mapping implementation is kinda hackish...
        # Link variables to each statement
        for block in self._blocks_by_addr_and_size.values():
            self._link_variables_on_block(block, tmp_kb)

    def _link_variables_on_block(self, block, kb):
        """
        Link atoms (AIL expressions) in the given block to corresponding variables identified previously.

        :param ailment.Block block: The AIL block to work on.
        :return:                    None
        """

        variable_manager = kb.variables[self.function.addr]

        for stmt_idx, stmt in enumerate(block.statements):
            # I wish I could do functional programming in this method...
            stmt_type = type(stmt)
            if stmt_type is ailment.Stmt.Store:
                # find a memory variable
                mem_vars = variable_manager.find_variables_by_atom(block.addr, stmt_idx, stmt)
                if len(mem_vars) == 1:
                    stmt.variable, stmt.offset = next(iter(mem_vars))
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.data)

            elif stmt_type is ailment.Stmt.Assignment:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.dst)
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.src)

            elif stmt_type is ailment.Stmt.ConditionalJump:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.condition)

            elif stmt_type is ailment.Stmt.Call:
                if stmt.ret_expr:
                    self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.ret_expr)

    def _link_variables_on_expr(self, variable_manager, block, stmt_idx, stmt, expr):
        """
        Link atoms (AIL expressions) in the given expression to corresponding variables identified previously.

        :param variable_manager:    Variable manager of the function.
        :param ailment.Block block: AIL block.
        :param int stmt_idx:        ID of the statement.
        :param stmt:                The AIL statement that `expr` belongs to.
        :param expr:                The AIl expression to work on.
        :return:                    None
        """

        if type(expr) is ailment.Expr.Register:
            # find a register variable
            reg_vars = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            # TODO: make sure it is the correct register we are looking for
            if len(reg_vars) == 1:
                reg_var, offset = next(iter(reg_vars))
                expr.variable = reg_var
                expr.variable_offset = offset

        elif type(expr) is ailment.Expr.Load:
            # import ipdb; ipdb.set_trace()
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 0:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.addr)
            else:
                if len(variables) > 1:
                    l.error("More than one variable are available for atom %s. Consider fixing it using phi nodes.",
                            expr
                            )
                var, offset = next(iter(variables))
                expr.variable = var
                expr.variable_offset = offset

        elif type(expr) is ailment.Expr.BinaryOp:
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var, offset = next(iter(variables))
                expr.referenced_variable = var
                expr.variable_offset = offset
            else:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands[0])
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands[1])

        elif type(expr) is ailment.Expr.UnaryOp:
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var, offset = next(iter(variables))
                expr.referenced_variable = var
                expr.variable_offset = offset
            else:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands)

        elif type(expr) is ailment.Expr.Convert:
            self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operand)

        elif isinstance(expr, ailment.Expr.BasePointerOffset):
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var, offset = next(iter(variables))
                expr.referenced_variable = var
                expr.variable_offset = offset

    def _function_graph_to_ail_graph(self, func_graph):

        node_to_block_mapping = {}
        graph = networkx.DiGraph()

        for node in func_graph.nodes():
            ail_block = self._blocks_by_addr_and_size.get((node.addr, node.size), node)
            node_to_block_mapping[node] = ail_block

            if ail_block is not None:
                graph.add_node(ail_block)

        for src_node, dst_node, data in func_graph.edges(data=True):
            src = node_to_block_mapping[src_node]
            dst = node_to_block_mapping[dst_node]

            if dst is not None:
                graph.add_edge(src, dst, **data)

        return graph

    @staticmethod
    def _make_callsites_rd_observe_callback(ob_type, **kwargs):
        if ob_type != 'insn':
            return False
        stmt = kwargs.pop('stmt')
        op_type = kwargs.pop('op_type')
        return isinstance(stmt, ailment.Stmt.Call) and op_type == OP_BEFORE

    @staticmethod
    def _simplify_function_rd_observe_callback(ob_type, **kwargs):
        if ob_type != 'node':
            return False
        op_type = kwargs.pop('op_type')
        return op_type == OP_AFTER


register_analysis(Clinic, 'Clinic')
