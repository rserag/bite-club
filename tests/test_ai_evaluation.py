"""Offline model gates remain distinct from application authorization guarantees."""

import base64
import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nutrition_bot.application.ai_evaluation import (
    EVALUATION_THRESHOLDS,
    REPORT_VERSION,
    SCORER_VERSION,
    EvaluationCase,
    _case_score,
    _model_score,
    case_photo,
    evaluate_report,
    load_cases,
    main,
)
from nutrition_bot.domain.ai import (
    AiCatalogItem,
    AiDiscoveryItem,
    AiMealIntent,
    AiMealItem,
    AiOutcome,
    AiUnavailable,
)

TODAY = date(2026, 10, 9)
EVALS = Path(__file__).resolve().parents[1] / "evals"


def source_evidence(case, item):
    # Synthetic oracle only: these food expressions are independent of the
    # evaluator's matching code and never perform model or application calls.
    patterns = {
        1: r"rice",
        2: r"rice",
        3: r"chicken breast",
        4: r"chicken breast",
        5: r"yogurt",
        6: r"banana",
        7: r"oatmeal",
        8: r"carrots?",
        9: r"carrots?",
        10: r"olive oil",
        11: r"potato(?:es)?",
        12: r"bell pepper",
        13: r"onion",
        14: r"tomato sauce",
        15: r"egg",
        16: r"bread",
        21: r"chicken breast",
        22: r"chicken breast",
        23: r"rice",
    }
    pattern = patterns.get(item.food_version_id)
    if pattern is None and isinstance(item, AiDiscoveryItem):
        pattern = (
            "mango"
            if "mango" in item.query.casefold()
            else "chicken breast"
            if "breast" in item.query.casefold()
            else "rice"
            if "rice" in item.query.casefold()
            else "chicken drumstick"
        )
    names = list(re.finditer(rf"\b(?:{pattern})\b", case.text, re.IGNORECASE)) if pattern else []
    identity = names[0][0] if names else None
    evidence = None
    if item.grams is not None and names:
        masses = []
        for match in re.finditer(
            r"([0-9]+(?:[.,][0-9]+)?)\s*(kg|mg|grams?|g)\b", case.text, re.IGNORECASE
        ):
            factor = (
                Decimal(1000)
                if match[2].casefold() == "kg"
                else Decimal("0.001")
                if match[2].casefold() == "mg"
                else Decimal(1)
            )
            if Decimal(match[1].replace(",", ".")) * factor == Decimal(item.grams):
                masses.append(match)
        spans = [
            (min(name.start(), mass.start()), max(name.end(), mass.end()))
            for name in names
            for mass in masses
            if not any(
                separator
                in case.text[min(name.start(), mass.start()) : max(name.end(), mass.end())]
                for separator in (";", "\n")
            )
        ]
        if spans:
            start, end = min(spans, key=lambda span: span[1] - span[0])
            evidence = case.text[start:end]
    return item.model_copy(update={"identity_evidence": identity, "quantity_evidence": evidence})


def synthetic_case(case_id="synthetic-rice", **changes):
    fields = {
        "id": case_id,
        "synthetic": True,
        "role": "meal_text",
        "text": "Cooked rice weighed 150g.",
        "catalog": (AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
        "context_local_date": TODAY,
        "expected_intent": "meal",
        "expected_items": (
            AiMealItem(
                food_version_id=1,
                grams="150",
                basis="Synthetic",
                quantity_source="reported",
                uncertainty="none",
            ),
        ),
    }
    return EvaluationCase.model_validate({**fields, **changes})


def expected_outcome(case, **changes):
    questions = tuple(group[0] for group in case.clarification_groups)
    if "weighing_preparation" in case.clarification_concepts:
        questions += ("Was the weight measured raw or cooked?",)
    proposal = {
        "intent": case.expected_intent,
        "local_date": TODAY if case.expected_local_date == "context" else case.expected_local_date,
        "label": case.expected_label or "Meal",
        "items": tuple(source_evidence(case, item) for item in case.expected_items),
        "unresolved": questions,
        "full_input_accounted_for": True,
        "clarification_reason": case.expected_clarification_reason or "incomplete",
        "discovery_items": tuple(
            source_evidence(case, item) for item in case.expected_discovery_items
        ),
        **changes,
    }
    return AiOutcome(
        request_key=f"eval:synthetic-run:{case.id}",
        role=case.role,
        status="ready" if proposal["intent"] == "meal" else "clarify",
        proposal=AiMealIntent.model_validate(proposal),
    )


class SyntheticService:
    evaluation_prefix = "eval:synthetic-run:"
    evaluation_catalog = ()

    def __init__(self, cases, *, status=None):
        self.cases = {case.id: case for case in cases}
        self.status = status
        self.called = []
        self.evaluation_raw_proposal = None

    async def interpret(self, *, request_key, text, local_date, photo):
        case_id = request_key.removeprefix(self.evaluation_prefix)
        self.called.append(case_id)
        case = self.cases[case_id]
        assert text == case.text and local_date == case.context_local_date
        if self.status:
            return AiOutcome(request_key=request_key, role=case.role, status=self.status)
        outcome = expected_outcome(case)
        self.evaluation_raw_proposal = outcome.proposal
        return outcome


async def known_measurement(request_key):
    return {
        "inference_ms": 150,
        "input_tokens": 0,
        "output_tokens": 40,
        "charged_micro_usd": None,
        "inference_sent": True,
        "attempts_sent": 1,
    }


def test_review_fixtures_are_representative_reproducible_and_not_blank_photos():
    text = load_cases(EVALS / "ai_meal_review_cases.jsonl")
    photos = load_cases(EVALS / "ai_photo_review_cases.jsonl")
    assert len(text) >= 40 and len(photos) == 20
    assert sum(case.split == "held_out" for case in text) >= 8
    assert sum(case.split == "held_out" for case in photos) >= 4
    assert all(case.context_local_date == TODAY for case in text + photos)
    image_data = [case_photo(case, fixture_root=EVALS) for case in photos]
    assert len({hashlib.sha256(data).hexdigest() for data in image_data}) == 10
    assert all(data is not None and 10_000 < len(data) <= 2_000_000 for data in image_data)
    assert all(case.image_reviewed for case in photos)


@pytest.mark.parametrize("expected_date", ["context", date(2026, 10, 8)])
def test_case_score_checks_fixed_context_and_user_date(expected_date):
    case = synthetic_case(expected_local_date=expected_date)
    assert _case_score(case, expected_outcome(case))["date"]
    assert not _case_score(case, expected_outcome(case, local_date=date(2026, 10, 7)))["date"]


def test_quantities_uncertainty_and_completeness_are_independent():
    case = synthetic_case()
    wrong_mass = (AiMealItem(food_version_id=1, grams="150.001", basis="Synthetic"),)
    score = _case_score(case, expected_outcome(case, items=wrong_mass))
    assert score["food_matching"] and score["completeness"]
    assert not score["quantity_handling"] and not score["uncertainty"]
    assert not score["correct"]

    unresolved = synthetic_case(
        expected_items=(
            AiMealItem(
                food_version_id=1,
                grams=None,
                basis="Synthetic",
                quantity_source="unresolved",
                uncertainty="portion_missing",
            ),
        )
    )
    assert not _case_score(unresolved, expected_outcome(unresolved, items=case.expected_items))[
        "quantity_handling"
    ]


def test_clarification_must_address_declared_missing_information():
    case = synthetic_case(
        expected_intent="clarify",
        expected_items=(),
        category="ambiguity",
        expected_clarification_reason="preparation",
        clarification_groups=(("raw", "cooked"),),
    )
    assert _case_score(case, expected_outcome(case))["correct"]
    score = _case_score(case, expected_outcome(case, unresolved=("Please provide grams",)))
    assert score["intent"] and not score["clarification"] and not score["correct"]


@pytest.mark.parametrize(
    "question",
    [
        "Was the 150g of rice weighed before or after cooking?",
        "Were the quantities measured before cooking or after cooking?",
        "Was the scale weight raw or cooked?",
        "Were these pre-cooking or post-cooking measurements?",
        "Please confirm whether the amount was raw or cooked.",
    ],
)
def test_declared_weighing_state_concept_accepts_equivalent_questions(question):
    case = next(
        case
        for case in load_cases(EVALS / "ai_meal_review_cases.jsonl")
        if case.id == "text-rice-preparation"
    )
    outcome = expected_outcome(case, unresolved=(question,))
    assert _case_score(case, outcome)["correct"]
    assert _model_score(case, outcome.proposal)["correct"]


@pytest.mark.parametrize(
    "question",
    [
        "How was the rice prepared?",
        "What weight or quantity did you measure?",
        "Was the rice cooked without oil?",
        "Was the cooking done before or after lunch?",
        "Was the cooked rice weighed before lunch or after cooking?",
        "The quantity was raw or cooked.",
        "Do not ask whether the weight was raw or cooked?",
        "Was the weight recorded? Was the rice raw or cooked?",
    ],
)
def test_weighing_state_concept_rejects_incomplete_unrelated_or_negated_questions(question):
    case = next(
        case
        for case in load_cases(EVALS / "ai_meal_review_cases.jsonl")
        if case.id == "text-rice-preparation"
    )
    score = _case_score(case, expected_outcome(case, unresolved=(question,)))
    assert not score["clarification"] and not score["correct"]


@pytest.mark.parametrize("reason", ["preparation", "food_identity"])
def test_generic_chicken_photo_accepts_declared_reason_overlap_only_with_weight_basis(reason):
    case = next(
        case
        for case in load_cases(EVALS / "ai_photo_review_cases.jsonl")
        if case.id == "photo-raw-preparation-unclear"
    )
    outcome = expected_outcome(
        case,
        clarification_reason=reason,
        unresolved=(
            "What cut of chicken is this?",
            "How were the chicken and rice prepared?",
            "Were the quantities measured before or after cooking?",
        ),
    )
    assert _case_score(case, outcome)["correct"]
    assert _model_score(case, outcome.proposal)["correct"]
    missing_basis = expected_outcome(
        case, clarification_reason=reason, unresolved=("How were the foods prepared?",)
    )
    assert not _case_score(case, missing_basis)["correct"]


@pytest.mark.parametrize("reason", ["image", "incomplete", "portion", "unsupported"])
def test_generic_chicken_photo_does_not_accept_unrelated_reason(reason):
    case = next(
        case
        for case in load_cases(EVALS / "ai_photo_review_cases.jsonl")
        if case.id == "photo-raw-preparation-unclear"
    )
    assert not _case_score(case, expected_outcome(case, clarification_reason=reason))["correct"]


def test_weighing_concept_does_not_replace_other_declared_questions_or_reason_policy():
    cases = {case.id: case for case in load_cases(EVALS / "ai_photo_review_cases.jsonl")}
    potato = cases["photo-potato-preparation-unclear"]
    assert not _case_score(
        potato,
        expected_outcome(potato, unresolved=("Was the weight measured before or after cooking?",)),
    )["correct"]
    drumstick = cases["photo-drumstick-identity-unclear"]
    assert not _case_score(
        drumstick, expected_outcome(drumstick, unresolved=("What chicken cut is this?",))
    )["correct"]
    assert not _case_score(
        drumstick, expected_outcome(drumstick, clarification_reason="preparation")
    )["correct"]


def test_clarification_alternatives_cannot_accept_partial_or_numeric_meal_guesses():
    case = next(
        case
        for case in load_cases(EVALS / "ai_photo_review_cases.jsonl")
        if case.id == "photo-raw-preparation-unclear"
    )
    safe = expected_outcome(case).proposal
    guess = safe.model_copy(
        update={
            "intent": "meal",
            "unresolved": (),
            "items": (
                AiMealItem(
                    food_version_id=3,
                    grams="150",
                    basis="Synthetic guess",
                    quantity_source="reported",
                    uncertainty="none",
                ),
            ),
        }
    )
    score = _model_score(case, guess)
    assert not score["intent"] and not score["quantity_handling"]
    assert not score["no_unsupported_mass"] and not score["quantity_evidence_valid"]
    assert not score["correct"]


def test_reason_and_question_metadata_changes_stay_scoped_to_two_existing_cases():
    text = load_cases(EVALS / "ai_meal_review_cases.jsonl")
    photos = load_cases(EVALS / "ai_photo_review_cases.jsonl")
    assert len(text) == 54 and len(photos) == 20
    assert {case.id for case in text + photos if case.clarification_concepts} == {
        "text-rice-preparation",
        "photo-raw-preparation-unclear",
    }
    assert {case.id for case in text + photos if case.expected_clarification_reasons} == {
        "photo-raw-preparation-unclear"
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_clarification_reasons": ("preparation", "preparation")},
        {
            "expected_clarification_reason": "preparation",
            "expected_clarification_reasons": ("food_identity",),
        },
        {"clarification_concepts": ("unbounded_model_judgment",)},
    ],
)
def test_clarification_metadata_requires_bounded_unambiguous_declarations(changes):
    with pytest.raises(ValueError):
        synthetic_case(expected_intent="clarify", expected_items=(), **changes)
    with pytest.raises(ValueError):
        synthetic_case(expected_clarification_reasons=("preparation",))


def test_scorer_v3_keeps_all_numeric_and_evidence_gate_thresholds():
    assert SCORER_VERSION == "meal-rubric-v3"
    assert EVALUATION_THRESHOLDS == {
        "minimum_text_cases": 40,
        "minimum_photo_cases": 20,
        "clear_accuracy": 0.95,
        "schema_validity": 0.98,
        "ambiguity_accuracy": 1.0,
        "safety_accuracy": 1.0,
        "completeness_accuracy": 1.0,
        "date_accuracy": 1.0,
        "preparation_accuracy": 1.0,
        "inference_p95_ms": 30_000,
        "model_evidence_coverage": 1.0,
        "unsupported_model_mass": 0.0,
    }


async def test_text_and_photo_gates_pass_separately_with_explicit_mocked_measurements():
    # This proves scoring/gate mechanics only; it is deliberately not live model evidence.
    text = load_cases(EVALS / "ai_meal_review_cases.jsonl")
    photos = load_cases(EVALS / "ai_photo_review_cases.jsonl")
    cases = text + photos
    report = await evaluate_report(
        SyntheticService(cases),
        cases,
        run_id="synthetic-run",
        fixture_root=EVALS,
        measurement_reader=known_measurement,
    )
    assert report["summary"]["passed"]
    assert report["summary"]["roles"]["meal_text"]["passed"]
    photo_report = report["summary"]["roles"]["meal_photo"]
    assert photo_report["passed"] and photo_report["latency"]["inference_p95_ms"] == 150
    assert photo_report["usage"]["input_tokens"] == {"known": 20, "unknown": 0, "total": 0}
    assert photo_report["usage"]["charged_micro_usd"] == {"known": 0, "unknown": 20, "total": None}


async def test_missing_measurements_and_insufficient_cases_cannot_pass_gate():
    case = synthetic_case()
    report = await evaluate_report(SyntheticService((case,)), (case,), run_id="synthetic-run")
    role = report["summary"]["roles"]["meal_text"]
    assert not role["passed"] and not role["checks"]["minimum_cases"]
    assert not role["checks"]["inference_latency_known"]
    assert role["usage"]["input_tokens"]["total"] is None


async def test_private_per_case_report_resumes_without_repeating_and_caps_additional_cases(
    tmp_path,
):
    cases = (synthetic_case("first"), synthetic_case("second"), synthetic_case("third"))
    path = tmp_path / "owner-only" / "report.json"
    service = SyntheticService(cases)
    first = await evaluate_report(
        service,
        cases,
        run_id="synthetic-run",
        max_cases=1,
        report_path=path,
        measurement_reader=known_measurement,
    )
    assert len(first["cases"]) == 1 and service.called == ["first"]
    assert path.stat().st_mode & 0o077 == 0
    second = await evaluate_report(
        service,
        cases,
        run_id="synthetic-run",
        max_cases=1,
        report_path=path,
        measurement_reader=known_measurement,
    )
    assert len(second["cases"]) == 2 and service.called == ["first", "second"]
    assert json.loads(path.read_text())["cases"][1]["id"] == "second"
    with pytest.raises(AiUnavailable, match="changed cases"):
        await evaluate_report(service, cases[:1], run_id="synthetic-run", report_path=path)
    assert service.called == ["first", "second"]


@pytest.mark.parametrize("status", ["unknown", "quota", "budget", "disabled"])
async def test_unknown_or_exhausted_route_stops_bounded_run_and_preserves_outcome(tmp_path, status):
    cases = (synthetic_case("first"), synthetic_case("second"))
    path = tmp_path / "owner-only" / "report.json"
    service = SyntheticService(cases, status=status)
    report = await evaluate_report(service, cases, run_id="synthetic-run", report_path=path)
    assert service.called == ["first"] and report["cases"][0]["status"] == status
    assert not report["summary"]["passed"]


async def test_insecure_report_directory_is_rejected_before_inference(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    case = synthetic_case()
    service = SyntheticService((case,))
    with pytest.raises(AiUnavailable, match="owner-only"):
        await evaluate_report(
            service, (case,), run_id="synthetic-run", report_path=directory / "report.json"
        )
    assert service.called == []


def test_photo_paths_need_snapshot_hash_and_cannot_escape_fixture_directory(tmp_path):
    photo_case = load_cases(EVALS / "ai_photo_review_cases.jsonl")[0]
    with pytest.raises(ValueError):
        photo_case.model_copy(update={"photo_path": "../secret.png"}).model_validate(
            {**photo_case.model_dump(), "photo_path": "../secret.png"}
        )
    changed = photo_case.model_copy(update={"photo_sha256": "0" * 64})
    with pytest.raises(AiUnavailable, match="snapshot"):
        case_photo(changed, fixture_root=EVALS)
    with pytest.raises(AiUnavailable, match="exactly one"):
        case_photo(
            photo_case.model_copy(update={"photo_base64": base64.b64encode(b"x").decode()}),
            fixture_root=EVALS,
        )


def test_cli_validates_selected_development_cases_without_external_calls(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "ai-eval",
            "--cases",
            str(EVALS / "ai_meal_review_cases.jsonl"),
            "--run-id",
            "synthetic-run",
            "--split",
            "development",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out) == {"validated_cases": 36, "external_requests": 0}


def test_cli_live_requires_private_resumable_report_before_loading_credentials(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["ai-eval", "--cases", "missing", "--run-id", "synthetic-run", "--live"]
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


async def test_measurements_read_exact_durable_case_and_keep_unknown_usage_unknown(store):
    from nutrition_bot.application.ai_evaluation import _read_measurement
    from nutrition_bot.application.ai_metrics import record_metric

    async with store.write() as connection:
        assert await record_metric(
            connection,
            request_key="eval:synthetic-run:exact",
            role="meal_text",
            source="evaluation",
            handling="ai",
            status="ready",
            created_at=1,
            model="synthetic-model",
            inference_ms=100,
            input_tokens=0,
            output_tokens=None,
            charged_micro_usd=None,
            inference_sent=True,
            attempts_sent=1,
        )
        assert await record_metric(
            connection,
            request_key="eval:unrelated-run:exact",
            role="meal_text",
            source="evaluation",
            handling="ai",
            status="ready",
            created_at=1,
            model="unrelated-model",
            input_tokens=999,
            inference_sent=True,
            attempts_sent=1,
        )
    measurement = await _read_measurement(store, "eval:synthetic-run:exact")
    assert measurement["model"] == "synthetic-model"
    assert measurement["input_tokens"] == 0
    assert measurement["output_tokens"] is None and measurement["charged_micro_usd"] is None
    assert "request_key" not in measurement and "proposal" not in measurement
    assert await _read_measurement(store, "eval:synthetic-run:absent") == {}


def test_raw_model_guess_cannot_be_credited_for_application_clamping_to_unknown():
    case = load_cases(EVALS / "ai_photo_review_cases.jsonl")[1]
    raw_items = tuple(
        item.model_copy(
            update={
                "grams": "150",
                "quantity_source": "approximate",
                "uncertainty": "user_approximation",
            }
        )
        for item in case.expected_items
    )
    guessed = expected_outcome(case, items=raw_items).proposal
    normalized = expected_outcome(case)
    assert _case_score(case, normalized)["correct"]
    score = _model_score(case, guessed)
    assert not score["quantity_handling"]
    assert not score["uncertainty"]
    assert not score["no_unsupported_mass"] and not score["correct"]


def test_model_uncertainty_loss_is_independent_of_application_repair():
    case = next(
        c
        for c in load_cases(EVALS / "ai_meal_review_cases.jsonl")
        if c.id == "text-reported-approx"
    )
    raw_items = tuple(
        item.model_copy(update={"quantity_source": "reported", "uncertainty": "none"})
        for item in case.expected_items
    )
    raw = expected_outcome(case, items=raw_items).proposal
    scores = _model_score(case, raw)
    assert scores["quantity_handling"] and scores["no_unsupported_mass"]
    assert not scores["uncertainty"] and not scores["correct"]
    assert _case_score(case, expected_outcome(case))["correct"]


async def test_raw_and_normalized_gates_are_separate_and_raw_evidence_is_consumed():
    cases = load_cases(EVALS / "ai_photo_review_cases.jsonl")

    class MaskedGuessService(SyntheticService):
        async def interpret(self, **kwargs):
            normalized = await super().interpret(**kwargs)
            if normalized.request_key.endswith("photo-plate-unweighed"):
                raw_items = tuple(
                    item.model_copy(
                        update={
                            "grams": "150",
                            "quantity_source": "approximate",
                            "uncertainty": "user_approximation",
                        }
                    )
                    for item in normalized.proposal.items
                )
                self.evaluation_raw_proposal = normalized.proposal.model_copy(
                    update={"items": raw_items}
                )
            return normalized

    service = MaskedGuessService(cases)
    report = await evaluate_report(
        service,
        cases,
        run_id="synthetic-run",
        fixture_root=EVALS,
        measurement_reader=known_measurement,
    )
    role = report["summary"]["roles"]["meal_photo"]
    assert role["application_ambiguity_accuracy"] == 1.0
    assert role["application_metrics"]["correct"] == 1.0
    assert role["ambiguity_accuracy"] < 1.0
    assert not role["checks"]["no_unsupported_model_mass"] and not role["passed"]
    assert service.evaluation_raw_proposal is None
    record = next(r for r in report["cases"] if r["id"] == "photo-plate-unweighed")
    assert record["scores"]["correct"] and not record["model_scores"]["correct"]
    assert record["model_raw_proposal"]["items"][0]["grams"] == "150"
    assert record["proposal"]["items"][0]["grams"] is None


async def test_missing_raw_model_evidence_remains_unknown_on_replay_and_cannot_pass(tmp_path):
    cases = load_cases(EVALS / "ai_meal_review_cases.jsonl")

    class NoRawEvidenceService(SyntheticService):
        async def interpret(self, **kwargs):
            outcome = await super().interpret(**kwargs)
            self.evaluation_raw_proposal = None
            return outcome

    service = NoRawEvidenceService(cases)
    path = tmp_path / "owner-only" / "report.json"
    report = await evaluate_report(
        service,
        cases,
        run_id="synthetic-run",
        report_path=path,
        measurement_reader=known_measurement,
    )
    role = report["summary"]["roles"]["meal_text"]
    assert role["application_metrics"]["correct"] == 1.0
    assert role["model_evidence"] == {"known": 0, "unknown": len(cases)}
    assert not role["checks"]["model_evidence_coverage"] and not role["passed"]
    called = len(service.called)
    resumed = await evaluate_report(
        service,
        cases,
        run_id="synthetic-run",
        report_path=path,
        measurement_reader=known_measurement,
    )
    assert len(service.called) == called and not resumed["summary"]["passed"]


def test_discovery_items_contribute_to_matching_quantity_preparation_and_uncertainty_metrics():
    case = next(
        c for c in load_cases(EVALS / "ai_meal_review_cases.jsonl") if c.id == "text-unknown-mango"
    )
    assert _model_score(case, expected_outcome(case).proposal)["correct"]
    absent = expected_outcome(case, discovery_items=()).proposal
    absent_scores = _model_score(case, absent)
    assert not absent_scores["food_matching"]
    assert not absent_scores["quantity_handling"]
    assert not absent_scores["preparation"] and not absent_scores["uncertainty"]
    changed = tuple(
        item.model_copy(update={"grams": "140", "preparation": "cooked"})
        for item in case.expected_discovery_items
    )
    changed_scores = _model_score(case, expected_outcome(case, discovery_items=changed).proposal)
    assert changed_scores["food_matching"]
    assert not changed_scores["quantity_handling"] and not changed_scores["preparation"]
    assert not changed_scores["no_unsupported_mass"]


@pytest.mark.parametrize(
    ("case_id", "query"),
    [
        ("text-unknown-mango", "Raw peeled mango flesh"),
        (
            "text-grilled-stewed-mismatch",
            "Chicken breast, grilled without added fat, weighed cooked",
        ),
        (
            "text-unenriched-enriched-mismatch",
            "Unenriched white long-grain rice cooked in water without added fat",
        ),
        ("photo-drumstick-edible-unclear", "Cooked chicken drumstick with skin and bone"),
    ],
)
def test_faithful_source_discovery_descriptors_match_without_invented_mass_false_failures(
    case_id, query
):
    cases = load_cases(EVALS / "ai_meal_review_cases.jsonl") + load_cases(
        EVALS / "ai_photo_review_cases.jsonl"
    )
    case = next(case for case in cases if case.id == case_id)
    proposal = expected_outcome(case).proposal
    items = tuple(item.model_copy(update={"query": query}) for item in proposal.discovery_items)
    proposal = proposal.model_copy(update={"discovery_items": items})
    score = _model_score(case, proposal)
    assert score["food_matching"] and score["no_unsupported_mass"] and score["correct"]


@pytest.mark.parametrize(
    ("case_id", "query"),
    [
        ("text-unknown-mango", "Mango chicken"),
        ("text-unknown-mango", "Cooked mango"),
        ("text-unknown-mango", "Mango olive oil"),
        ("text-grilled-stewed-mismatch", "Stewed chicken breast"),
        ("text-grilled-stewed-mismatch", "Grilled stewed chicken breast"),
        ("text-fried-stewed-mismatch", "Fried skinless chicken breast"),
        ("text-unenriched-enriched-mismatch", "Enriched white long-grain rice"),
        ("photo-drumstick-edible-unclear", "Cooked chicken breast with skin and bone"),
    ],
)
def test_discovery_matching_rejects_wrong_food_and_preparation_attributes(case_id, query):
    cases = load_cases(EVALS / "ai_meal_review_cases.jsonl") + load_cases(
        EVALS / "ai_photo_review_cases.jsonl"
    )
    case = next(case for case in cases if case.id == case_id)
    proposal = expected_outcome(case).proposal
    proposal = proposal.model_copy(
        update={
            "discovery_items": tuple(
                item.model_copy(update={"query": query}) for item in proposal.discovery_items
            )
        }
    )
    score = _model_score(case, proposal)
    assert not score["food_matching"] and not score["correct"]
    if proposal.discovery_items[0].grams is not None:
        assert not score["no_unsupported_mass"]


def test_discovery_descriptor_matching_does_not_credit_wrong_quantity_or_reordered_foods():
    case = next(
        case
        for case in load_cases(EVALS / "ai_meal_review_cases.jsonl")
        if case.id == "text-unknown-mixed"
    )
    proposal = expected_outcome(case).proposal
    mango = proposal.discovery_items[1].model_copy(
        update={"query": "raw peeled mango flesh", "grams": "150"}
    )
    wrong = proposal.model_copy(update={"discovery_items": (proposal.discovery_items[0], mango)})
    score = _model_score(case, wrong)
    assert score["food_matching"] and not score["quantity_handling"]
    assert not score["no_unsupported_mass"]
    reversed_items = proposal.model_copy(
        update={"discovery_items": tuple(reversed(proposal.discovery_items))}
    )
    assert not _model_score(case, reversed_items)["food_matching"]


@pytest.mark.parametrize(
    ("field", "evidence", "metric"),
    [
        ("identity_evidence", "Cooked Rice", "identity_evidence_valid"),
        ("identity_evidence", "Cooked", "identity_evidence_valid"),
        ("identity_evidence", None, "identity_evidence_valid"),
        ("quantity_evidence", "150g", "quantity_evidence_valid"),
        ("quantity_evidence", "Cooked rice weighed", "quantity_evidence_valid"),
        ("quantity_evidence", "Cooked rice weighed 151g.", "quantity_evidence_valid"),
        ("quantity_evidence", None, "quantity_evidence_valid"),
    ],
)
def test_raw_evidence_requires_exact_bounded_food_and_mass_source_spans(field, evidence, metric):
    case = synthetic_case()
    proposal = expected_outcome(case).proposal
    assert _model_score(case, proposal)["correct"]
    changed = proposal.items[0].model_copy(update={field: evidence})
    score = _model_score(case, proposal.model_copy(update={"items": (changed,)}))
    assert score["schema_valid"] and score["quantity_handling"]
    assert not score[metric] and not score["correct"]


def test_raw_evidence_cannot_shave_a_source_mass_sign_or_borrow_across_foods():
    signed_case = synthetic_case(text="Cooked rice weighed -150g rice.")
    proposal = expected_outcome(signed_case).proposal
    item = proposal.items[0].model_copy(update={"quantity_evidence": "150g rice"})
    assert not _model_score(signed_case, proposal.model_copy(update={"items": (item,)}))[
        "quantity_evidence_valid"
    ]

    case = synthetic_case(
        text="Cooked rice 150g and cooked chicken breast 150g.",
        catalog=(
            AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),
            AiCatalogItem(food_version_id=3, name="Chicken breast", preparation="cooked"),
        ),
        expected_items=(
            AiMealItem(
                food_version_id=1,
                grams="150",
                basis="Synthetic",
                quantity_source="reported",
                uncertainty="none",
            ),
            AiMealItem(
                food_version_id=3,
                grams="150",
                basis="Synthetic",
                quantity_source="reported",
                uncertainty="none",
            ),
        ),
    )
    proposal = expected_outcome(case).proposal
    chicken = proposal.items[1].model_copy(
        update={"quantity_evidence": "150g and cooked chicken breast"}
    )
    score = _model_score(case, proposal.model_copy(update={"items": (proposal.items[0], chicken)}))
    assert score["quantity_handling"] and score["no_unsupported_mass"]
    assert not score["quantity_evidence_valid"] and not score["correct"]


@pytest.mark.parametrize(
    ("case_id", "field", "evidence", "metric"),
    [
        (
            "photo-oatmeal-missing-topping",
            "identity_evidence",
            "Prepared oatmeal with raw banana slices. No milk, sugar, 油",
            "identity_evidence_valid",
        ),
        (
            "photo-potato-measured",
            "quantity_evidence",
            "These are boiled potatoes including edible skin, without added ingredients. "
            "Their cooked edible weight was measured at ,",
            "quantity_evidence_valid",
        ),
    ],
)
def test_corrupt_photo_evidence_cannot_pass_raw_quality_even_when_ids_and_grams_match(
    case_id, field, evidence, metric
):
    case = next(
        case for case in load_cases(EVALS / "ai_photo_review_cases.jsonl") if case.id == case_id
    )
    proposal = expected_outcome(case).proposal
    changed = proposal.items[0].model_copy(update={field: evidence})
    score = _model_score(
        case, proposal.model_copy(update={"items": (changed, *proposal.items[1:])})
    )
    assert score["food_matching"] and score["quantity_handling"] and score["no_unsupported_mass"]
    assert not score[metric] and not score["correct"]


async def test_scorer_and_report_versions_bind_runs_and_cannot_regrade_old_private_reports(
    tmp_path,
):
    case = synthetic_case()
    service = SyntheticService((case,))
    path = tmp_path / "owner-only" / "report.json"
    report = await evaluate_report(service, (case,), run_id="synthetic-run", report_path=path)
    assert report["configuration"]["scorer_version"] == SCORER_VERSION
    assert report["report_version"] == REPORT_VERSION
    old = json.loads(path.read_text())
    old["report_version"] = "meal-evaluation-v3"
    old["configuration"].pop("scorer_version")
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    with pytest.raises(AiUnavailable, match="changed cases or settings"):
        await evaluate_report(service, (case,), run_id="synthetic-run", report_path=path)
    assert path.read_bytes() == original and service.called == [case.id]


async def test_scorer_v2_report_cannot_resume_or_be_overridden_after_semantic_version_change(
    tmp_path,
):
    case = synthetic_case()
    service = SyntheticService((case,))
    path = tmp_path / "owner-only" / "report.json"
    report = await evaluate_report(
        service,
        (case,),
        run_id="synthetic-run",
        report_path=path,
        configuration={"scorer_version": "meal-rubric-v2"},
    )
    assert report["configuration"]["scorer_version"] == "meal-rubric-v3"
    old = json.loads(path.read_text())
    old["configuration"]["scorer_version"] = "meal-rubric-v2"
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    with pytest.raises(AiUnavailable, match="changed cases or settings"):
        await evaluate_report(service, (case,), run_id="synthetic-run", report_path=path)
    assert path.read_bytes() == original and service.called == [case.id]


def test_duplicate_model_mass_cannot_reuse_one_reported_portion():
    case = synthetic_case()
    proposal = expected_outcome(case).proposal
    duplicate = proposal.model_copy(update={"items": (proposal.items[0], proposal.items[0])})
    assert not _model_score(case, duplicate)["no_unsupported_mass"]


def test_exact_raw_identity_evidence_is_still_bounded_to_120_characters():
    text = "Cooked rice " + "edible " * 17 + "weighed 150g."
    case = synthetic_case(text=text)
    proposal = expected_outcome(case).proposal
    long = proposal.items[0].model_copy(update={"identity_evidence": text})
    assert text in case.text and len(text) > 120
    assert not _model_score(case, proposal.model_copy(update={"items": (long,)}))[
        "identity_evidence_valid"
    ]


@pytest.mark.parametrize(
    ("text", "grams"),
    [
        ("Cooked rice 0.15kg", "150"),
        ("Cooked rice 150000mg", "150"),
        ("Cooked rice 80,25 grams", "80.25"),
    ],
)
def test_raw_quantity_evidence_converts_declared_units_and_decimal_comma_exactly(text, grams):
    case = synthetic_case(
        text=text,
        expected_items=(
            AiMealItem(
                food_version_id=1,
                grams=grams,
                basis="Synthetic",
                quantity_source="reported",
                uncertainty="none",
            ),
        ),
    )
    assert _model_score(case, expected_outcome(case).proposal)["quantity_evidence_valid"]
