''' Tests for HTN_Planner's planning loop: decomposition, method selection,
    root tasks, reacting to state changes, the environment interface, and
    execution trees.

    Tests marked xfail(strict=True) document known planner bugs. Each one
    starts failing (XPASS) when its bug is fixed, as a reminder to remove
    the marker.
'''
import pytest
from cre import Var, FactType
from pyhtn.htn import HTN, HTN_Planner, Method, Operator, Task, Unord
from pyhtn.htn.element_executions import ExStatus, tree_dict_to_str
from pyhtn.planner.trace import TraceKind


Switch = FactType("Switch", {"id": {"type": str, "unique_id": True}, "on": bool})
Num = FactType("Num", {"id": {"type": str, "unique_id": True}, "n": int})
S = Var(Switch, "S")

ops = [Operator(name) for name in "abcdefg"]


class Env:
    ''' Logs executed operators. `flip_after` names an operator after which
        the switch flips; operators in `fail` report failure.
    '''
    def __init__(self, on=True, fail=(), flip_after=None):
        self.on, self.fail, self.flip_after = on, set(fail), flip_after
        self.log = []

    def get_state(self):
        return [{"type": "Switch", "id": "s", "on": self.on}]

    def execute_action(self, name, args):
        self.log.append(name)
        if(name == self.flip_after):
            self.on = not self.on
        return name not in self.fail


def run_stepwise(planner, pick=0):
    ''' Plan and apply operators one at a time until exhausted. '''
    while not planner.is_exhausted():
        planner.plan_to_next_operators()
        op_execs = planner.get_next_operator_execs()
        assert len(op_execs) > 0, "planner stalled with no operators"
        planner.apply(op_execs[pick])


def make(htn, tasks, env):
    return HTN_Planner(htn, tasks=tasks, env=env)


# ------------------------------------------------------
# : Decomposition and method selection

def test_full_plan_applies_operators():
    # With no stop kinds, plan() applies operators itself.
    env = Env()
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    make(htn, [Task("S")], env).plan(multiheaded=True)
    assert env.log == ["a", "b"]


def test_nested_decomposition_order():
    env = Env()
    htn = HTN(*ops,
        Method("S", subtasks=[Task("A"), Task("g")]),
        Method("A", subtasks=[Task("B"), Task("c")]),
        Method("B", subtasks=[Task("a"), Task("b")]),
    )
    run_stepwise(make(htn, [Task("S")], env))
    assert env.log == ["a", "b", "c", "g"]


def test_first_matching_method_is_used():
    env = Env()
    htn = HTN(*ops,
        Method("S", subtasks=[Task("a")]),
        Method("S", subtasks=[Task("b")]),
    )
    run_stepwise(make(htn, [Task("S")], env))
    assert env.log == ["a"]


@pytest.mark.parametrize("on, expected", [(True, ["b"]), (False, ["a"])])
def test_precondition_selects_method(on, expected):
    env = Env(on=on)
    htn = HTN(*ops,
        Method("S", pre=S.on == False, subtasks=[Task("a")]),
        Method("S", pre=S.on == True, subtasks=[Task("b")]),
    )
    run_stepwise(make(htn, [Task("S")], env))
    assert env.log == expected


def test_later_subtasks_see_updated_state():
    # X is expanded after `a` runs and flips the switch, so X's method is
    #  chosen from the new state.
    env = Env(on=True, flip_after="a")
    htn = HTN(*ops,
        Method("S", subtasks=[Task("a"), Task("X")]),
        Method("X", pre=S.on == True, subtasks=[Task("b")]),
        Method("X", pre=S.on == False, subtasks=[Task("c")]),
    )
    run_stepwise(make(htn, [Task("S")], env))
    assert env.log == ["a", "c"]


def test_operator_matches_give_alternative_execs():
    N, M = Var(Num, "N"), Var(Num, "M")

    class NumEnv(Env):
        def get_state(self):
            return [{"type": "Num", "id": "1", "n": 1},
                    {"type": "Num", "id": "2", "n": 2},
                    {"type": "Num", "id": "3", "n": -1}]

    htn = HTN(
        Operator("pick", N, pre=N.n > 0),
        Method("S", subtasks=[Task("pick", M)]),
    )
    planner = make(htn, [Task("S")], NumEnv())
    planner.plan_to_next_operators()
    picked = sorted(oe.match[0].n for oe in planner.get_next_operator_execs())
    assert picked == [1, 2]


def test_unord_children_are_all_executable():
    env = Env()
    htn = HTN(*ops, Method("S", subtasks=[Unord(Task("a"), Task("b")), Task("c")]))
    planner = make(htn, [Task("S")], env)
    planner.plan_to_next_operators()
    names = [oe.operator.name for oe in planner.get_next_operator_execs()]
    assert names == ["a", "b"]
    planner.apply(planner.get_next_operator_execs()[1])
    run_stepwise(planner)
    assert env.log == ["b", "a", "c"]


# ------------------------------------------------------
# : Root tasks and modifying the planner

ROOTS = HTN(*ops,
    Method("S", subtasks=[Task("a")]),
    Method("T", subtasks=[Task("b")]),
    Method("U", subtasks=[Task("c")]),
)


def test_multiple_root_tasks_all_run():
    env = Env()
    run_stepwise(make(ROOTS, [Task("S"), Task("T"), Task("U")], env))
    assert sorted(env.log) == ["a", "b", "c"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Root tasks run in reverse order: RootTaskQueue.add() inserts every "
    "priority='first' task at the front of the queue."))
def test_root_tasks_run_in_order():
    env = Env()
    run_stepwise(make(ROOTS, [Task("S"), Task("T"), Task("U")], env))
    assert env.log == ["a", "b", "c"]


@pytest.mark.parametrize("root", [
    Task("Q", 7),
    {"name": "Q", "args": [7]},
    ("Q", 7),
])
def test_root_task_formats(root):
    q = Var("q")

    class LogArgs(Env):
        def execute_action(self, name, args):
            self.log.append((name, *args))
            return True

    env = LogArgs()
    htn = HTN(Operator("p", q), Method("Q", q, subtasks=[Task("p", q)]))
    run_stepwise(make(htn, [root], env))
    assert env.log == [("p", 7)]


def test_add_tasks_after_start():
    env = Env()
    planner = make(ROOTS, [Task("S")], env)
    planner.plan_to_next_operators()
    planner.apply(planner.get_next_operator_execs()[0])
    planner.add_tasks(Task("T"))
    run_stepwise(planner)
    assert env.log == ["a", "b"]


def test_add_method_after_construction():
    env = Env()
    planner = make(HTN(*ops), [Task("S")], env)
    planner.add_method(Method("S", subtasks=[Task("a")]))
    run_stepwise(planner)
    assert env.log == ["a"]


def test_dict_domain_is_wrapped():
    planner = make({"a": ops[0], "S": [Method("S", subtasks=[Task("a")])]},
                   [Task("S")], Env())
    assert isinstance(planner.domain_network, HTN)


# ------------------------------------------------------
# : Environment interface

def test_planning_without_env():
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    planner = make(htn, [Task("S")], None)
    planner.plan_to_next_operators()
    assert [oe.operator.name for oe in planner.get_next_operator_execs()] == ["a"]


def test_apply_reports_env_failure():
    env = Env(fail={"a"})
    htn = HTN(*ops, Method("S", subtasks=[Task("a")]))
    planner = make(htn, [Task("S")], env)
    planner.plan_to_next_operators()
    op_exec = planner.get_next_operator_execs()[0]
    assert planner.apply(op_exec) is False
    assert op_exec.status == ExStatus.FAILED
    assert planner.trace.entries[-1].kind == TraceKind.OPERATOR_FAIL


def test_apply_success_marks_status_and_trace():
    env = Env()
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    planner = make(htn, [Task("S")], env)
    planner.plan_to_next_operators()
    op_exec = planner.get_next_operator_execs()[0]
    assert planner.apply(op_exec) is True
    assert op_exec.status == ExStatus.SUCCESS
    assert planner.get_prev_operator_exec() is op_exec


# ------------------------------------------------------
# : Execution trees

def test_tree_to_dict_and_str():
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    planner = make(htn, [Task("S")], Env())
    planner.plan_to_next_operators()
    planner.apply(planner.get_next_operator_execs()[0])
    planner.plan_to_next_operators()
    op_exec = planner.get_next_operator_execs()[0]
    root = op_exec.parent_exec.parent_exec.parent_exec

    tree = root.tree_to_dict()
    kinds = sorted(d["kind"] for d in tree.values())
    assert kinds == ["MethodEx", "OperatorEx", "OperatorEx", "TaskEx", "TaskEx", "TaskEx"]

    s = tree_dict_to_str(tree, show_colors=False, show_operators=True)
    assert s.splitlines() == [
        "TE  : S()",
        " ME  : S()",
        "  TE-P: a()",
        "   OE : a()",
        "  TE-P: b()",
        "   OE : b()",
    ]


# ------------------------------------------------------
# : Known bugs

@pytest.mark.xfail(strict=True, raises=IndexError, reason=(
    "_attempt_conflict_set() indexes an empty list when a root task "
    "completes without reaching any operator."))
def test_root_task_with_empty_method():
    env = Env()
    run_stepwise(make(HTN(*ops, Method("S", subtasks=[])), [Task("S")], env))
    assert env.log == []


@pytest.mark.xfail(strict=True, raises=AttributeError, reason=(
    "A root task that is an operator: Cursor.advance_subtask() pops to an "
    "empty stack and then calls next_frame_after_commit() on None."))
def test_root_task_is_operator():
    env = Env()
    run_stepwise(make(HTN(*ops), [Task("a")], env))
    assert env.log == ["a"]


@pytest.mark.xfail(strict=True, raises=UnboundLocalError, reason=(
    "Single-headed planning is unfinished: plan(multiheaded=False) "
    "references an undefined `child_exec`, and never checks stop_kinds."))
def test_single_headed_plan():
    env = Env()
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    make(htn, [Task("S")], env).plan(multiheaded=False)
    assert env.log == ["a", "b"]


@pytest.mark.xfail(strict=True, raises=UnboundLocalError, reason=(
    "plan_to_next_decomposition() runs the unfinished single-headed path."))
def test_plan_to_next_decomposition():
    htn = HTN(*ops, Method("S", subtasks=[Task("a")]))
    planner = make(htn, [Task("S")], Env())
    planner.plan_to_next_decomposition()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Backtracking after an operator fails: Cursor.backtrack() reads "
    "current_method_exec on an operator frame. Beyond that, multiheaded "
    "cursors keep only their chosen method, and "
    "FrameContext.next_child_frame() calls a misnamed "
    "_next_child_exec_ind()."))
def test_backtrack_to_next_method_after_operator_failure():
    env = Env(fail={"a"})
    htn = HTN(*ops,
        Method("S", subtasks=[Task("a")]),
        Method("S", subtasks=[Task("b")]),
    )
    make(htn, [Task("S")], env).plan(multiheaded=True)
    assert env.log == ["a", "b"]


@pytest.mark.xfail(strict=True, raises=TypeError, reason=(
    "Tasks with no matching Method/Operator: _expand_taskex() calls "
    "push_nomatch_frame() without `state`, and FrameContext assumes "
    "child_execs is a list."))
def test_task_with_no_matches():
    env = Env(on=True)
    htn = HTN(*ops,
        Method("S", subtasks=[Task("X")]),
        Method("X", pre=S.on == False, subtasks=[Task("a")]),
    )
    planner = make(htn, [Task("S")], env)
    planner.plan_to_next_operators()
    assert planner.get_next_operator_execs() == []


@pytest.mark.xfail(strict=True, raises=AttributeError, reason=(
    "Cursor.backtrack() is passed a TraceKind as `state` by "
    "_expand_taskex() when push_nomatch_frame=False."))
def test_task_with_no_matches_backtracks():
    env = Env(on=True)
    htn = HTN(*ops,
        Method("S", subtasks=[Task("X")]),
        Method("S", subtasks=[Task("b")]),
        Method("X", pre=S.on == False, subtasks=[Task("a")]),
    )
    planner = make(htn, [Task("S")], env)
    planner.plan_to_next_operators(push_nomatch_frame=False)
    assert [oe.operator.name for oe in planner.get_next_operator_execs()] == ["b"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Trace.get_current_plan() looks for entry_type == 'operator' instead "
    "of TraceKind.APPLY_OPERATOR, so it always returns []."))
def test_get_current_plan():
    htn = HTN(*ops, Method("S", subtasks=[Task("a"), Task("b")]))
    planner = make(htn, [Task("S")], Env())
    planner.plan(multiheaded=True)
    assert [oe.operator.name for oe in planner.get_current_plan()] == ["a", "b"]


@pytest.mark.parametrize("call", [
    lambda p: p.get_next_method_execs(),
    lambda p: p.get_current_root(),
    lambda p: p.clear_tasks(),
], ids=["get_next_method_execs", "get_current_root", "clear_tasks"])
@pytest.mark.xfail(strict=True, raises=AttributeError, reason=(
    "Uses self.cursor, which HTN_Planner replaced with self.cursors."))
def test_cursor_accessors(call):
    htn = HTN(*ops, Method("S", subtasks=[Task("a")]))
    planner = make(htn, [Task("S")], Env())
    planner.plan_to_next_operators()
    call(planner)
