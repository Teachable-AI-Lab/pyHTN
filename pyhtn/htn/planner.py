from abc import abstractmethod
from copy import deepcopy
from itertools import chain
import logging
import time
from dataclasses import dataclass

from pyhtn.common.imports.typing import *
from pyhtn.htn import Task, Method, Operator, TaskEx, MethodEx, OperatorEx, ExStatus
from pyhtn.htn.htn import HTN
from pyhtn.htn.matching import to_factset

# from pyhtn.domain.method import GroundedMethod
# from pyhtn.domain.method import Method
# from pyhtn.domain.operators import GroundedOperator
# from pyhtn.domain.operators import Operator
# from pyhtn.domain.task import TaskEx
# from pyhtn.domain.task import Task
from pyhtn.exceptions import FailedPlanException, StopException
from pyhtn.validation import validate_domain, validate_tasks
from pyhtn.planner.planner_logger import PlannerLogger
from pyhtn.planner.trace import Trace, TraceKind


@dataclass(eq=True, frozen=False)
class FrameContext:
    """
    Keeps track of the current position within an attempt
    to carry out a task exection
    """

    # The task execution being handled by this frame
    task_exec : TaskEx

    # The the index of the task execution in the parent frame
    task_ind : int

    # Child method or operator executions for current task execution.
    #  If one fails we can backtrack and try another
    child_execs : List[MethodEx]

    # Index of current MethodEx
    child_exec_ind : int

    # MethodEx indices already visited. Keep this list in case 
    #  for some reason we don't go through them in order
    visited_child_exec_inds : List[int]

    # Within the current MethodEx the index of the active subtask_exec
    subtask_exec_ind : int

    # The span of subtasks that this frame effectively covers 
    #   because they are part of an unordered group or are skippable
    eff_span : tuple[int,int] 

    # unord_span : tuple[int,int] 

    # MethodEx indices already visited. Keep this list in case 
    #  for some reason we don't go through them in order
    visted_subtask_exec_inds : List[int]

    can_skip_mask : List[bool]
    eff_span_sweep_ind : int

    is_operator_frame : bool
    skippables_overflow : bool
    is_all_skippable : bool
    state : dict 

    def __init__(self, state, task_exec, task_ind, child_execs,
                child_exec_ind, visited_child_exec_inds,
                subtask_exec_ind, visted_subtask_exec_inds=[]):
        self.task_exec = task_exec
        self.task_ind = task_ind
        self.child_execs = child_execs
        self.child_exec_ind = child_exec_ind
        self.visited_child_exec_inds = visited_child_exec_inds
        self.subtask_exec_ind = subtask_exec_ind
        self.visted_subtask_exec_inds = visted_subtask_exec_inds

        child = child_execs[child_exec_ind] if child_exec_ind < len(child_execs) else None
        self.is_operator_frame = isinstance(child, OperatorEx)

        self.skippables_overflow = False
        if(not self.is_operator_frame):
            self.can_execute_mask = [True] * len(child.subtask_execs)
            self.can_skip_mask = [False] * len(child.subtask_execs)
            for i, t in enumerate(child.subtask_execs):
                can_execute, can_skip = t.check_can_execute_skip(state)
                self.can_execute_mask[i] = can_execute
                self.can_skip_mask[i] = can_skip

            # self.can_skip_mask = [t.task.optional for t in child.subtask_execs]
            self._update_eff_span()
            s,e =  self.eff_span
            self.eff_span_sweep_ind = s
        else:
            self.eff_span = None
            # self.unord_span = None

        # print("MAKE FRAME", self, (self.eff_span), self.skippables_overflow)
    

    def _update_eff_span(self):
        ''' 
        For a particular subtask index, returns the effective span
        of other subtasks that are reachable within the same
        unordered group, or because subsequent subtasks are optional
        '''
        # print("> ", self)

        ind = self.subtask_exec_ind
        method_exec = self.current_method_exec
        method = method_exec.method
        subtask_execs = method_exec.subtask_execs

        spans = method.unord_spans
        span_starts = [span[0] for span in method.unord_spans]
        span_ends =   [span[1] for span in method.unord_spans]
        start, end = ind, ind+(len(method.subtasks) > 0)

        # If the cursor resides in an unorderd span make it the curr_span
        curr_span = None
        span_ind = 0
        for i, (s,e) in enumerate(spans):
            span_ind = i
            if(s < end and e >= start):
                curr_span = (s,e)
                break

        skippable = lambda i : (self.can_skip_mask[i] or 
                                subtask_execs[i].status in (ExStatus.SUCCESS, ExStatus.FAILED)
                               )
        blocker = lambda i : (not self.can_execute_mask[i] and not self.can_skip_mask[i])

        ended_on_unfin_unord = False
        while(end < len(method.subtasks)):
            # If ther is a curr_span, i.e. the end currently overlaps 
            #  with an unord_span, process it at once.
            if(curr_span is not None):
                # Extend effective span to the current unord span.
                s,e = curr_span
                end = e

                # If there is an unskippable item in this unordered span
                #  then keep track of this and break.
                if(any(not skippable(i) for i in range(s,e))):
                    ended_on_unfin_unord = True
                    break

                # Don't advance past the end
                if(end >= len(method.subtasks)):
                    break

                span_ind += 1
                end += 1

            # If the next item is skippable move the end of the effective
            #   span past it.
            elif(skippable(end-1)):
                end += 1

            # Otherwise stop
            else:
                break

            # Update the curr_span if the end now extends into a new
            #  unordered span
            curr_span = None
            if(span_ind < len(spans)):
                s,e = spans[span_ind]
                if(s < end and e >= start):
                    curr_span = (s,e)

        # Advance the start of the effective span past any completed items
        while(start < end and 
              subtask_execs[start].status not in (ExStatus.INITIALIZED, ExStatus.EXECUTABLE)):
            start += 1
            
        self.eff_span = (start, end)

        child = self.child_execs[self.child_exec_ind] if self.child_exec_ind < len(self.child_execs) else None
        self.skippables_overflow = (len(child.subtask_execs) == 0 or 
                                     (end >= len(child.subtask_execs) and
                                      skippable(end-1) and
                                      not ended_on_unfin_unord
                                    ))

        # print("self.skippables_overflow", self.skippables_overflow)

        self.is_all_skippable = (end-start) == len(child.subtask_execs)
        self.is_all_blockers = (len(child.subtask_execs) > 0 and 
                                end-start > 0 and 
                                all(blocker(i) for i in range(start, end))
                               )

        # print("< ", self, self.eff_span, self.skippables_overflow)

    def _next_child_exec_index(self):
        # TODO : Might consider policies other than trying MethodExs sequentially  
        nxt = self.child_exec_index + 1 
        if(nxt >= len(self.child_execs)):
            return None
        return nxt

    def next_child_frame(self, state):
        ind = self._next_child_exec_ind()

        if(ind is None):
            return None

        return FrameContext(
            state     =                state,
            task_exec =                self.task_exec,
            task_ind =                 self.task_ind,
            child_execs =              self.child_execs,
            child_exec_ind =           ind, 
            visited_child_exec_inds =  self.visited_child_exec_inds + [self.child_exec_ind],
            subtask_exec_ind =         0,
            visted_subtask_exec_inds = [],
        )


    def next_frame_after_commit(self, state, ind, is_success=False):
        subtask_execs = self.current_method_exec.subtask_execs
        s,e = self.eff_span
        visited_inds = self.visted_subtask_exec_inds

        # Find the unordered span that `ind` is in
        unord_spans = self.current_method_exec.method.unord_spans
        us,ue = ind, ind+1
        in_unord = False
        for _us, _ue in unord_spans:
            if(ind >= _us and ind < _ue):
                us,ue = _us, _ue
                in_unord = True
                break

        # Any not yet executed items before the start of the unordered
        # span covered by 'ind' are skipped
        for i in range(s, us):
            ex = subtask_execs[i]
            if(ex.status == ExStatus.EXECUTABLE):
                ex.status = ExStatus.SKIPPED

        # Any not yet executed items after the start of the unordered span
        #   covered by 'ind' are rechecked for executability so their statuses
        #   are reduced to INITIALIZED
        for i in range(us, e):
            if(i == ind): continue
            ex = subtask_execs[i]
            if(ex.status == ExStatus.EXECUTABLE):
                ex.status = ExStatus.INITIALIZED

        # Set the committed subtask to SUCCESS or IN_PROGRESS
        ex = subtask_execs[ind]
        ex.status = ExStatus.SUCCESS if is_success else ExStatus.IN_PROGRESS

        # The default next subtask is 'ind' if this is an 
        #  in_progress commit or ind+1 if is success commit
        nxt = ind+1 if is_success else ind

        # But if ind is in an unordered group then it is the first
        #  unresolved subtask.
        all_unord_executed = in_unord
        for i in range(us, ue):
            if(subtask_execs[i].status == ExStatus.INITIALIZED):
            # if(i not in visited_inds):
                nxt = i
                all_unord_executed = False
                break;

        # If unordered group all executed, nxt is subtask after it.
        if(all_unord_executed):
            nxt = ue

        # If `nxt` is beyond the end of the method then return None 
        if(nxt >= len(subtask_execs)):
            return None

        visited_inds = self.visted_subtask_exec_inds + [ind]

        return FrameContext(
            state     =                state,
            task_exec =                self.task_exec,
            task_ind =                 self.task_ind,
            child_execs =              self.child_execs,
            child_exec_ind =           self.child_exec_ind, 
            visited_child_exec_inds =  self.visited_child_exec_inds,
            subtask_exec_ind =         nxt,
            visted_subtask_exec_inds = visited_inds
        )

    def mark_subtask_skippable(self, ind, skippable=True):
        if(not self.can_skip_mask[ind]):
            # print("BEF MARK", self.eff_span)
            self.can_skip_mask[ind] = skippable
            self._update_eff_span()
            # print("MARK SKIPPABLE", ind, self.eff_span)

    @classmethod
    def new_frame(self, state, task_exec, child_execs, 
                    child_ind=0,
                    task_ind=0,
                    subtask_ind=0):

        return FrameContext(
            state =                    state,
            task_exec =                task_exec,
            task_ind =                 task_ind,
            child_execs =              child_execs,
            child_exec_ind =           child_ind, 
            visited_child_exec_inds =  [],
            subtask_exec_ind =         subtask_ind,
            visted_subtask_exec_inds = [],
        )

    @property
    def current_task_exec(self):
        return self.task_exec

    @property
    def current_child_exec(self):
        child_execs = self.child_execs
        child_ind = self.child_exec_ind
        if(child_execs and child_ind is not None 
            and len(child_execs) > 0):

            return child_execs[child_ind]
        else:
            return None

    @property
    def current_method_exec(self):
        child = self.current_child_exec
        assert(isinstance(child, MethodEx))
        return child

    @property
    def current_operator_exec(self):
        child = self.current_child_exec
        assert(isinstance(child, OperatorEx))
        return child

    @property
    def current_subtask_exec(self):
        if(self.is_operator_frame or not self.current_method_exec):
            return None
        subtask_execs = self.current_method_exec.subtask_execs
        ind = self.subtask_exec_ind
        if(subtask_execs and ind is not None
           and len(subtask_execs) > 0):
            return subtask_execs[ind]
        else:
            return None

    # def get_cand_next_subtask_execs(self):
    #     # s,e = self._index_eff_span(self.child_exec_index)
    #     s,e = self.eff_span

    #     subtask_execs = []
    #     method_exec = self.current_method_exec
    #     for i in range(s,e):
    #         subtask_execs.append((i, method_exec.subtask_execs[i]))
    #     return subtask_execs

    def next_subtask_exec(self):
        s,e = self.eff_span
        ind = self.eff_span_sweep_ind

        if(ind + 1 <= e):
            self.eff_span_sweep_ind += 1
            method_exec = self.current_method_exec
            return ind, method_exec.subtask_execs[ind]
        else:
            return None

        

        # subtask_execs = []
        # method_exec = self.current_method_exec
        # for i in range(s,e):
        #     subtask_execs.append((i, method_exec.subtask_execs[i]))
        # return subtask_execs

    @property
    def is_nomatch(self):
        return not self.child_execs

    def __str__(self):
        task_name = self.task_exec.task.name
        if(not self.is_operator_frame):
            s = ""
            start, end = getattr(self, "eff_span", (0,0))
            eff_span_sweep_ind = getattr(self, "eff_span_sweep_ind", 0)
            method_exec = self.current_method_exec
            unord_spans = method_exec.method.unord_spans
            unord_span_starts = [s[0] for s in unord_spans]
            unord_span_ends = [s[1] for s in unord_spans]
            subtask_execs = self.current_method_exec.subtask_execs
            in_eff_span = False
            for i in range(len(subtask_execs)):
                ex = subtask_execs[i]
                if(i == start): in_eff_span = True
                if(i == end): in_eff_span = False

                if(i == eff_span_sweep_ind):
                    s += "`"
                if(i == self.subtask_exec_ind):
                    s += "|"

                if(ex.status == ExStatus.SUCCESS):
                    s += "\033[38;5;28m" # Start Green
                    # s += "\033[96m" # Start Green
                if(ex.status == ExStatus.FAILED):
                    s += "\033[31m" # Start RED
                elif(ex.status == ExStatus.IN_PROGRESS):
                    # s += "\033[34m" # Start Blue
                    s += "\033[38;5;12m" # Start BLUE
                elif(ex.status == ExStatus.SKIPPED):
                    s += "\033[38;5;248m" # Start Grey
                # elif(in_eff_span):
                elif(ex.status == ExStatus.EXECUTABLE):
                    s += "\033[93m" # Start Yellow
                
                if(i in unord_span_starts):
                    # s += "\033[4m"  # Start Underline
                    s += "("  # Start Underline
                

                if(ex.status == ExStatus.EXECUTABLE):
                    s += "\033[1m" # Start Bold

                s += ex.task.name
                if(ex.task.optional): s += "*"

                if(i+1 in unord_span_ends):
                    # s += "\033[24m" # End Underline
                    s += ")"

                next_caret = i+1 == eff_span_sweep_ind or i+1 == self.subtask_exec_ind
                if(i != len(subtask_execs)-1 and not next_caret):
                    s += ", "

                s += "\033[39m" # End Color
                s += "\033[22m" # End Bold

                
                # if(i == end):
                #     in_eff_span = False
                #     s += "\033[39m" # End Green
                

                


            return f"{task_name} -> {s}"
        else:    
            return f"{self.current_operator_exec}"

    __repr__ = __str__


class Cursor:
    def __init__(self, trace=None):
        # Current task execution being processed
        self.trace = Trace() if trace is None else trace
        self.reset()

    def reset(self):
        self.current_frame = None
        self.stack = []

    def copy(self):
        new_cursor = Cursor()
        new_cursor.trace = self.trace
        new_cursor.stack = [*self.stack]
        new_cursor.current_frame = self.current_frame
        return new_cursor

    def is_at_end(self):
        return self.current_frame is None


    @property
    def current_task_exec(self):
        return self.current_frame.current_task_exec

    @property
    def current_method_exec(self):
        return self.current_frame.current_method_exec

    @property
    def current_operator_exec(self):
        return self.current_frame.current_operator_exec

    @property
    def current_subtask_exec(self):
        return self.current_frame.current_subtask_exec

    @property
    def subtask_exec_ind(self):
        return self.current_frame.subtask_exec_ind

    @property
    def is_operator_frame(self):
        return self.current_frame.is_operator_frame

    @property
    def skippables_overflow(self):
        return self.current_frame.skippables_overflow

    @property
    def is_all_skippable(self):
        return self.current_frame.is_all_skippable

    def has_unswept_parent(self):
        if(len(self.stack) > 0):
            par_frame = self.stack[-1]
            # print("::", par_frame.eff_span_sweep_ind, par_frame.eff_span)
            if(par_frame.eff_span_sweep_ind == par_frame.eff_span[0]):
                return True
        return False

    # def get_cand_next_subtask_execs(self):
    #     return self.current_frame.get_cand_next_subtask_execs()

    def next_subtask_exec(self):
        return self.current_frame.next_subtask_exec()

    def _pop_frame(self):
        if(len(self.stack) == 0):
            self.current_frame = None
            return None

        self.current_frame = self.stack[-1]
        self.stack.pop()
        return self.current_frame

    def _advance_parent_frames_to_child(self, next_state):
        curr_frame = self.current_frame
        for i in range(len(self.stack)-1, -1, -1):
            par_frame = self.stack[i]

            self.stack[i] = par_frame = par_frame.next_frame_after_commit(next_state, curr_frame.task_ind, False)

            curr_frame = par_frame

    def advance_subtask(self, next_state, trace=None):
        ''' Try to move the cursor to the next position by: 
                1. Moving it laterally to the next subtask
                2. Popping up the stack if at last subtask in method

            This is the normal movement of the cursor after a successfully
            applying an operator.  
        ''' 
        # self._advance_parent_frames_to_child()
        # curr_frame = self.current_frame

        # Beginning with an operator frame, pop up to the parent 
        assert(self.current_frame.is_operator_frame)
        # print("CF:", self.current_frame)
        task_ind = self.current_frame.task_ind
        curr_frame = self._pop_frame()

        # A root task that is itself an operator has no parent frame, so
        #  applying it finishes the root task.
        if(curr_frame is None):
            if(trace):
                trace.add(TraceKind.POP_FRAME, None, None)
            return
        # curr_frame.current_method_exec.subtask_execs[task_ind].status = ExStatus.SUCCESS
        # print(" ::", curr_frame, curr_frame.current_method_exec)
        # print("\t->>", curr_frame.current_method_exec.subtask_execs[task_ind])
        # print("advance_subtask", self)
        # curr_frame.subtask_execs[task_ind].status = ExStatus.SUCCESS

        while True:
            # Advance to next subtask exececution in current MethodEx
            
            # print()
            # If the frame is not an operator frame first try advancing the 
            #   current subtask within the current method
            # if(not curr_frame.is_operator_frame):
                # print("BEF:", curr_frame)


            # if(curr_frame.current_subtask_exec):
            #     curr_frame.current_subtask_exec.status = ExStatus.SUCCESS
            
            # print("\t!>>", curr_frame.current_method_exec.subtask_execs[task_ind])
            # print("\t cf", curr_frame)
            next_frame = curr_frame.next_frame_after_commit(next_state, task_ind, True)

            # task_ind = curr_frame.task_ind
            
            if(next_frame is not None):
                # next_frame.current_method_exec.subtask_execs[task_ind].status = ExStatus.SUCCESS
                
                # print("SUC BEF:", curr_frame)
                # print("SUC AFT:", next_frame)
                if(trace):
                    trace.add(TraceKind.ADVANCE_SUBTASK, curr_frame, next_frame)
                break
        
            # Otherwise try popping frame from stack and mark the current
            #  frame's current child as successful.
            task_ind = curr_frame.task_ind
            next_frame = self._pop_frame()
            
            # if(curr_frame.current_child_exec):
            #     curr_frame.current_child_exec.status = ExStatus.SUCCESS
            
            if(trace):
                trace.add(TraceKind.POP_FRAME, curr_frame, next_frame)
            
            # If popping up a frame yields none we are done
            if(next_frame is None):
                break

            next_frame.current_method_exec.subtask_execs[task_ind].status = ExStatus.SUCCESS
            # print("\t:>>", next_frame.current_method_exec.subtask_execs[task_ind])

            # If the popped frame is not None mark the calling task as successful
            # self._advance_from_child_commit(next_frame, curr_frame.task_ind,
            #     is_in_progress=not curr_frame.is_operator_frame)
            # subtask_execs = next_frame.current_method_exec.subtask_execs
            # subtask_execs[curr_frame.task_ind].status = ExStatus.SUCCESS
            
            # print("next_frame:", next_frame)
            # task_ind = curr_frame.task_ind
            curr_frame = next_frame
        self.current_frame = next_frame

        # print("current_frame: ", self.current_frame)

        self._advance_parent_frames_to_child(next_state)

        

    def push_task_exec(self, state, task_exec, task_ind, child_execs, child_ind=0, trace=None):
        ''' Recurse into a new frame from a task execution and
            its list of method executions
        ''' 
        # Push current frame 
        if(self.current_frame is not None):
            self.stack.append(self.current_frame)

        # Make a new frame pointing at the selected MethodEx
        curr_frame = self.current_frame        
        next_frame = FrameContext.new_frame(state, task_exec, child_execs, child_ind, task_ind)
        next_frame.current_child_exec.status = ExStatus.IN_PROGRESS
        # if()
        # if(trace):
        #     trace.add(TraceKind.SELECT_METHOD,       curr_frame, next_frame)
        #     trace.add(TraceKind.FIRST_SUBTASK, curr_frame, next_frame)
        self.current_frame = next_frame



    def backtrack(self, state, trace_kind=None, trace=None):
        ''' Pop frame off the stack and try the next method execution.
            Keep popping up stack until a method execution is found.
            This is the normal movement of the cursor after failing to 
            apply an operator as part of a subtask sequence.
        '''
        next_frame = None
        while True:
            curr_frame = self.current_frame
            if(curr_frame is not None):
                curr_frame.current_task_exec.status = ExStatus.FAILED
                if(curr_frame.current_method_exec):
                    curr_frame.current_method_exec.status = ExStatus.FAILED
                if(curr_frame.current_subtask_exec):
                    curr_frame.current_subtask_exec.status = ExStatus.FAILED
                next_frame = self._pop_frame()

            if(trace):
                trace.add(trace_kind, curr_frame, next_frame)

            if not next_frame:
                return False
            
            # Try next possible MethodEx
            curr_frame = self.current_frame
            next_frame = self.current_frame.next_child_frame(state)
            if(next_frame is not None):
                if(trace):
                    trace.add(TraceKind.SELECT_METHOD,       curr_frame, next_frame)
                    trace.add(TraceKind.FIRST_SUBTASK, curr_frame, next_frame)
                break

            # If no more MethodEx then pop up again
            trace_kind = TraceKind.BACKTRACK_CHILD_CASCADE

            self.current_frame = next_frame
        return True

    def mark_parent_skippable(self, skippable=True):
        if(len(self.stack) > 0):
            curr_frame = self.current_frame

            # print("curr_frame", curr_frame)
            for par_frame in reversed(self.stack):
                
                did_skip_overflow = par_frame.skippables_overflow
                par_frame.mark_subtask_skippable(curr_frame.task_ind, skippable)
                # print("par_frame", par_frame, par_frame.skippables_overflow)
                if(not par_frame.skippables_overflow or did_skip_overflow):
                    # print("BREAK")
                    break
                curr_frame = par_frame


    # def branch_skip_overflow(self):
    #     ''' In the event that the effective span of next tasks in this frame
    #         overflows past the last task, (i.e. if the last remaining task turns 
    #         out to be skippable), make a new branching cursor pointing to a
    #         position in the parent frame beyond the parent task.
    #     '''
    #     curr_frame = self.current_frame

    #     # Make a copy
    #     cursor = self.copy()

    #     # Pop copy's current frame
    #     curr_frame = cursor._pop_frame()

    #     # Pop copy's parent frame
    #     par_frame = cursor._pop_frame()

    #     # Push a new frame with the cursor set beyond 
    #     ind = curr_frame.task_ind + 1
    #     cursor.stack.push(FrameContext(
    #         task_exec =                par_frame.task_exec,
    #         task_ind =                 par_frame.task_ind,
    #         child_execs =              par_frame.child_execs,
    #         child_exec_ind =           par_frame.child_exec_ind, 
    #         visited_child_exec_inds =  par_frame.visited_child_exec_inds,
    #         subtask_exec_ind =         ind,
    #         visted_subtask_exec_inds = par_frame.visted_subtask_exec_inds,
    #     ))

        # cursor.current_frame.








    def user_select_method_exec(self, state, method_ind=0, trace=None):
        # Make a new frame pointing at the selected MethodEx
        curr_frame = self.current_frame

        if(curr_frame.current_method_exec):
            curr_frame.current_method_exec.status = ExStatus.INITIALIZED

        next_frame = FrameContext.new_frame(
            state,
            curr_frame.task_exec,
            curr_frame.child_execs,
            method_ind,
            curr_frame.task_ind)

        next_frame.current_method_exec.status = ExStatus.IN_PROGRESS

        if(trace):
            trace.add(TraceKind.USER_SELECT_METHOD,  curr_frame, next_frame)
            trace.add(TraceKind.FIRST_SUBTASK, curr_frame, next_frame)

        self.current_frame = next_frame

    def push_nomatch_frame(self, state, task_exec, task_ind, method_execs, trace=None):
        ''' Recurse into an nomatch frame this is a kind of 
            frame where method_execs is None or []. In normal planning
            this is an invalid frame. When using the planner 
            interactively (like in VAL) this is a valid state in which
            is used in 
        ''' 
        curr_frame = self.current_frame

        # Push current frame 
        if(self.current_frame is not None):
            self.stack.append(self.current_frame)

        next_frame = FrameContext.new_frame(
            state,
            task_exec,
            method_execs,
            None)

        if(trace):
            trace.add(TraceKind.ENTER_NOMATCH_FRAME, curr_frame, next_frame)

        self.current_frame = next_frame

    def add_method_exec(self, method_exec, task_exec=None, trace=None):
        # Resolve the frame associated with task_exec
        task_frame = None
        if(task_exec is None):
            task_frame = self.current_frame
        else:
            for frame in [self.current_frame, *self.stack]:
                if(task_exec is frame.current_task_exec):
                    task_frame = frame
                    break

        if(task_frame is None):
            raise ValueError(f"No frame in plan stack associated with {task_exec}.")

        # Add the method_exec to the possibilities in the frame
        if(task_frame.child_execs is None):
            task_frame.child_execs = []
        task_frame.child_execs.append(method_exec)

        if(trace):
            trace.add(TraceKind.USER_ADD_METHOD, method_exec, task_frame)


    def print(self):
        """
        Print the current state of the cursor in a readable format.
        """
        print("========== CURSOR STATE ==========")

        frame = self.current_frame
        if(self.is_at_end()):
            print("-- Cursor reached end of its root task -- ")
            return

        te = self.current_task_exec
        if(te):
            # TODO: status
            # print(f"Current TaskEx: {te} (Status: {te.status})")
            print(f"Current TaskEx: {te}")
        else:
            print(f"Current TaskEx: {te}")

        me = self.current_method_exec
        ste = self.current_subtask_exec
        st_ind = frame.subtask_exec_ind
        if me:
            print(f"Current MethodEx: {me}")
            print(f"Current SubtaskEx: {ste} ({st_ind+1}/{len(me.subtask_execs)})")
        else:
            print("Current Method: None")
            print("Current Subtask Index: N/A")

        print(f"Possible MethodExs: {len(frame.child_execs)}")
        print(f"Current Method Index: {frame.child_exec_ind}")
        print(f"Stack Depth: {len(self.stack)}")

        if self.stack:
            print("-- Stack (most recent first) --")
            for i, stack_frame in enumerate(reversed(self.stack[-3:])):
                te = frame.current_task_exec
                me = frame.current_method_exec
                ste = frame.current_subtask_exec

                print(f"  {i}: {te}, '{me}', {ste}")

            if len(self.stack) > 3:
                print(f"  ... and {len(self.stack) - 3} more")

        print("==================================\n")

    def __str__(self):
        return f"Cursor(depth={len(self.stack)}, {self.current_frame})"

    __repr__ = __str__


class TaskManager:
    def __init__(self, choice_criterion: str = 'random'):
        self.queue = []
        self.options = ()
        self.choice_criterion = choice_criterion

    def flatten(self):
        """
        Convert partially ordered task list into total ordered task list
        based on choice resolution criteria.
        """
        pass

    @abstractmethod
    def get_next_task(self):
        if not self.options:
            if not self.queue:
                return None
            self.options = tuple(self._get_next_tasks(self.queue))
        if self.options:
            if self.choice_criterion == 'random':
                pass


    def _get_next_tasks(self, tasks: Union[List, Tuple]) -> Union[List, Tuple]:
        """
        Returns list/tuple of tasks which no other task is constrained to precede.
        """
        if isinstance(tasks, list) and not isinstance(tasks[0], (list, tuple)):
            return list([tasks.pop()])
        elif isinstance(tasks, list):
            return self._get_next_tasks(tasks[0])
        elif isinstance(tasks, tuple):
            return tuple(
                chain.from_iterable(self._get_next_tasks(t) if isinstance(t, (list, tuple)) else (t,) for t in tasks))



class RootTaskQueue(TaskManager):
    def __init__(self, tasks: List[Union[Task,dict]],
                       choice_criterion: str = 'random',
                       enable_logging=False):
        """
        :param task_specs: A list of root tasks or task specifications with priorities  
        :param choice_criterion: How partially ordered tasks should be selected. One of [random, ordered, cost]
        """
        super().__init__(choice_criterion)
        self.enable_logging = enable_logging

        for task in tasks:
            self.add(task)
        

    PRIORITY_RANKS = {'first': 0, 'high': 1, 'medium': 2, 'low': 3}

    @classmethod
    def _priority_rank(cls, priority):
        if(isinstance(priority, (int, float))):
            return priority
        if(priority not in cls.PRIORITY_RANKS):
            raise ValueError(
                f"Unknown task priority {priority!r}; expected a number or "
                f"one of {list(cls.PRIORITY_RANKS)}.")
        return cls.PRIORITY_RANKS[priority]

    def add(self, task: Union[dict, tuple, Task]):
        ''' Queue a root task. Tasks are ordered by priority (a lower rank
            runs sooner), and tasks with the same priority run in the order
            they were added.
        '''
        if(isinstance(task, dict)):
            task = Task(**task)
        elif(isinstance(task, tuple)):
            task = Task(*task)

        rank = self._priority_rank(task.priority)

        # Insert after every queued task with the same or a sooner rank.
        pos = len(self.queue)
        for i, queued in enumerate(self.queue):
            if(rank < self._priority_rank(queued.priority)):
                pos = i
                break
        self.queue.insert(pos, task)

    def get_next_task(self) -> Task:
        if(self.is_empty()):
            return None
        return self.queue.pop(0)

    def is_empty(self):
        return len(self.queue) == 0

    def clear(self):
        self.queue =[]

    def __len__(self):
        return len(self.queue)

    def __iter__(self):
        return iter(self.queue)



class HTN_Planner:
    """
    This planner implements HTN (Hierarchical Task Network) planning using a graph of
    task, method, and operator nodes.
    """

    def __init__(self,
                 domain: Union[HTN, Dict[str, List[Method]]],
                 tasks: List[dict] = [],
                 env: Any = None,
                 validate_input: bool = False,
                 repeat_wait_time: float = 0.1,
                 order_by_cost: bool = False,
                 enable_logging: bool = False,
                 log_dir: str = None,
                 log_level: int = logging.INFO,
                 console_output: bool = False):
        """
        Initialize the HTN planner.
        :param tasks:
        :param domain:
        :param env:
        :param validate_input:
        :param repeat_wait_time:
        :param order_by_cost:
        :param enable_logging:
        :param log_dir:
        :param log_level:
        :param console_output:
        """
        # Validate inputs if requested
        if validate_input:
            validate_domain(domain)
            validate_tasks(tasks)

        # Initialize instance variables
        self.env = env
        self.state = self._get_env_state()

        # Replace plan_list with trace
        self.trace = Trace()

        # Planning options
        self.validate = validate_input
        self.repeat_wait_time = repeat_wait_time
        self.order_by_cost = order_by_cost
        self.enable_logging = enable_logging

        # print("enable_logging", enable_logging)
        
        # Setup logger
        if self.enable_logging:
            log_file = log_dir if log_dir is not None else 'planner_logs/planner_log.log'
            self.logger = PlannerLogger(log_file, log_level, console_output)
            self.logger.info("HTN Planner initialized")
        else:
            self.logger = None

        if self.enable_logging:
            self.logger.info(f"Added {len(tasks)} root tasks")
        self.root_task_queue = RootTaskQueue(tasks)

        self.planning_start_time = None

        # Set domain network (a dict domain is wrapped in an HTN)
        self.domain_network = HTN.coerce(domain)

        # Create cursor to track planning state
        # Cursors share the planner's trace so it records the whole run.
        self.cursors = [Cursor(self.trace)]

        if self.enable_logging:
            self.logger.info(f"Domain network built with {len(domain)} task types")
            self.logger.info(f"Domain network structure: {list(self.domain_network.keys())}")


    # def get_current_trace(self, include_states: bool = False):
    #     """

    #     :param include_states: Whether to include state information in the output
    #     :return:
    #     """
    #     """Gets the current trace."""
    #     if self.enable_logging:
    #         self.logger.log_function()
    #     return self.trace.get_current_trace(include_states)

    def is_exhausted(self):
        return (self.root_task_queue.is_empty() and 
                all(c.is_at_end() for c in self.cursors))

    # --------------------------
    # : plan()

    def _plan_start(self):
        self.planning_start_time = time.time()
        if self.enable_logging:
            self.logger.info("Starting automated planning")

        if self.env and hasattr(self.env, 'get_state'):
            self.state = self._get_env_state()

        if(self.is_exhausted()):
            raise StopException("There are no tasks to plan for")




    def _get_env_state(self):
        ''' Get the environment's state as a cre FactSet. An environment may
            return a FactSet directly, or a list of dicts with "type" keys.
        '''
        if self.env is None or not hasattr(self.env, 'get_state'):
            return to_factset(None)
        return to_factset(self.env.get_state())

    def _expand_taskex(self, task_exec, task_ind, cursor, push_nomatch_frames=False):
        ''' Handle the execution of a TaskEx instance
        '''

        # Apply pattern matching to find child MethodExs or OperatorEx
        #   of the current TaskEx.
        # print("get_child_executions", task_exec)
        child_execs = task_exec.get_child_executions(
            self.domain_network, self.state
        )

        # Backtrack or push a nomatch frame if matching fails for any reason.
        if(child_execs is None or len(child_execs) == 0):
            if(push_nomatch_frames):
                print("NO MATCH FRAME", task_exec, child_execs)
                cursor.push_nomatch_frame(task_exec, task_ind, child_execs, trace=self.trace)
                return None, TraceKind.FIRST_SUBTASK
            else:
                backtrack_kind = (
                    TraceKind.BACKTRACK_NO_CHILDREN,
                    TraceKind.BACKTRACK_NO_MATCH
                )[child_execs is not None]

                cursor.backtrack(backtrack_kind, trace=self.trace)
                return None, backtrack_kind

        return child_execs, None

        
    def _pop_next_root_task(self, push_nomatch_frame):
        trace = self.cursors[0].trace
        cursor = Cursor(trace)
        root_task = self.root_task_queue.get_next_task()
        

        task_exec = root_task.as_task_exec(self.state)
        child_execs, trace_kind = self._expand_taskex(task_exec, None, cursor, push_nomatch_frame)

        trace.add(TraceKind.NEW_ROOT_TASK, task_exec)
        if(trace_kind is not None):
            trace.add(trace_kind, task_exec)

        # A root task that is an operator: give each matching OperatorEx its
        #  own cursor, as expanding a subtask into operators does.
        if(child_execs and isinstance(child_execs[0], OperatorEx)):
            self.cursors = []
            for child_exec in child_execs:
                op_cursor = cursor.copy()
                op_cursor.push_task_exec(self.state, task_exec, None, [child_exec], trace=trace)
                child_exec.cursor = op_cursor
                self.cursors.append(op_cursor)
            return TraceKind.EXPAND_TO_OPERATOR

        cursor.push_task_exec(self.state, task_exec, None, child_execs, trace=trace)
        self.cursors = [cursor]
        return None


    def plan(self, multiheaded=False, stop_kinds=[], push_nomatch_frame=False) -> List:
        """
        Execute the planning process.
        :return: The complete plan.
        """
        self._plan_start()

        while True:
            if(all(cursor.is_at_end() for cursor in self.cursors)):
                # If all cursors exhausted, create a new cursor by popping off a new root task
                if(not self.root_task_queue.is_empty()):
                    if(self._pop_next_root_task(push_nomatch_frame) in stop_kinds):
                        return self.trace

                # But if no more root tasks terminate
                else:
                    self.trace = self.cursors[0].trace
                    self.trace.add(TraceKind.ROOT_TASKS_EXHAUSTED)
                    return self.trace

            # print()
            # print("LOOP START:", self.cursors)
            # print("-------------------")

            # Plan all cursors down to their next stop point 
            #  or to their next operator execution, whatever comes first
            halted_cursors = []
            operator_cursors = []
            cursors = [*self.cursors]
            while(len(cursors) > 0):
                # print("CURSORS:", cursors)
                cursor = cursors.pop()
                curr_frame = cursor.current_frame
                new_cursors = [] if multiheaded else [cursor]

                # print("CURSOR:", cursor)
                    
                # If the current cursor is pointing at an operator exec, save it
                #  for later to be part of the conflict set that we present to env
                if(cursor.is_operator_frame):
                    operator_cursors.append(cursor)
                    continue

                if(curr_frame.is_all_blockers):
                    raise NotImplementedError("NEED TO HANDLE BACKTRACK??")

                # if(cursor.skippables_overflow):
                #     cursor.mark_parent_skippable()
                #     # print()
                #     print("OVERFLOWS", cursor)

                
                if(multiheaded):
                    # Go through the the cursor's effective span: the sequence of next subtasks 
                    #  that can be executed from the current frame's cursor position, of which
                    #  there may be several because of optional and skippable subtasks and unordered
                    #  groups. This loop marks INITIALIZED subtasks as EXECUTABLE to indicate that
                    #  they are in the current effective span. As subtasks are descended into and 
                    #  popped out of (e.g. when the iteration process overflows past the end of the subtask
                    #  sequence), the effective span within each frame (i.e. method level) can widen.
                    #  For instance, widening can occur when the parent subtask can be skipped because it
                    #  descends into methods with empty subtask, when all of its subtasks are skippable.

                    did_expand_method = False;
                    while((tup := cursor.next_subtask_exec()) is not None):
                        task_ind, task_exec = tup

                        # Skip any task_execs that are already SUCCESS, FAIL, or IN_PROGRESS
                        if(task_exec.status != ExStatus.INITIALIZED):
                            continue

                        if(not curr_frame.can_execute_mask[task_ind]):
                            task_exec.status = ExStatus.SKIPPED
                            continue

                        # print("<<", task_exec)
                        child_execs, trace_kind = self._expand_taskex(task_exec, task_ind, cursor, push_nomatch_frame)

                        if(child_execs is None): continue
                        
                        # did_expand_method = False;
                        # _new_cursors = []
                        for child_exec in child_execs:
                            new_cursor = cursor.copy()
                            new_cursor.push_task_exec(self.state, task_exec, task_ind, [child_exec], trace=cursor.trace)
                            child_exec.cursor = new_cursor
                            # print("      >", child_exec.cursor)

                            if isinstance(child_exec, MethodEx):
                                # If ever expand method, push this cursor to the stack so 
                                #   it can resume after descending into its children
                                if(not did_expand_method): new_cursors.append(cursor)
                                did_expand_method = True
                                trace_kind = TraceKind.EXPAND_TO_METHOD 
                            else:
                                trace_kind = TraceKind.EXPAND_TO_OPERATOR

                            if(trace_kind in stop_kinds):
                                halted_cursors.append(new_cursor)
                            else:
                                # If we didn't stop then push the child cursor to the stack
                                new_cursors.append(new_cursor)

                        # Append in reversed order so sooner popped first
                        # new_cursors += _new_cursors[::-1]


                        task_exec.status = ExStatus.EXECUTABLE

                        if(did_expand_method): break;

                    # If the skippable items in this cursor overflow past its end
                    # print("       ", cursor, cursor.skippables_overflow)
                    if(cursor.skippables_overflow):
                        # Then mark the parent as skippable
                        cursor.mark_parent_skippable(True)

                        # print("DOES OVERFLOW" , cursor.has_unswept_parent())

                        # Additionally, if this cursor was not generated via a descent 
                        #  because it is the starting cursor for the cycle or generated
                        #  through ascending from a child, then add a cursor that pops
                        #  up one frame
                        if(cursor.has_unswept_parent()):
                            par_cursor = cursor.copy()
                            par_cursor._pop_frame()
                            # print("   UNSWEPT PARENT", cursor, par_cursor)
                            new_cursors = [par_cursor] + new_cursors


                    # If ever expanded method, while going through this frame prepend 
                    #   it to stack so can resume after descending into its children
                    # if(did_expand_method):
                    #     new_cursors = [cursor] + new_cursors


                else:
                    task_ind, task_exec = cursor.subtask_exec_ind, cursor.current_subtask_exec
                    child_execs, trace_kind = self._expand_taskex(task_exec, task_ind, cursor, push_nomatch_frame)

                    if(child_execs is None): continue
                        
                    trace_kind = cursor.push_task_exec(self.state, task_exec, task_ind, [child_exec], trace=cursor.trace)
                    # task_exec.status = ExStatus.IN_PROGRESS

                    if(trace_kind in stop_kinds or trace_kind is None):
                        should_break = True

                        # trace_kind = self._step_cursor(task_exec, cursor, stop_kinds, push_nomatch_frame)
                cursors += new_cursors
                
            
            # print("HALT CURSORS:", [c.current_frame.current_operator_exec.operator.name for c in halted_cursors])
            # print("HALT CURSORS:", len(halted_cursors))

            if(len(halted_cursors) > 0):
                self.cursors = halted_cursors
                break; 

            # If planning ran out of work without reaching any operator
            #  (e.g. the root's method has no subtasks), the root task is
            #  done. Leave a finished cursor so the loop moves on to the
            #  next root task.
            if(len(operator_cursors) == 0):
                cursors = self.cursors = [Cursor(self.cursors[0].trace)]
                continue

            operator_cursors = operator_cursors[::-1]
            # print("OPERATOR CURSORS:", [c.current_frame.current_operator_exec.operator.name for c in operator_cursors])
            # print("-------------------")
            # print("OPERATOR CURSOR:", operator_cursors[0])
            result_cursor = self._attempt_conflict_set(operator_cursors)
            # print("RESULT CURSOR", result_cursor)
            # if(result_cursor.current_frame): print(result_cursor.current_frame.eff_span)
            cursors = self.cursors = [result_cursor]

            # cursor = operator_cursors[0]
            # print("APPLY", cursor)
            # trace_kind = self._handle_operator_exec(cursor)
            # print("TK:", trace_kind)
            # cursors = self.cursors = [cursor]
            # break;


        # if(not multiheaded):
        #     while True:
        #         # TODO: Is there a case where cursors[0] resolves to something different or can we 
        #         #   grab this outside of the loop?
        #         cursor = self.cursors[0]
        #         trace_kind = self._step_cursor_linear(cursor, stop_kinds, push_nomatch_frame)
        #         if(trace_kind in stop_kinds or trace_kind is None):
        #             return self.trace

        # else:
        #     # TODO: This needs to work even if the cursor list changes while looping
        #     #   we might for instance advance one cursor in a way that invalidates others
        #     #   or add new cursors as we bifurcate the path.
        #     for cursor in self.cursors:
        #         while True:
        #             trace_kind = self._step_cursor_multiheaded(cursor, stop_kinds, push_nomatch_frame)
        #             if(trace_kind in stop_kinds or trace_kind is None):
        #                 break


        # for cursor in self.cursors:

        # Main Plan Loop:
        #  Each loop handles one TaskEx instance and makes one move of a Cursor.
        #   (the Cursor holds the current plan state and stack).
        #  Every Cursor movement is gaurenteed to either:
        #   1. Enter a valid frame w/ a parent TaskEx, MethodEx, and current sub-TaskEx.
        #   2. Exhaust the cursor (the current TaskEx will come from the root_task_queue)
            # while True:

                # self._step_cursor_linear

                # task_exec = self._next_task_exec()

                # if(task_exec is None):
                #     break

                # # Handle the current task execution
                # trace_kind = self._handle_task_exec(task_exec, cursor, push_nomatch_frame)
                # if(trace_kind in stop_kinds):
                #     return self.trace

        return self.trace

    def _attempt_conflict_set(self, operator_cursors):
        any_success = False
        for cursor in operator_cursors:
            operator_exec = cursor.current_frame.current_operator_exec
            # success = self._env_try_operator_exec(operator_exec)
            success = self.apply(operator_exec, cursor)
            if(success):
                return cursor;

        cursor = operator_cursors[0]
        operator_exec = cursor.current_frame.current_operator_exec
        self.backtrack(operator_exec, cursor)
        return cursor

    def _recover_cursor(self, operator_exec):
        cursor = getattr(operator_exec, 'cursor', None)
        if(cursor is None):
            raise NotImplementedError("Cannot recover cursor from operator exec.")
        return cursor
            
    def apply(self, operator_exec, cursor=None):
        if(cursor is None):
            cursor = self._recover_cursor(operator_exec)
        # print()
        # print()
        # print("-- APPLY:", operator_exec, " --")
        success = True
        if(self.env):
            success = self._env_try_operator_exec(operator_exec)

        if(success):
            self.trace.add(TraceKind.APPLY_OPERATOR, operator_exec)
            if(cursor.current_frame):
                cursor.advance_subtask(self.state, trace=cursor.trace)

            self.cursors = [cursor]
        else:
            self.trace.add(TraceKind.OPERATOR_FAIL, operator_exec)
        return success

        
        # return TraceKind.APPLY_OPERATOR

    def backtrack(self, operator_exec, cursor=None):
        if(cursor is None):
            cursor = self._recover_cursor(operator_exec)

        print("Backtrack:", operator_exec)
        cursor.backtrack(
            self.state,
            TraceKind.BACKTRACK_OPERATOR_FAIL,
            trace=self.trace
        )
        # return TraceKind.BACKTRACK_OPERATOR_FAIL



    def _next_task_exec(self, cursor):
        # If cursors are exhausted, pop off new root task, and make it into a TaskEx.
        if(cursor.is_at_end()):
            if(self.root_task_queue.is_empty()):
                self.trace.add(TraceKind.ROOT_TASKS_EXHAUSTED)
                return None

            root_task = self.root_task_queue.get_next_task()
            task_exec = root_task.as_task_exec(self.state)
            self.trace.add(TraceKind.NEW_ROOT_TASK, task_exec)

        # Otherwise find all the next TaskExs that the cursor is pointing to
        else:
            task_exec = cursor.current_subtask_exec

        return task_exec

    # def _step_cursor(self, task_exec, cursor, stop_kinds, push_nomatch_frame):
    #     if(task_exec is None):
    #         return None

    #     # Expand the current task execution
    #     child_execs, trace_kind = self._expand_taskex(task_exec, cursor, push_nomatch_frame)

    #     if(child_execs is None):
    #         return trace_kind

    #     # Primitive Task Case: execute Operator
    #     if(isinstance(child_execs[0], OperatorEx)):
    #         trace_kind = self._handle_operator_exec(cursor, child_execs[0])

    #     # Higher-Order Task Case: Push a new frame with options for 
    #     #    executing the task (will be handled in next loop).
    #     else:
    #         cursor.push_task_exec(task_exec, child_execs, trace=self.trace)
    #         task_exec.status = ExStatus.IN_PROGRESS
    #         trace_kind = TraceKind.FIRST_SUBTASK

    #     return trace_kind


    # def _all_next_task_execs(self, cursor):
        


    # def _step_cursor_multiheaded(self, cursor, stop_kinds, push_nomatch_frame):
    #     if(cursor.is_at_end()):
    #         return None
        
    #     task_execs = cursor.get_cand_next_subtask_execs()

    #     # print(cursor)
    #     if(task_execs is None or len(task_execs) == 0):
    #         return None

    #     pairs = []
    #     should_branch = False
    #     trace_kind = None
    #     pref = [None, TraceKind.BACKTRACK_NO_CHILDREN, TraceKind.BACKTRACK_NO_MATCH]

    #     for task_exec in task_execs:
    #         # Expand the current task execution
    #         child_execs, _trace_kind = self._expand_taskex(task_exec, cursor, push_nomatch_frame)


    #         if(pref.index(_trace_kind) > pref.index(trace_kind)):
    #             trace_kind = _trace_kind

    #         if(child_execs is None):
    #             continue

    #         # If the current frame has any staged operator executions then if 
    #         #   there are also any staged method executions those should be handled 
    #         #   by copies of the current cursor.
    #         if(any(isinstance(child_exec, OperatorEx) for child_exec in child_execs)):
    #             should_branch = True

    #         pairs.push(task_exec, child_execs)

    #     if(len(pairs) == 0):
    #         return trace_kind

    #     for task_exec, child_execs in pairs:
    #         for child_exec in child_execs:
    #             # Primitive Task Case: execute Operator
    #             if(isinstance(child_exec, OperatorEx)):
    #                 trace_kind = self._handle_operator_exec(cursor, child_exec)

    #             # Higher-Order Task Case: Push a new frame with options for 
    #             #    executing the task (will be handled in next loop).
    #             else:
    #                 c = cursor
    #                 if(should_branch):
    #                     c = cursor.copy()
    #                     self.cursors.append(c)
    #                 c.push_task_exec(task_exec, [child_exec], trace=self.trace)
    #                 task_exec.status = ExStatus.IN_PROGRESS
    #                 trace_kind = TraceKind.FIRST_SUBTASK
    #                 should_branch = True

    #     return trace_kind




            

    # --------------------------
    # : printing methods

    def print_current_plan(self):
        """Prints the current plan."""
        if self.enable_logging:
            self.logger.log_function()
        return self.trace.print_plan()


    def print_current_frames(self):
        # print(len(self.cursors))
        frames = {}
        for cursor in self.cursors:
            if(cursor.is_at_end()): continue
            for i, frame in enumerate(cursor.stack):
                if(id(frame) not in frames):
                    frames[id(frame)] = (i, frame)
            frame = cursor.current_frame
            if(frame is None): continue
            if(id(frame) not in frames):
                frames[id(frame)] = (len(cursor.stack), frame)

        if(len(frames) == 0):
            print("All Cursors Exhausted")
        for depth, frame in frames.values():
            if(not frame.is_operator_frame):
                print(frame)








    def print_current_trace(self, include_states: bool = False) -> None:
        """
        Prints the current trace.
        :param include_states: Whether to include state information in the output
        :return:
        """
        if self.enable_logging:
            self.logger.log_function()
        return self.trace.print_trace(include_states)

    def print_network(self) -> None:
        """
        Visualize the planner network.
        :return: None
        """
        if self.enable_logging:
            self.logger.log_function()
        strings = []
        tab = '  '
        header = "PLANNER NETWORK"
        border = '#' * (len(header) + 4)
        print(border)
        print('# ' + header + ' #')
        print(border + '\n')

        for task_key, methods_or_op in self.domain_network.items():
            strings.append(0 * tab + f'Task({task_key})')
            if(not isinstance(methods_or_op, list)):
                methods_or_op = [methods_or_op]

            for method_or_op in methods_or_op:
                if(isinstance(method_or_op, Method)):
                    method = method_or_op
                    pos_args = [method.name, *method.args]
                    strings.append(1 * tab + f"Method({', '.join([repr(x) for x in pos_args])}")

                    for subtask in method.subtasks:
                        strings.append(2 * tab + f'{subtask})')

                elif(isinstance(method_or_op, Operator)):                
                    strings.append(1 * tab + f'{method_or_op}')

                
        for s in strings:
            print(s)
        print('\n')

    def print_planner_state(self):
        if self.enable_logging:
            self.logger.log_function()
        print("===== PLANNER STATE =====")
        print(f"Queued root tasks: {len(self.root_task_queue)}")
        for i, task in enumerate(self.root_task_queue.queue):
            print(f"  Task {i}: {task}")

        print(f"Cursors: {len(self.cursors)}")
        for i, cursor in enumerate(self.cursors):
            if(cursor.is_at_end()):
                print(f"  Cursor {i}: at end")
                continue
            frame = cursor.current_frame
            print(f"  Cursor {i}: task={frame.current_task_exec}, "
                  f"child={frame.current_child_exec}, "
                  f"stack depth={len(cursor.stack)}")

        print(f"Trace entries: {len(self.trace.entries)}")
        for i, entry in enumerate(self.trace.entries[-5:]):  # Show last 5 entries
            print(f"  Entry {i}: kind={entry.kind}")

        print("=========================")


    # ----------------------
    # : Adding Removing Root Tasks and Methods

    # def add_method(self, task_name: str, task_args: tuple['V'], preconditions: 'Fact', subtasks: List[Any]):
    #     if self.enable_logging:
    #         self.logger.log_function()
    #     new_method = Method(name=task_name, args=task_args, preconditions=preconditions, subtasks=subtasks)
    #     task_id = f"{task_name}/{len(task_args)}"
    #     self.domain_network[task_id].append(new_method)
    #     return new_method

    def add_method(self, method: Method, task: Optional[str] = None):
        self.domain_network.add(method, task=task)


    def add_tasks(self, tasks: Union[
                         Union[dict, tuple, Task],
                         Sequence[Union[dict, tuple, Task]]
                        ]) -> None:
        """
        Add tasks to the planner's root task queue.
        :param tasks: A list of task specifications.
        :return: None
        """
        if self.enable_logging:
            self.logger.log_function()

        if not isinstance(tasks, list):
            tasks = [tasks]

        for task in tasks:
            self.root_task_queue.add(task)
        # self._add_tasks(tasks)

    def clear_tasks(self):
        """Clear all tasks."""
        if self.enable_logging:
            self.logger.log_function()
        self.root_task_queue.clear()
        self.cursors = [Cursor(self.cursors[0].trace)]

    @property
    def cursor(self):
        ''' The primary cursor. The interactive interface assumes a single
            cursor; multiheaded planning can leave several, all within the
            same root task.
        '''
        return self.cursors[0]

    def get_current_root(self):
        """Gets the root TaskEx currently being planned, or None."""
        if self.enable_logging:
            self.logger.log_function()

        cursor = self.cursor
        if(cursor.is_at_end()):
            return None
        if(len(cursor.stack) > 0):
            return cursor.stack[0].task_exec
        return cursor.current_frame.task_exec

    def get_current_plan(self):
        """Gets the current plan."""
        if self.enable_logging:
            self.logger.log_function()
        return self.trace.get_current_plan()

    def remove_task(self, task: str):
        """
        Remove the next occurrence of a task. If the provided task is the current task,
        it will be removed and the planner will automatically continue to the next text.
        """
        if self.enable_logging:
            self.logger.log_function()
        pass

    def remove_tasks(self, tasks: List[str]):
        """
        Remove all occurrences of the tasks in the given list.
        """
        if self.enable_logging:
            self.logger.log_function()
        pass

    def reset(self):
        if self.enable_logging:
            self.logger.log_function()
        self.trace = Trace()
        self.cursors = [Cursor(self.trace)]

# --------------------------------------------
# : Interactive Interface

    def plan_to_next_decomposition(self, push_nomatch_frame=True):
        """
        Steps to the next applicable method for a task.
        :param all_methods: Whether to return all methods or one method at a time
        :return: The current task and either next method or all methods
        """
        return self.plan(stop_kinds=[TraceKind.FIRST_SUBTASK], push_nomatch_frame=push_nomatch_frame)

    def get_next_method_execs(self):
        frame = self.cursor.current_frame
        method_execs = frame.child_execs
        return frame.task_exec, method_execs


    def plan_to_next_operators(self, multiheaded=True, push_nomatch_frame=True):
        return self.plan(stop_kinds=[TraceKind.EXPAND_TO_OPERATOR], 
                         multiheaded=multiheaded,
                         push_nomatch_frame=push_nomatch_frame)

    def get_next_operator_execs(self):
        operator_cursors = []
        for cursor in self.cursors:
            if(not cursor.is_at_end() and cursor.is_operator_frame):
                operator_cursors.append(cursor.current_operator_exec)
        return operator_cursors

    def get_prev_operator_exec(self):
        op_exec = self.trace.get_prev_operator()
        return op_exec 

    def stage_method_exec(self, method_exec):
        task_exec, method_execs = self.get_next_method_execs()
        inds = [i for i, x in enumerate(method_execs) if x == method_exec]

        if(len(inds) == 0):
            raise ValueError(f"MethodEx stage {method_exec} fail." +
                "Not a possible MethodEx for this state")

        self.cursor.user_select_method_exec(self.state, inds[0], self.trace)
    
    def add_method_exec(self, method_exec, task_exec=None):
        self.cursor.add_method_exec(method_exec, task_exec, self.trace)


    ####################
    # HELPER FUNCTIONS #
    ####################

    def _env_try_operator_exec(self, operator_exec: OperatorEx) -> bool:
        """Execute an operator and update state."""

        # Execute in environment if available
        # print(operator_exec)

        success = True
        if self.env:
            if self.enable_logging:
                self.logger.info(f"Executing operator in environment: {operator_exec}")

            # Try to apply the operator in the environment
            try:
                success = self.env.execute_action(
                    operator_exec.operator.name,
                    operator_exec.match
                )
            except Exception as e:
                print(">> Exception")
                print(e)
                raise e
                success = False
        else:
            if self.enable_logging:
                self.logger.info(f"No environment, skipping: {operator_exec}")
        
        if(success):
            operator_exec.status = ExStatus.SUCCESS
            if self.enable_logging:
                self.logger.info(f"State updated after operator execution: {operator_exec}")
        else:
            operator_exec.status = ExStatus.FAILED
            if self.enable_logging:
                self.logger.info(f"Operator execution failed: {operator_exec}")

        # Update planner state with environment state after applying operator
        if hasattr(self.env, 'get_state'):
            self.state = self._get_env_state()

        return success
            

    # def _get_method_executions(self) -> List[MethodEx]:
    #     """Get methods applicable to the current task."""
    #     if self.enable_logging:
    #         self.logger.log_function()
    #         self.logger.info(f"Locating applicable methods for task {self.cursor.current_task.name}")

    #     if not self.state:
    #         raise ValueError("State must be set before getting applicable methods")

    #     # Check if there are methods for this task type
    #     if self.cursor.current_task.id not in self.domain_network:
    #         if self.enable_logging:
    #             self.logger.warning(f"No methods found for task {self.cursor.current_task.id}")
    #         return []

    #     applicable_methods = []
    #     methods = self.domain_network[self.cursor.current_task.id]

    #     # Sort by cost if requested
    #     if self.order_by_cost:
    #         methods = sorted(methods, key=lambda m: m.cost)

    #     # Check each method
    #     visited = []
    #     for method in methods:
    #         result = method.applicable(self.cursor.current_task, self.state, str(self.trace.get_current_plan()), visited)
    #         if result:
    #             applicable_methods.append(result)
    #             if self.enable_logging:
    #                 self.logger.info(f"Method {method.name} is applicable to task {self.cursor.current_task.name}")

    #     if self.enable_logging:
    #         self.logger.info(
    #             f"Found {len(applicable_methods)} applicable methods for task {self.cursor.current_task.name}")

    #     return applicable_methods

    # def _process_current_task(self) -> bool:
    #     """Process the current task by finding and applying methods."""
    #     if self.enable_logging:
    #         self.logger.log_function()
    #         self.logger.info(f"Processing task {self.cursor.current_task.name}")

    #     # Add task entry to trace
    #     self.trace.add_task(self.cursor.current_task)

    #     # If we already have a method, continue executing it
    #     if self.cursor.current_method:
    #         return self._continue_method_execution()

    #     # Get applicable methods for this task
    #     method_execs = self._get_method_executions()
    #     if not method_execs:
    #         if self.enable_logging:
    #             self.logger.warning(f"No applicable methods for task {self.cursor.current_task.name}")
    #         self.cursor.current_task.status = 'failed'
    #         return False

    #     # Store available methods for backtracking
    #     self.cursor.available_method_execs = method_execs
    #     self.cursor.current_method_index = 0
    #     self.cursor.current_method = methods[0]
    #     self.cursor.current_subtask_index = 0

    #     # Add method entry to trace
    #     self.trace.add_method(
    #         self.cursor.current_task,
    #         self.cursor.current_method,
    #         "Method selected as first applicable method"
    #     )

    #     if self.enable_logging:
    #         self.logger.info(
    #             f"Selected method {self.cursor.current_method.name} for task {self.cursor.current_task.name}")

    #     # Execute the selected method
    #     return self._continue_method_execution()

    # def _set_cursor_to_new_task(self):
    #     if self.enable_logging:
    #         self.logger.log_function()
    #     next_task = self.root_task_execs.pop(0)
    #     if self.enable_logging:
    #         self.logger.info(f"Moving on to new task: ({next_task.name})")
    #     # Handle repeat tasks
    #     if next_task.repeat > 0:
    #         next_task.repeat -= 1
    #         self.root_task_execs.insert(0, next_task)
    #     self.cursor.set_task(next_task)
    #     # Get and store applicable methods
    #     method_execs = self._get_method_executions()
    #     if not method_execs:
    #         if self.enable_logging:
    #             self.logger.info(f"No applicable methods found for task: ({self.cursor.current_task.name})")
    #         return False

    #     self.cursor.available_method_execs = methods
    #     self.cursor.current_method_index = 0
    #     return True




