"""What each kind of activity must hold to be saved, and the formula language of the simulators."""

import copy
import json
from pathlib import Path

import pytest

from app.services import formula
from app.services.activity_spec import SpecError, parse_spec, ready_problems

CASES = json.loads((Path(__file__).parent / "fixtures" / "formula_cases.json").read_text())

DECISION = {
    "nodes": [
        {"id": "inicio", "speaker": "Cliente", "situation": "Solo quiero la llanta más barata para mi flota.",
         "choices": [
             {"id": "precio", "text": "Le doy la más barata.", "points": 2,
              "feedback": "Vendes hoy, pero el costo por kilómetro sube.", "next": ""},
             {"id": "pregunta", "text": "¿Qué ruta y carga manejan?", "points": 6,
              "feedback": "Entiendes la aplicación antes de cotizar.", "next": "ruta"},
         ]},
        {"id": "ruta", "speaker": "Cliente", "situation": "Carretera, 40 toneladas, 12 mil km al mes.",
         "choices": [
             {"id": "cpk", "text": "Le muestro el costo por kilómetro.", "points": 4,
              "feedback": "El cliente ve el ahorro en números.", "next": ""},
             {"id": "marca", "text": "Le hablo de la marca.", "points": 1,
              "feedback": "Sin números, el precio vuelve a mandar.", "next": ""},
         ]},
    ]
}
INSPECTION = {
    "prompt": "Encuentra los dos problemas de desgaste.",
    "zones": [
        {"id": "hombros", "label": "Hombros gastados", "x": 0.05, "y": 0.1, "w": 0.2, "h": 0.3, "is_problem": True,
         "explanation": "Desgaste en ambos hombros: baja presión o sobrecarga.", "placed": True},
        {"id": "centro", "label": "Centro gastado", "x": 0.4, "y": 0.1, "w": 0.2, "h": 0.3, "is_problem": True,
         "explanation": "Desgaste en el centro: exceso de presión.", "placed": True},
        {"id": "parejo", "label": "Desgaste parejo", "x": 0.7, "y": 0.1, "w": 0.2, "h": 0.3, "is_problem": False,
         "explanation": "Esta llanta está bien: desgaste uniforme.", "placed": True},
    ],
}
SIMULATOR = {
    "prompt": "Baja el costo por kilómetro a menos de $0.050.",
    "variables": [
        {"key": "precio", "label": "Precio de la llanta", "unit": "MXN", "min": 4000, "max": 12000, "step": 500,
         "default": 6000},
        {"key": "km", "label": "Kilómetros de vida", "unit": "km", "min": 60000, "max": 250000, "step": 10000,
         "default": 100000},
    ],
    "formula": "precio / km",
    "output": {"label": "Costo por kilómetro", "unit": "MXN/km", "decimals": 3},
    "target": {"op": "lte", "value": 0.05},
    "success_text": "Así se vende: más vida útil, menos costo por kilómetro.",
    "fail_text": "Todavía no: prueba con más kilómetros de vida.",
    "hint": "Una llanta renovable rinde más kilómetros.",
}
QUICK_MATCH = {
    "mode": "match", "prompt": "Une cada desgaste con su causa.", "seconds": 45,
    "pairs": [{"left": "Ambos hombros", "right": "Baja presión"}, {"left": "Centro", "right": "Exceso de presión"},
              {"left": "Un hombro", "right": "Alineación"}, {"left": "Copas", "right": "Balanceo"}],
}
QUICK_ORDER = {
    "mode": "order", "prompt": "Ordena la revisión en patio.", "seconds": 40,
    "items": ["Medir presión en frío", "Revisar desgaste", "Leer el DOT", "Registrar y recomendar"],
}
VALID = {"decision": DECISION, "inspection": INSPECTION, "simulator": SIMULATOR, "quick": QUICK_MATCH}


@pytest.mark.parametrize("kind", list(VALID))
def test_each_kind_accepts_a_well_formed_activity(kind):
    assert parse_spec(kind, copy.deepcopy(VALID[kind]))
    assert parse_spec("quick", copy.deepcopy(QUICK_ORDER)).size == 4


def test_a_conversation_scores_its_best_path():
    assert parse_spec("decision", copy.deepcopy(DECISION)).max_score() == 10  # 6 + 4


@pytest.mark.parametrize("change, message", [
    (lambda s: s["nodes"][1]["choices"][0].update(next="nada"), "no existe"),
    (lambda s: s["nodes"][1]["choices"][0].update(next="inicio"), "vuelve a una situación anterior"),
    (lambda s: s["nodes"][0]["choices"][1].update(next=""), "ninguna opción lleva"),
    (lambda s: s["nodes"][1].update(id="inicio"), "mismo identificador"),
    (lambda s: s["nodes"][0]["choices"].pop(), "choices"),
    (lambda s: s["nodes"][0]["choices"][0].update(points=50), "points"),
])
def test_a_conversation_must_move_forward_and_end(change, message):
    spec = copy.deepcopy(DECISION)
    change(spec)
    with pytest.raises(SpecError, match=message):
        parse_spec("decision", spec)


def test_a_conversation_is_at_most_four_steps_deep():
    nodes = [{"id": f"n{i}", "situation": "Situación", "choices": [
        {"id": "a", "text": "Seguir", "points": 1, "feedback": "Bien.", "next": f"n{i + 1}" if i < 4 else ""},
        {"id": "b", "text": "Cerrar", "points": 0, "feedback": "Fin.", "next": ""}]} for i in range(5)]
    with pytest.raises(SpecError, match="más de 4 pasos"):
        parse_spec("decision", {"nodes": nodes})


@pytest.mark.parametrize("change, message", [
    (lambda s: s["zones"][2].update(is_problem=True), "al menos 1 zona sin problema"),
    (lambda s: s["zones"][1].update(is_problem=False), "al menos 2 zonas con un problema"),
    (lambda s: s["zones"][0].update(x=0.9), "se sale de la imagen"),
    (lambda s: s["zones"][0].update(w=0.01), "w"),
    (lambda s: s["zones"].pop(), "zones"),
])
def test_an_inspection_has_problems_and_distractors_inside_the_picture(change, message):
    spec = copy.deepcopy(INSPECTION)
    change(spec)
    with pytest.raises(SpecError, match=message):
        parse_spec("inspection", spec)


def test_an_inspection_is_published_only_with_its_picture_and_every_zone_placed():
    spec = copy.deepcopy(INSPECTION)
    spec["zones"][0]["placed"] = False
    parsed = parse_spec("inspection", spec)
    assert ready_problems("inspection", parsed, has_image=False) == [
        "Falta la imagen sobre la que se inspecciona.", "Ubica cada zona sobre la imagen y confírmala."]
    assert ready_problems("inspection", parse_spec("inspection", copy.deepcopy(INSPECTION)), has_image=True) == []
    assert ready_problems("decision", parse_spec("decision", copy.deepcopy(DECISION)), has_image=False) == []


@pytest.mark.parametrize("change, message", [
    (lambda s: s.update(formula="precio ** 2"), "no se entiende"),
    (lambda s: s.update(formula="precio / (km - km)"), "divide entre cero"),
    (lambda s: s.update(formula="precio / costo"), "no es una de las variables"),
    (lambda s: s["target"].update(value=0.001), "ningún valor"),
    (lambda s: s["target"].update(value=0.07), "ya se cumple"),
    (lambda s: s["variables"][0].update(default=20000), "valor inicial"),
    (lambda s: s["variables"][0].update(step=0.001), "paso es demasiado pequeño"),
    (lambda s: s["variables"][1].update(key="precio"), "misma clave"),
])
def test_a_simulator_must_compute_and_have_a_reachable_challenge(change, message):
    spec = copy.deepcopy(SIMULATOR)
    change(spec)
    with pytest.raises(SpecError, match=message):
        parse_spec("simulator", spec)


def test_a_simulator_keeps_the_controls_on_their_range_and_steps():
    spec = parse_spec("simulator", copy.deepcopy(SIMULATOR))
    assert spec.clamp({"precio": 99999, "km": 123456}) == {"precio": 12000, "km": 120000}
    assert spec.result({"precio": 6000, "km": 120000}) == pytest.approx(0.05)


@pytest.mark.parametrize("spec, message", [
    ({**QUICK_ORDER, "items": QUICK_ORDER["items"][:3]}, "entre 4 y 8 pasos"),
    ({**QUICK_ORDER, "items": [*QUICK_ORDER["items"][:3], "medir presión en frío"]}, "dos pasos iguales"),
    ({**QUICK_MATCH, "pairs": QUICK_MATCH["pairs"][:3]}, "entre 4 y 8 parejas"),
    ({**QUICK_MATCH, "seconds": 5}, "seconds"),
])
def test_a_quick_challenge_has_enough_distinct_items(spec, message):
    with pytest.raises(SpecError, match=message):
        parse_spec("quick", copy.deepcopy(spec))


def test_an_unknown_kind_is_refused():
    with pytest.raises(SpecError, match="desconocido"):
        parse_spec("ruleta", {})


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda case: case["formula"])
def test_formulas_compute_like_arithmetic(case):
    tree = formula.parse(case["formula"], case["variables"])
    assert formula.evaluate(tree, case["variables"]) == pytest.approx(case["result"])


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda case: case["formula"] or "vacía")
def test_anything_but_arithmetic_on_the_controls_is_refused(case):
    with pytest.raises(formula.FormulaError):
        formula.parse(case["formula"], case["variables"])


def test_a_division_by_zero_is_reported_not_raised_as_a_crash():
    case = CASES["division_by_zero"]
    with pytest.raises(formula.FormulaError, match="divide entre cero"):
        formula.evaluate(formula.parse(case["formula"], case["variables"]), case["variables"])


def test_a_formula_can_not_be_endless():
    with pytest.raises(formula.FormulaError, match="demasiado larga"):
        formula.parse("precio + " * 30 + "precio", ["precio"])
    with pytest.raises(formula.FormulaError, match="anidada"):
        formula.parse("(" * 40 + "precio" + ")" * 40, ["precio"])
