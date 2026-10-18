from mira_engine.modules.prompts import module_confirmation_instructions, module_system_instructions


def test_clarification_is_shared_by_all_modules_and_independent_of_approval():
    for module in ("initiation", "planning", "experiment", "delivery"):
        prompt = module_system_instructions(module, "test")
        for requirement in (
            "EVERY module", "clarify_requirements", "resolve_conflict",
            "resolved_interactions", "Do not re-ask settled questions",
            "never offer to override safety rules",
        ):
            assert requirement in prompt
        assert "does not disable clarification" in module_confirmation_instructions(
            module, required=False, approved=True, interaction_type="",
        )


def test_research_outcome_clarification_and_actionable_synthesis_contract():
    prompt = module_system_instructions("initiation", "research")
    for requirement in (
        "Before substantive external research", "intended output direction",
        "let the AI propose a focus", "not a quota", "reference IDs",
        "data/resource requirements", "baseline and success criterion",
        "overview/comparison", "only start a\nseparate downstream Run",
    ):
        assert requirement in prompt


def test_experiment_plans_a_full_initial_set_before_adapting():
    prompt = module_system_instructions("experiment", "test")
    for requirement in (
        "COMPLETE\nset of top-level experiments", "not only a first experiment",
        "never pad it", "re-planning refines this plan",
    ):
        assert requirement in prompt
    brief = module_confirmation_instructions(
        "experiment", required=True, approved=False, interaction_type="approve_experiment_plan",
    )
    assert "full initial experiment plan listing every planned" in brief
