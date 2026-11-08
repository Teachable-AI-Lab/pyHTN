''' Tests for cre-backed matching: typed vars, bindings flowing from
    preconditions into subtasks, derefs, facts across changing states, and
    HTN construction checks.
'''
import warnings
import pytest
from cre import Var, FactType, FactSet
from pyhtn.htn import HTN, Method, Operator, Task, Unord
from pyhtn.htn import HTN_Planner
from pyhtn.htn.matching import head_binding, iter_bindings, to_factset


Block = FactType("Block", {
    "id": {"type": str, "unique_id": True},
    "on": "Block",
    "clear": bool,
})

x, y, dest = Var(Block, "x"), Var(Block, "y"), Var(Block, "dest")


class BlocksEnv:
    ''' A tiny simulated environment. get_state() returns a fresh list of
        dicts each call, so every step yields a new FactSet.
    '''
    def __init__(self, blocks):
        self.blocks = {b["id"]: dict(b) for b in blocks}
        self.log = []

    def get_state(self):
        out = []
        for b in self.blocks.values():
            d = {"type": "Block", "id": b["id"], "clear": b["clear"]}
            if(b.get("on")):
                d["on"] = "@" + b["on"]
            out.append(d)
        return out

    def execute_action(self, name, match):
        ids = [m.id for m in match]
        self.log.append((name, *ids))
        if(name == "unstack"):
            a, b = ids
            self.blocks[a]["on"] = None
            self.blocks[b]["clear"] = True
        elif(name == "stack"):
            a, b = ids
            self.blocks[a]["on"] = b
            self.blocks[b]["clear"] = False
        return True


def initial_blocks():
    # a is on b; b and c are on the table.
    return [
        {"id": "a", "on": "b", "clear": True},
        {"id": "b", "clear": False},
        {"id": "c", "clear": True},
    ]


blocks_ops = [
    Operator("unstack", x, y, pre=[x.on == y, x.clear == True]),
    Operator("stack", x, dest, pre=[x.clear == True, dest.clear == True]),
]


def run_to_end(planner, env):
    while not planner.is_exhausted():
        planner.plan_to_next_operators(multiheaded=True)
        op_execs = planner.get_next_operator_execs()
        if(len(op_execs) == 0):
            break
        planner.apply(op_execs[0])
    return env.log


def make_planner(htn, env, *args):
    return HTN_Planner(htn, tasks=[Task("move", *args)], env=env)


def test_precondition_binds_subtask_args():
    # `y` isn't in the head; it's bound by matching `pre` and then used as a
    #  subtask arg. The second subtask is expanded after the state changed,
    #  so its Facts must be re-resolved in the new FactSet.
    htn = HTN(
        *blocks_ops,
        Method("move", x, dest, pre=x.on == dest, subtasks=[]),
        Method("move", x, dest,
            pre=[x.on == y, x.clear == True, dest.clear == True],
            subtasks=[Task("unstack", x, y), Task("stack", x, dest)]),
    )
    env = BlocksEnv(initial_blocks())
    log = run_to_end(make_planner(htn, env, "a", "c"), env)
    assert log == [("unstack", "a", "b"), ("stack", "a", "c")]
    assert env.blocks["a"]["on"] == "c"


def test_deref_subtask_arg():
    # `x.on` in a subtask is evaluated on the fact bound to `x`, and the
    #  method has no `pre` at all (the head grounds "a" to its fact).
    htn = HTN(
        *blocks_ops,
        Method("move", x, dest,
            subtasks=[Task("unstack", x, x.on), Task("stack", x, dest)]),
    )
    env = BlocksEnv(initial_blocks())
    log = run_to_end(make_planner(htn, env, "a", "c"), env)
    assert log == [("unstack", "a", "b"), ("stack", "a", "c")]


def test_free_var_multiple_matches():
    # A free var in `pre` yields one MethodEx per match.
    htn = HTN(
        *blocks_ops,
        Method("put_on_any", x,
            pre=[x.clear == True, y.clear == True, y.id != "a"],
            subtasks=[Task("stack", x, y)]),
    )
    fs = to_factset(BlocksEnv(initial_blocks()).get_state())
    task_exec = Task("put_on_any", "a").as_task_exec(fs)
    method_execs = task_exec.get_child_executions(htn, fs)
    targets = sorted(me.child_execs[0].match[1].id for me in method_execs)
    assert targets == ["c"]


def test_head_constants_must_unify():
    method = Method("m", x, "fixed", subtasks=[])
    assert head_binding(method.args, ("a", "fixed")) == {"x": "a"}
    assert head_binding(method.args, ("a", "other")) is None
    assert head_binding(method.args, ("a",)) is None
    # A repeated var must bind the same value.
    assert head_binding((x, x), ("a", "a")) == {"x": "a"}
    assert head_binding((x, x), ("a", "b")) is None


def test_missing_fact_cannot_match():
    fs = to_factset(BlocksEnv(initial_blocks()).get_state())
    a = fs.get_fact(unique_id="a")
    logic = Operator("o", x, pre=x.clear == True).pre
    assert len(list(iter_bindings(logic, fs, {"x": a}))) == 1

    # The same fact in a later state that no longer contains it.
    env = BlocksEnv(initial_blocks())
    del env.blocks["a"]
    env.blocks["b"]["clear"] = True
    fs2 = to_factset(env.get_state())
    assert list(iter_bindings(logic, fs2, {"x": a})) == []
    # An id that names no fact doesn't match either.
    assert list(iter_bindings(logic, fs2, {"x": "zzz"})) == []


def test_reresolve_fact_with_non_id_unique_field():
    # Facts are re-resolved via Fact.get_unique_id(), whatever the unique
    #  member is called.
    Item = FactType("Item", {"name": {"type": str, "unique_id": True}, "ok": bool})
    it = Var(Item, "it")
    logic = Operator("o", it, pre=it.ok == True).pre

    fs1 = FactSet.from_py([{"type": "Item", "name": "w", "ok": True}])
    old = fs1.get_fact(unique_id="w")
    fs2 = FactSet.from_py([{"type": "Item", "name": "w", "ok": True}])
    (b,) = iter_bindings(logic, fs2, {"it": old})
    assert b["it"] is fs2.get_fact(unique_id="w")

    fs3 = FactSet.from_py([{"type": "Item", "name": "w", "ok": False}])
    assert list(iter_bindings(logic, fs3, {"it": old})) == []


def test_skip_if_uses_bound_args():
    # skip_if is checked with the subtask's args bound to its match values.
    htn = HTN(
        *blocks_ops,
        Method("move", x, dest,
            pre=[x.on == y],
            subtasks=[Task("unstack", x, y, skip_if=y.id == "b"),
                      Task("stack", x, dest)]),
    )
    env = BlocksEnv(initial_blocks())
    log = run_to_end(make_planner(htn, env, "a", "c"), env)
    # unstack(a, b) is skipped; stack(a, c) runs (c is clear, a is clear).
    assert log == [("stack", "a", "c")]


# ------------------------------------------------------
# : HTN construction

def test_htn_groups_in_order():
    m1 = Method("move", x, dest, subtasks=[])
    m2 = Method("move", x, dest, subtasks=[])
    htn = HTN(*blocks_ops, m1, m2)
    assert htn["move"] == [m1, m2]
    assert set(htn.keys()) == {"unstack", "stack", "move"}
    assert htn.is_primitive("stack") and not htn.is_primitive("move")


def test_htn_rejects_arity_mismatch():
    with pytest.raises(ValueError):
        HTN(Method("move", x, dest), Method("move", x))
    with pytest.raises(ValueError):
        HTN(*blocks_ops, Method("m", subtasks=[Task("stack", x)]))


def test_htn_rejects_method_operator_clash():
    with pytest.raises(ValueError):
        HTN(Operator("move", x, dest), Method("move", x, dest))


def test_htn_warns_undefined_task():
    with pytest.warns(UserWarning, match="no Methods or Operators"):
        HTN(Method("m", subtasks=[Task("not_defined")]))


def test_htn_add_remove_and_combine():
    htn = HTN(*blocks_ops)
    m = Method("move", x, dest, subtasks=[])
    htn.add(m)
    assert htn["move"] == [m]
    htn.remove(m)
    assert "move" not in htn

    combined = HTN(blocks_ops[0]) + HTN(blocks_ops[1])
    assert set(combined.keys()) == {"unstack", "stack"}


def test_pre_accepts_literal_list_or_logic():
    from cre import AND
    for pre in (x.clear == True, [x.clear == True], AND(x.clear == True)):
        op = Operator("o", x, pre=pre)
        assert op.pre is not None
    assert Operator("o", x, pre=[]).pre is None
    with pytest.raises(TypeError):
        Operator("o", x, pre=x.clear == True, preconditions=x.clear == True)
