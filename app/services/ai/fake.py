"""
Offline stand-in for the LLM (tests, local development and e2e without API keys).

It produces schema-valid, deterministic content instantly, so the whole studio flow — outline,
storyboard, quiz — runs end to end with no network.
"""

import re

from app.services.ai import designer


def _topic(text: str) -> str:
    words = re.findall(r"\w+", text)
    return " ".join(words[:5]).capitalize() or "Tema del curso"


class FakeLLM:
    def structured(self, system, user, schema, max_tokens=8000):
        if schema is designer.CourseOutline:
            return self._outline(user)
        if schema is designer.ModuleDraft:
            return self._module(user)
        if schema is designer.QuizDraft:
            return designer.QuizDraft(quiz=self._questions())
        raise NotImplementedError(schema.__name__)

    def _outline(self, prompt: str) -> designer.CourseOutline:
        brief = prompt.split("BRIEF DEL ADMINISTRADOR:", 1)[-1].split("AUDIENCIA:", 1)[0].strip()
        topic = _topic(brief)
        count_match = re.search(r"Exactamente (\d+) módulos", prompt)
        count = int(count_match.group(1)) if count_match else 3
        modules = [
            designer.OutlineModule(
                title=f"{topic}: parte {i}",
                summary=f"Qué necesitas saber sobre {topic.lower()} (parte {i}).",
                objectives=[f"Explicar la idea {i}", f"Aplicar la idea {i} en el trabajo"],
                key_points=[f"Punto clave {i}.{n}" for n in range(1, 4)],
                estimated_minutes=3,
                include_quiz=i != 1,
            )
            for i in range(1, count + 1)
        ]
        return designer.CourseOutline(
            title=f"Curso de {topic.lower()}",
            description=f"Un curso práctico sobre {topic.lower()}.",
            audience="Colaboradores de la empresa",
            objectives=["Comprender el tema", "Aplicarlo en el día a día", "Evitar errores comunes"],
            modules=modules,
        )

    def _scene(self, layout: str, title: str, narration: str, **fields) -> designer.SceneDraft:
        base = {"subtitle": "", "points": [], "stat_value": "", "stat_label": "", "quote_author": "",
                "left_heading": "", "left_points": [], "right_heading": "", "right_points": []}
        return designer.SceneDraft(layout=layout, title=title, narration=narration, **{**base, **fields})

    def _module(self, prompt: str) -> designer.ModuleDraft:
        title_match = re.search(r"MÓDULO \d+: (.+)", prompt)
        title = title_match.group(1).strip() if title_match else "Módulo"
        include_quiz = "lista vacía" not in prompt
        scenes = [
            self._scene("cover", title, f"En este módulo vas a aprender {title.lower()}.",
                        subtitle="Lo esencial en pocos minutos"),
            self._scene("bullets", "Lo más importante", "Empecemos por las ideas clave.",
                        points=["Primera idea clave", "Segunda idea clave", "Tercera idea clave"]),
            self._scene("comparison", "Así sí, así no", "Veamos la diferencia.",
                        left_heading="Correcto", left_points=["Hacerlo con cuidado"],
                        right_heading="Incorrecto", right_points=["Hacerlo con prisa"]),
            self._scene("closing", "Para recordar", "Repasemos lo aprendido.", points=["Idea uno", "Idea dos", "Idea tres"]),
        ]
        return designer.ModuleDraft(
            scenes=scenes,
            reading_summary=f"### {title}\n\n- Primera idea clave\n- Segunda idea clave\n- Tercera idea clave",
            quiz=self._questions() if include_quiz else [],
        )

    def _questions(self) -> list[designer.QuestionDraft]:
        empty = {"scenario": "", "options": [], "correct_index": 0, "correct_bool": False, "items": [], "pairs": [],
                 "answers": [], "hint": ""}
        return [
            designer.QuestionDraft(**{**empty, "type": "single_choice", "prompt": "¿Cuál es la primera idea clave?",
                                      "options": ["La primera", "Ninguna", "La última"], "correct_index": 0,
                                      "explanation": "Es la primera."}),
            designer.QuestionDraft(**{**empty, "type": "true_false", "prompt": "Hay que hacerlo con prisa.",
                                      "correct_bool": False, "explanation": "Se hace con cuidado."}),
            designer.QuestionDraft(**{**empty, "type": "ordering", "prompt": "Ordena los pasos",
                                      "items": ["Preparar", "Hacer", "Revisar"], "explanation": "Ese es el orden."}),
            designer.QuestionDraft(**{**empty, "type": "fill_blank", "prompt": "Hay que trabajar con _____.",
                                      "answers": ["cuidado"], "hint": "c...", "explanation": "Con cuidado."}),
        ]
