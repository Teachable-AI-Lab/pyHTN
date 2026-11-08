from pyhtn.common.imports.typing import *
from abc import ABC, abstractmethod
from pyhtn.htn.htn_elements import Task, Method, Operator, HTN_Element
from pyhtn.common.utils import unique_hash
from pyhtn.htn.matching import to_factset, head_binding, any_match
from cre import FactSet
from enum import Enum

class ExStatus(Enum):
    INITIALIZED : "ExStatus" = 1
    EXECUTABLE  : "ExStatus" = 2
    SKIPPED     : "ExStatus" = 3
    IN_PROGRESS : "ExStatus" = 4
    SUCCESS :     "ExStatus" = 5
    FAILED :      "ExStatus" = 6

class ElementExecution(ABC):
    def __init__(self,
                 element: HTN_Element,
                 state: FactSet,
                 match: Sequence[Any] = (),
                 parent_exec: Any = None,
                 child_execs: Sequence["ElementExecution"] = [],
                 ) -> None:

        self.element = element        
        self.state = state
        self.match = match
        self.parent_exec = parent_exec
        self.child_execs = child_execs
        self.status = ExStatus.INITIALIZED

    def _base_longhash(self):
        parent_id = None
        if(self.parent_exec is not None):
            self.parent_exec.id

        state = self.state
        state_hash = state.long_hash() if isinstance(state, FactSet) else state
        return unique_hash([
            self.element.id, state_hash, self.match, parent_id
        ])

    def _str_helper(self, kind):
        if(len(self.match) > 0):
            match_str = ", ".join([repr(x) for x in self.match])
            return f"{kind}({self.element.name!r}, {match_str})"
        else:
            return f"{kind}({self.element.name!r})"

    def as_dict(self):
        elem = self.element
        return {
            "id" : self.id,
            "name" : elem.name,
            "match" : self.match,
            "kind" : type(self).__name__,
            "parent_id" : self.parent_exec.id if self.parent_exec else '',
            "child_ids" : [ex.id for ex in self.child_execs],
            "child_data" : [
                {
                    "name": ex.element.name,
                    "match": ex.match,
                    "id": ex.id
                }
                for ex in self.child_execs
            ],

            "status" : self.status.name,
            "htn_element" : elem.id
        }

    def tree_to_dict(self):#, cursor=None):
        d = {}
        frontier = [self]
        while len(frontier) > 0:
            new_frontier = []
            for ex in frontier:
                d[ex.id] = ex.as_dict()
                new_frontier += ex.child_execs
            frontier = new_frontier

        # if(cursor):
        #     frames = [*cursor.stack, cursor.current_frame]
        #     for frame in frames:
        #         te = frame.current_task_exec
        #         me = frame.current_method_exec
        #         if(te):
        #             d[te.id]['on_plan_path'] = True
        #         if(me):
        #             d[me.id]['on_plan_path'] = True
                
        return d

    def fn_str(self):
        return f"{self.element.name}({', '.join([str(x) for x in self.match])})"

def _match_str(match):
    return ', '.join([str(x) for x in match])

def _dict_obj_to_str(tree_dict, obj, options={}, recurse=True,  depth=0):

    cpre = ""
    cpost = ""
    if(options.get("show_colors", False)):
        colors = {
            "INITIALIZED": "\033[0m",  
            "IN_PROGRESS": "\033[94m",  # Blue
            "SUCCESS": "\033[92m",  # Green
            "FAILED": "\033[91m"  # Red
        }
        cpre = colors[obj.get('status', "INITIALIZED")]
        cpost = "\033[0m"

    s = ""
    if(obj['kind'] == "TaskEx"):
        prim = obj.get('is_primitive', False)
        s = f"{cpre}{' '*depth}TE{'-P' if prim else '  '}: {obj['name']}({_match_str(obj['match'])}){cpost}\n"
        for child_id in obj['child_ids']:
            child = tree_dict[child_id]
            on_path = child['status'] in ("SUCCESS", "IN_PROGRESS")
            visible = on_path or options.get("show_alt_methods", False)
            if(visible):
                s += _dict_obj_to_str(tree_dict, child, options, 
                        recurse=on_path, depth=depth+1)

    elif(obj['kind'] == "MethodEx"):
        if(options.get("show_methods", False)):
            s = f"{cpre}{' '*depth}ME  : {obj['name']}({_match_str(obj['match'])}){cpost}\n"
        for child_id in obj['child_ids']:
            child = tree_dict[child_id]
            on_path = child['status'] in ("SUCCESS", "IN_PROGRESS")
            s += _dict_obj_to_str(tree_dict, child, options, 
                    recurse=on_path, depth=depth+1)        
    elif(obj['kind'] == "OperatorEx"):
        if(options.get("show_operators", False)):
            s = f"{cpre}{' '*depth}OE : {obj['name']}({_match_str(obj['match'])}){cpost}\n"

    return s

def tree_dict_to_str(tree_dict, show_colors=True,
                                show_methods=True, 
                                show_operators=False,
                                show_alt_methods=False):
    root_obj = None
    for k, obj in tree_dict.items():
        if(not obj['parent_id']):
            root_obj = obj

    options = {"show_colors" : show_colors,
               "show_methods" : show_methods,
               "show_operators" : show_operators,
               "show_alt_methods" : show_alt_methods}

    return _dict_obj_to_str(tree_dict, root_obj,  options)[:-1]


class TaskEx(ElementExecution):
    def __init__(self,
                 task: Task,
                 state: FactSet,
                 match: Sequence[Any] = (),
                 parent_method_exec: Any = None,
                 child_method_execs: Sequence["MethodEx"] = [],                 
                 ) -> None:

        super().__init__(task, state, match,
            parent_method_exec, child_method_execs)

    @property
    def id(self):
        return f"TE_{self._base_longhash()}"

    @property
    def task(self):
        return self.element

    @property
    def parent_method_exec(self):
        return self.parent_exec

    @property
    def child_method_execs(self):
        return self.child_execs

    def __str__(self):
        return self._str_helper("TaskEx")

    __repr__ = __str__

    def get_child_executions(self, domain, state):
        ''' Get all method executions or operator execution down stream
            of this task execution.
        ''' 
        # from pyhtn.htn.element_executions import MethodEx, TaskEx
        task = self.task
        # key = f"{task.name}/{len(task.args)}"
        methods_or_op = domain.get(self.task.name, None)
        if(methods_or_op is None):
            return None

        if(not isinstance(methods_or_op, list)):
            methods_or_op = [methods_or_op]

        child_execs = []
        for m_or_op in methods_or_op:
            child_execs += m_or_op.get_match_executions(self, state)

        self.child_execs = child_execs
        return child_execs

    def check_can_execute_skip(self, state):
        ''' Evaluate optional, optional_if, skip_if, block_if '''

        # The default: we can execute the subtask, and cannot skip it
        can_execute = True
        can_skip = False
        t = self.task

        # If optional we can skip it
        if(t.optional):
            can_skip = True

        # The rest... A condition is checked with the Task's own args
        #  bound to this TaskEx's match values.
        else:
            for kind in ('optional_if', 'skip_if', 'block_if'):
                cond = getattr(t, kind)
                if(cond is not None):
                    break
            else:
                return can_execute, can_skip

            binding = head_binding(t.args, self.match, skip_derefs=True) or {}
            if(any_match(cond, to_factset(state), binding)):
                if(kind == 'optional_if'):
                    can_skip = True
                elif(kind == 'skip_if'):
                    can_skip = True
                    can_execute = False
                elif(kind == 'block_if'):
                    can_execute = False

        return can_execute, can_skip



    def as_dict(self):
        d = super().as_dict()
        is_primitive = (
            len(self.child_execs) > 0 and
            all([isinstance(ex, OperatorEx) for ex in self.child_execs])
        )
        d['is_primitive'] = is_primitive
        return d

class MethodEx(ElementExecution):
    def __init__(self,
                 method: Method,
                 state: FactSet,
                 match: Sequence[Any] = (),
                 parent_task_exec: Any = None,
                 subtask_execs: Sequence[TaskEx] = [],
                 ) -> None:
        super().__init__(method, state, match,
            parent_task_exec, subtask_execs)

    @property
    def method(self):
        return self.element

    @property
    def parent_task_exec(self):
        return self.parent_exec

    @property
    def subtask_execs(self):
        return self.child_execs

    @property
    def id(self):
        return f"ME_{self._base_longhash()}"

    def __str__(self):
        return self._str_helper("MethodEx")

    __repr__ = __str__

class OperatorEx(ElementExecution):
    def __init__(self,
                 operator: Operator,
                 state: FactSet,
                 match: Sequence[Any] = (),
                 parent_task_exec: TaskEx = None,
                 ) -> None:
        super().__init__(operator, state, match, parent_task_exec)

    @property
    def id(self):
        return f"OE_{self._base_longhash()}"

    @property
    def operator(self):
        return self.element

    @property
    def parent_task_exec(self):
        return self.parent_exec

    def __str__(self):
        return self._str_helper("OperatorEx")

    __repr__ = __str__

