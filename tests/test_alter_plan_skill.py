from ego.domain.templates import skill_md


def test_work_plan_skill_teaches_alter_flow_and_keeps_inline_approval():
    text = skill_md('work-plan')

    for needle in ('alter-plan push', 'alter-plan wait', 'alter-plan pull',
                   '--from <from_run>', 'ego work alter-plan sync'):
        assert needle in text
    assert 'この計画で進めてよいですか？' in text
