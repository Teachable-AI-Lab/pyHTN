from copy import deepcopy
import time
from enum import Enum
from pyhtn.htn import Task, Method, Operator, TaskEx, MethodEx, OperatorEx
from pyhtn.common.imports.typing import *


class TraceKind(Enum):
    NEW_ROOT_TASK :           "TraceKind" = 1
    FIRST_SUBTASK :           "TraceKind" = 2
    EXPAND_TO_OPERATOR :      "TraceKind" = 3
    EXPAND_TO_METHOD :        "TraceKind" = 4
    ADVANCE_SUBTASK :         "TraceKind" = 5
    USER_SELECT_SUBTASK :     "TraceKind" = 6

    SELECT_METHOD :           "TraceKind" = 7
    USER_SELECT_METHOD :      "TraceKind" = 8
    USER_ADD_METHOD :         "TraceKind" = 9

    APPLY_OPERATOR :          "TraceKind" = 10

    POP_FRAME :               "TraceKind" = 11


    OPERATOR_FAIL :           "TraceKind" = 12
    BACKTRACK_NO_CHILDREN :   "TraceKind" = 13
    BACKTRACK_NO_MATCH :      "TraceKind" = 14  
    BACKTRACK_OPERATOR_FAIL : "TraceKind" = 15   
    BACKTRACK_CHILD_CASCADE : "TraceKind" = 16

    ENTER_NOMATCH_FRAME :     "TraceKind" = 17

    ROOT_TASKS_EXHAUSTED :    "TraceKind" = 18



class TraceEntry:
    """Base class for all trace entries."""
    def __init__(self, 
            kind: TraceKind,
            arg0=None,
            arg1=None):        
        from pyhtn.htn.planner import FrameContext 

        self.kind = kind
        if(isinstance(arg0, FrameContext)):
            self.prev_frame = arg0
        elif(isinstance(arg0, TaskEx)):
            self._task_exec = arg0
        elif(isinstance(arg0, OperatorEx)):
            self._operator_exec = arg0
        elif(isinstance(arg0, MethodEx)):
            self._method_exec = arg0

        if(isinstance(arg1, FrameContext)):
            self.next_frame = arg1

        self.timestamp = time.time()

    def _get_ex(self, kind='task', frame_kind='next'):
        if(hasattr(self, f'_{kind}_exec')):
            return getattr(self,f"_{kind}_exec")
        frame = getattr(self, f"{frame_kind}_frame", None)
        if(frame is None):
            return None
        return getattr(frame, f"current_{kind}_exec", None)

    @property
    def operator_exec(self):
        return getattr(self, '_operator_exec', None)

    @property
    def task_exec(self):
        return self._get_ex('task', 'next')
    @property
    def method_exec(self):
        return self._get_ex('method', 'next')
    @property
    def subtask_exec(self):
        return self._get_ex('subtask', 'next')

    @property
    def prev_task_exec(self):
        return self._get_ex('task', 'prev')
    @property
    def prev_method_exec(self):
        return self._get_ex('method', 'prev')
    @property
    def prev_subtask_exec(self):
        return self._get_ex('subtask', 'prev')
        
    def get_description(self):
        if(self.kind in (
            TraceKind.NEW_ROOT_TASK,
            TraceKind.ENTER_NOMATCH_FRAME,
            )):
            return f"{self.task_exec}"
        elif(self.kind in (
                TraceKind.FIRST_SUBTASK,
                TraceKind.EXPAND_TO_OPERATOR,
                TraceKind.EXPAND_TO_METHOD,
                TraceKind.ADVANCE_SUBTASK,
                TraceKind.USER_SELECT_SUBTASK,
            )):
            return f"{self.subtask_exec}"
        
        elif(self.kind in (
                TraceKind.SELECT_METHOD,
                TraceKind.USER_SELECT_METHOD,
                TraceKind.USER_ADD_METHOD
            )):
            return f"{self.method_exec}"
        elif(self.kind in (
                TraceKind.APPLY_OPERATOR,
                TraceKind.BACKTRACK_OPERATOR_FAIL,
            )):
            return f"{self.operator_exec}"
        elif(self.kind == TraceKind.POP_FRAME):
            return f"Return to parent {self.subtask_exec}"
        elif(self.kind in (
                TraceKind.BACKTRACK_NO_CHILDREN,
                TraceKind.BACKTRACK_NO_MATCH,
                TraceKind.BACKTRACK_CHILD_CASCADE,
            )): 
            return f"From {self.prev_subtask_exec} to {self.subtask_exec} in {self.method_exec}"
        else:
            return ""

    def __str__(self):
        return f"{self.kind.name}: {self.get_description()}"

    @property
    def entry_type(self):
        if("TASK" in self.kind.name and 
            self.kind != TraceKind.ROOT_TASKS_EXHAUSTED):
            return 'task'
        elif("METHOD" in self.kind.name):
            return 'method'
        elif("OPERATOR" in self.kind.name):
            return 'operator'
        elif("BACKTRACK" in self.kind.name):
            return 'backtrack'
        else:
            return 'control'

class Trace:
    """
    Maintains a comprehensive trace of the planning process including method decisions,
    operator executions, task decompositions, and backtracking operations.
    """
    def __init__(self):
        self.entries = []
        self.sequence_counter = 0

    def add(self, kind : TraceKind,
                  arg0 : Union[TaskEx, "FrameContext"] = None,
                  arg1 : "FrameContext" = None):
        self.entries.append(TraceEntry(kind, arg0, arg1))

    def get_current_plan(self):
        """
        Get the current plan consisting of successful operator executions.
        Returns a list of operators in the order they were executed.
        """
        return [entry.operator_exec for entry in self.entries
                if entry.kind == TraceKind.APPLY_OPERATOR]


    def get_prev_entry(self):
        return self.entries[-1]
        
    def get_prev_root_index(self):
        for i in range(len(self.entries)-1, -1, -1):
            if(self.entries[i].kind == TraceKind.NEW_ROOT_TASK):
                return i

    def get_prev_root(self):
        entry = self.entries[self.get_prev_root_index()]
        return entry.task_exec

    def get_prev_operator_index(self):
        for i in range(len(self.entries)-1, -1, -1):
            if(self.entries[i].kind == TraceKind.APPLY_OPERATOR):
                return i

    def get_prev_operator(self):
        entry = self.entries[self.get_prev_operator_index()]
        return entry.operator_exec



    # def get_current_trace(self, include_states=False):
    #     """
    #     Get the full trace with all entries.
    #     If include_states is False, state information is omitted to reduce verbosity.
    #     """
    #     if include_states:
    #         return self.entries

    #     # Create a copy without state information
    #     simplified_trace = []
    #     for entry in self.entries:
    #         entry_copy = deepcopy(entry)
    #         if hasattr(entry_copy, 'state_before'):
    #             entry_copy.state_before = "..."
    #         if hasattr(entry_copy, 'state_after'):
    #             entry_copy.state_after = "..."
    #         simplified_trace.append(entry_copy)

    #     return simplified_trace

    def print_plan(self):
        """
        Print the current plan.
        """
        plan = self.get_current_plan()

        print("┌─────────────────────────────────────────────────┐")
        print("│                 CURRENT PLAN                    │")
        print("└─────────────────────────────────────────────────┘")

        if not plan:
            print("No actions in plan yet.")
            return

        for i, op_ex in enumerate(plan):
            print(f"Step {i + 1:02d}: {op_ex}")

        print(f"\nTotal actions: {len(plan)}")
        print("────────────────────────────────────────────────────")

    def print_trace(self, include_states=False, max_entries=None):
        """
        Prints the full trace of the plan.
        :param include_states: Whether to include state information
        :param max_entries: Maximum number of entries to print (None for all)
        :return:
        """
        # trace = self.get_current_trace(include_states)
        start = 0 if max_entries is None else -max_entries
        entries = self.entries[start:]

        print("┌─────────────────────────────────────────────────┐")
        print("│                PLANNING TRACE                   │")
        print("└─────────────────────────────────────────────────┘")

        if len(entries) == 0:
            print("No trace entries yet.")
            return

        # Define colors for different entry types (ANSI escape codes)
        colors = {
            "task": "\033[94m",  # Blue
            "method": "\033[92m",  # Green
            "operator": "\033[93m",  # Yellow
            "backtrack": "\033[91m"  # Red
        }
        reset_color = "\033[0m"

        for i, entry in enumerate(entries):
            # Format timestamp
            timestamp = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))

            # Start with line number and timestamp
            line = f"{(i+start)+1:03d} [{timestamp}] "

            # Add colored type indicator
            type_color = colors.get(entry.entry_type, "")
            type_str = f"{type_color}[{entry.kind.name}]{reset_color}"
            line += f"{type_str:12} "

            # Add description
            line += entry.get_description()

            # Print line
            print(line)

            # # Add additional info based on entry type
            # if entry.entry_type == "method":
            #     print(f"     Reason: {entry.reason}")

            # elif entry.entry_type == "operator" and include_states:
            #     print(f"     Success: {entry.success}")
            #     if include_states:
            #         print(f"     State before: {entry.state_before}")
            #         if entry.state_after:
            #             print(f"     State after: {entry.state_after}")

            # Add separator between entries
            print("─" * 60)

        print(f"\nTotal entries: {len(entries)}")
        if max_entries and len(self.entries) > max_entries:
            print(f"Showing last {max_entries} of {len(self.entries)} entries.")
        print("────────────────────────────────────────────────────")

