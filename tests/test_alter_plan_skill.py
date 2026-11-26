from ego.domain.templates import skill_md


def test_work_plan_skill_teaches_alter_flow_and_keeps_inline_approval():
    text = skill_md('work-plan')

    for needle in ('alter-plan push', 'alter-plan wait', 'alter-plan pull',
                   '--from <from_run>', 'ego work alter-plan sync'):
        assert needle in text
    assert 'この計画で進めてよいですか？' in text


def test_work_plan_alter_flow_pushes_before_approval_and_cli_yes_marks_ready():
    text = skill_md('work-plan')
    alter = text[text.index('When `knowledge = "alter"`'):text.index('## 3. Execute')]

    assert alter.index('alter-plan push') < alter.index('この計画で進めてよいですか？')
    assert '.baseline.json' in alter
    assert '/api/session' in alter and 'x-alter-csrf' in alter
    assert '/ego-plan/ready' in alter
    for state in ('`ready`', '`draft`, CLI yes, `same`', '`draft`, CLI yes, `edited`'):
        assert state in alter
    assert 'ALTER_URL' in alter


def test_work_plan_skill_requires_self_contained_readable_node_text():
    text = skill_md('work-plan')
    plan = text[text.index('## 2. Plan'):text.index('## 3. Execute')]

    for needle in ('understandable on its own', "in the user's language", 'the node title', 'Each step is one action'):
        assert needle in plan
    assert '1-4 observable' not in plan


def test_work_plan_skill_defines_one_purpose_per_plan_field():
    text = skill_md('work-plan')
    plan = text[text.index('## 2. Plan'):text.index('## 3. Execute')]

    for field in ('`goal`', '`done`', '`instructions`', '`intent`', '`requirement.what`', '`requirement.how`',
                  '`requirement.when`', '`requirement.where`', '`checks`', '`inputs`', '`outputs`'):
        assert field in plan
    for rule in ('Say each fact in one field only', 'Every string in a node is a single line',
                 'Omit `requirement.who` and `requirement.why`'):
        assert rule in plan
