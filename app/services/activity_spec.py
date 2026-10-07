"""
What each kind of activity holds (answers included) and the rules it must follow to be played.

`parse_spec(type, data)` validates an activity's `spec`; `ready_problems(type, spec)` lists what still keeps a valid
activity from being published (e.g. inspection zones nobody has placed on the picture yet).
"""

from itertools import product
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator, model_validator

from app.services import formula

Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,24}$")]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Spoken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
OptionalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)]
Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Number = Annotated[float, Field(ge=-1e9, le=1e9, allow_inf_nan=False)]

MAX_DECISION_DEPTH = 4
MIN_ZONE_SIZE = 0.03


class SpecError(ValueError):
    """The activity can't be saved as it is: the message says why, in Spanish."""


# ── Decisión con cliente ──────────────────────────────────────────────


class Choice(BaseModel):
    id: Key
    text: Short
    points: int = Field(ge=0, le=10)
    feedback: Spoken  # the consequence, spoken
    next: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{0,24}$")] = ""  # "" ends the conversation


class DecisionNode(BaseModel):
    id: Key
    speaker: Label = "Cliente"
    situation: Spoken
    choices: list[Choice] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def _distinct_choices(self) -> "DecisionNode":
        if len({choice.id for choice in self.choices}) != len(self.choices):
            raise ValueError("Cada opción de una situación necesita su propio identificador.")
        return self


class DecisionSpec(BaseModel):
    """A conversation with a client: the first node opens it; each choice scores and leads on (or ends it)."""

    nodes: list[DecisionNode] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _is_a_tree_that_ends(self) -> "DecisionSpec":
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("Dos situaciones tienen el mismo identificador.")
        by_id = {node.id: node for node in self.nodes}
        for node in self.nodes:
            for choice in node.choices:
                if choice.next and choice.next not in by_id:
                    raise ValueError(f"Una opción lleva a «{choice.next}», que no existe.")
        reached: set[str] = set()

        def walk(node_id: str, depth: int, path: tuple[str, ...]) -> None:
            if node_id in path:
                raise ValueError("La conversación vuelve a una situación anterior: debe avanzar siempre.")
            if depth > MAX_DECISION_DEPTH:
                raise ValueError(f"La conversación tiene más de {MAX_DECISION_DEPTH} pasos.")
            reached.add(node_id)
            for choice in by_id[node_id].choices:
                if choice.next:
                    walk(choice.next, depth + 1, (*path, node_id))

        walk(self.nodes[0].id, 1, ())
        if reached != set(ids):
            raise ValueError("Hay situaciones a las que ninguna opción lleva.")
        return self

    def max_score(self) -> int:
        """The best path's points."""
        by_id = {node.id: node for node in self.nodes}

        def best(node_id: str) -> int:
            return max(choice.points + (best(choice.next) if choice.next else 0) for choice in by_id[node_id].choices)

        return best(self.nodes[0].id)


# ── Inspección visual ─────────────────────────────────────────────────


class Zone(BaseModel):
    id: Key
    label: Label
    x: Fraction
    y: Fraction
    w: Annotated[float, Field(ge=MIN_ZONE_SIZE, le=1, allow_inf_nan=False)]
    h: Annotated[float, Field(ge=MIN_ZONE_SIZE, le=1, allow_inf_nan=False)]
    is_problem: bool
    explanation: Spoken
    placed: bool = False  # an admin confirmed where it is on the picture (the AI only guesses a region)

    @model_validator(mode="after")
    def _inside_the_picture(self) -> "Zone":
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError(f"La zona «{self.label}» se sale de la imagen.")
        return self


class InspectionSpec(BaseModel):
    """Find the problems on a picture: taps are checked against zones the learner never receives."""

    prompt: Short
    zones: list[Zone] = Field(min_length=3, max_length=8)
    max_misses: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _problems_and_distractors(self) -> "InspectionSpec":
        if len({zone.id for zone in self.zones}) != len(self.zones):
            raise ValueError("Dos zonas tienen el mismo identificador.")
        problems = sum(zone.is_problem for zone in self.zones)
        if problems < 2:
            raise ValueError("La inspección necesita al menos 2 zonas con un problema.")
        if problems == len(self.zones):
            raise ValueError("La inspección necesita al menos 1 zona sin problema, para distinguir.")
        return self

    @property
    def problems(self) -> list[Zone]:
        return [zone for zone in self.zones if zone.is_problem]


# ── Simulador con reto ────────────────────────────────────────────────


class Variable(BaseModel):
    key: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,20}$")]
    label: Label
    unit: Unit = ""
    min: Number
    max: Number
    step: Annotated[float, Field(gt=0, le=1e9, allow_inf_nan=False)]
    default: Number

    @model_validator(mode="after")
    def _range(self) -> "Variable":
        if self.min >= self.max:
            raise ValueError(f"En «{self.label}», el mínimo debe ser menor que el máximo.")
        if not self.min <= self.default <= self.max:
            raise ValueError(f"En «{self.label}», el valor inicial debe estar entre el mínimo y el máximo.")
        if (self.max - self.min) / self.step > 1000:
            raise ValueError(f"En «{self.label}», el paso es demasiado pequeño para el rango.")
        return self


class Output(BaseModel):
    label: Label
    unit: Unit = ""
    decimals: int = Field(default=2, ge=0, le=4)


class Target(BaseModel):
    op: Literal["lte", "gte", "between"]
    value: Number
    value2: Number = 0

    @model_validator(mode="after")
    def _band(self) -> "Target":
        if self.op == "between" and self.value2 <= self.value:
            raise ValueError("El rango del objetivo debe ir de menor a mayor.")
        return self

    def met(self, result: float) -> bool:
        if self.op == "lte":
            return result <= self.value
        if self.op == "gte":
            return result >= self.value
        return self.value <= result <= self.value2


class SimulatorSpec(BaseModel):
    """Move the controls until the formula's result meets the target."""

    prompt: Short
    variables: list[Variable] = Field(min_length=1, max_length=4)
    formula: Annotated[str, StringConstraints(strip_whitespace=True, max_length=formula.MAX_FORMULA_CHARS)]
    output: Output
    target: Target
    success_text: Spoken
    fail_text: Spoken
    hint: OptionalText = ""

    @model_validator(mode="after")
    def _formula_works(self) -> "SimulatorSpec":
        keys = [variable.key for variable in self.variables]
        if len(set(keys)) != len(keys):
            raise ValueError("Dos controles del simulador tienen la misma clave.")
        try:
            tree = formula.parse(self.formula, keys)
            ranges = {variable.key: (variable.min, variable.max) for variable in self.variables}
            results = [formula.evaluate(tree, values) for values in formula.corners(ranges)]
            start = formula.evaluate(tree, {variable.key: variable.default for variable in self.variables})
        except formula.FormulaError as exc:
            raise ValueError(str(exc)) from exc
        if not any(self.target.met(result) for result in results) and not self._reachable_inside(tree):
            raise ValueError("Con ningún valor de los controles se alcanza el objetivo.")
        if self.target.met(start):
            raise ValueError("Con los valores iniciales el objetivo ya se cumple: no hay reto.")
        return self

    def _reachable_inside(self, tree: formula.Node) -> bool:
        """A coarse grid over the ranges, for targets reached between the corners (e.g. a 'between' band)."""
        grids = {v.key: [v.min + (v.max - v.min) * i / 6 for i in range(7)] for v in self.variables}
        names = list(grids)
        for combo in product(*(grids[name] for name in names)):
            try:
                if self.target.met(formula.evaluate(tree, dict(zip(names, combo)))):
                    return True
            except formula.FormulaError:
                continue
        return False

    def result(self, values: dict[str, float]) -> float:
        return formula.evaluate(formula.parse(self.formula, [v.key for v in self.variables]), values)

    def clamp(self, values: dict[str, float]) -> dict[str, float]:
        """Each control's value as the slider allows it: inside its range, on its steps."""
        clamped = {}
        for variable in self.variables:
            raw = float(values.get(variable.key, variable.default))
            steps = round((min(max(raw, variable.min), variable.max) - variable.min) / variable.step)
            clamped[variable.key] = min(variable.max, variable.min + steps * variable.step)
        return clamped


# ── Reto rápido con tiempo ────────────────────────────────────────────


class Pair(BaseModel):
    left: Label
    right: Short


class QuickSpec(BaseModel):
    """Match pairs or put steps in order before the time runs out. Order mode keeps `items` in the right order."""

    mode: Literal["match", "order"]
    prompt: Short
    items: list[Label] = Field(default_factory=list, max_length=8)
    pairs: list[Pair] = Field(default_factory=list, max_length=8)
    seconds: int = Field(default=60, ge=20, le=120)

    @model_validator(mode="after")
    def _enough_items(self) -> "QuickSpec":
        if self.mode == "order":
            if not 4 <= len(self.items) <= 8:
                raise ValueError("Para ordenar se necesitan entre 4 y 8 pasos.")
            if len({item.casefold() for item in self.items}) != len(self.items):
                raise ValueError("Hay dos pasos iguales.")
        else:
            if not 4 <= len(self.pairs) <= 8:
                raise ValueError("Para emparejar se necesitan entre 4 y 8 parejas.")
            for side in ("left", "right"):
                texts = [getattr(pair, side).casefold() for pair in self.pairs]
                if len(set(texts)) != len(texts):
                    raise ValueError("Hay dos parejas con el mismo texto.")
        return self

    @field_validator("items", "pairs", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return value or []

    @property
    def size(self) -> int:
        return len(self.items) if self.mode == "order" else len(self.pairs)


# ── All of them ───────────────────────────────────────────────────────

SPECS: dict[str, type[BaseModel]] = {
    "decision": DecisionSpec,
    "inspection": InspectionSpec,
    "simulator": SimulatorSpec,
    "quick": QuickSpec,
}
Spec = DecisionSpec | InspectionSpec | SimulatorSpec | QuickSpec


def parse_spec(activity_type: str, data: dict) -> Spec:
    """The activity's spec, validated; SpecError with the first problem, in Spanish."""
    model = SPECS.get(activity_type)
    if model is None:
        raise SpecError(f"Tipo de actividad desconocido: «{activity_type}».")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        message = str(error.get("ctx", {}).get("error") or error["msg"]).removeprefix("Value error, ")
        where = ".".join(str(part) for part in error["loc"])
        raise SpecError(message if error["type"] == "value_error" else f"{where}: {message}") from exc


def ready_problems(activity_type: str, spec: Spec, has_image: bool) -> list[str]:
    """What keeps a valid activity from being published."""
    problems = []
    if activity_type == "inspection":
        if not has_image:
            problems.append("Falta la imagen sobre la que se inspecciona.")
        if not all(zone.placed for zone in spec.zones):
            problems.append("Ubica cada zona sobre la imagen y confírmala.")
    return problems
