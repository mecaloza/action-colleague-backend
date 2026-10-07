"""
What each kind of activity holds (answers included) and the rules it must follow to be played.

`parse_spec(type, data)` validates an activity's `spec`: a valid activity can be published.
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
Number = Annotated[float, Field(ge=-1e9, le=1e9, allow_inf_nan=False)]

MAX_DECISION_DEPTH = 4
MAX_SIMULATOR_CHECKS = 5000  # slider combinations a simulator's formula is checked on when saved


class SpecError(ValueError):
    """The activity can't be saved as it is: the message says why, in Spanish."""


# ── Decisión con cliente ──────────────────────────────────────────────


class Choice(BaseModel):
    id: Key
    text: Short
    points: int = Field(ge=0, le=10)
    feedback: Spoken  # the consequence, spoken
    next: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{0,24}$")] = ""  # "" ends the conversation

    @field_validator("id")
    @classmethod
    def _not_a_line_name(cls, value: str) -> str:
        if value == "situation":  # "<node>.situation" is the narration key of the situation itself
            raise ValueError("Una opción no puede llamarse «situation».")
        return value


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


# ── Inspección (un juego: revisar cada elemento y decidir qué hacer) ──

# How a tire is drawn: its wear pattern or damage ("none": not a tire, or nothing to see).
Look = Literal["none", "even", "center", "shoulders", "one_side", "cupping", "cuts", "bulge"]
MAX_TRUCK_TIRES = 10  # 5 axles


class Fact(BaseModel):
    label: Label
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class Verdict(BaseModel):
    id: Key
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class Item(BaseModel):
    id: Key
    label: Label
    facts: list[Fact] = Field(min_length=1, max_length=5)
    look: Look = "none"
    answer: Key  # the verdict that is right for it
    explanation: Spoken


class InspectionSpec(BaseModel):
    """Check each item (a tire on a truck, or a case on a card), read its facts and decide what to do with it.
    The learner never receives the answers: each decision is graded on the server."""

    prompt: Short
    scene: Literal["truck", "cards"] = "cards"
    verdicts: list[Verdict] = Field(min_length=2, max_length=4)
    items: list[Item] = Field(min_length=3, max_length=MAX_TRUCK_TIRES)
    lives: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _answers_are_verdicts(self) -> "InspectionSpec":
        verdicts = [verdict.id for verdict in self.verdicts]
        if len(set(verdicts)) != len(verdicts):
            raise ValueError("Dos decisiones tienen el mismo identificador.")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Dos elementos tienen el mismo identificador.")
        for item in self.items:
            if item.answer not in verdicts:
                raise ValueError(f"La respuesta de «{item.label}» no es una de las decisiones.")
        if len({item.answer for item in self.items}) < 2:
            raise ValueError("Usa al menos 2 decisiones distintas como respuesta: si todas son iguales no hay reto.")
        if self.scene == "truck" and len(self.items) % 2:
            raise ValueError("En el camión las llantas van por pares (izquierda y derecha de cada eje).")
        if self.scene == "truck" and any(item.look == "none" for item in self.items):
            raise ValueError("En el camión cada llanta necesita cómo se ve (su desgaste o daño).")
        if any(len({fact.label for fact in item.facts}) != len(item.facts) for item in self.items):
            raise ValueError("Un elemento tiene dos datos con el mismo nombre.")
        return self


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
        # The slider only stops on min + k*step: both ends and the initial value must be among those stops.
        if not _on_step(self.max - self.min, self.step) or not _on_step(self.default - self.min, self.step):
            raise ValueError(f"En «{self.label}», el máximo y el valor inicial deben caer en un paso del control.")
        return self

    @property
    def stops(self) -> int:
        """How many values the slider has."""
        return round((self.max - self.min) / self.step) + 1

    def sample(self, count: int) -> list[float]:
        """`count` of the slider's values, evenly spread and ends included (all of them if it has fewer)."""
        last = self.stops - 1
        positions = range(self.stops) if self.stops <= count else sorted({round(last * i / (count - 1)) for i in range(count)})
        return [self.min + position * self.step for position in positions]


def _on_step(distance: float, step: float) -> bool:
    steps = distance / step
    return abs(steps - round(steps)) < 1e-6


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
        # The values the sliders can actually take (all of them, or an even sample of up to ~5000 combinations):
        # the formula must compute on every one, and the target be met on at least one.
        per_variable = max(2, int(MAX_SIMULATOR_CHECKS ** (1 / len(self.variables))))
        names = keys
        try:
            tree = formula.parse(self.formula, keys)
            results = [formula.evaluate(tree, dict(zip(names, combo)))
                       for combo in product(*(variable.sample(per_variable) for variable in self.variables))]
            start = formula.evaluate(tree, {variable.key: variable.default for variable in self.variables})
        except formula.FormulaError as exc:
            raise ValueError(str(exc)) from exc
        if not any(self.target.met(result) for result in results):
            raise ValueError("Con ningún valor de los controles se alcanza el objetivo.")
        if self.target.met(start):
            raise ValueError("Con los valores iniciales el objetivo ya se cumple: no hay reto.")
        return self

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
