"""
Slides Generator Router — AI-powered slide generation for courses

Endpoints:
- POST /api/v1/slides/generate → Generate slides structure from topic
"""

import json
import os
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException
from openai import OpenAI
from pydantic import BaseModel

from auth import get_current_user
from models import User

router = APIRouter(prefix="/slides", tags=["slides"])

# ── Schemas ──────────────────────────────────────────────────────────


class SlideContent(BaseModel):
    title: str
    content: List[str]


class GenerateSlidesResponse(BaseModel):
    slides: List[SlideContent]


# ── Helpers ──────────────────────────────────────────────────────────


def _get_openai_client():
    """Get OpenAI client instance."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "OpenAI API key not configured")
    return OpenAI(api_key=api_key)


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/generate", response_model=GenerateSlidesResponse)
async def generate_slides(
    topic: str = Body(..., embed=True),
    num_slides: int = Body(5, embed=True, ge=1, le=20),
    current_user: User = Depends(get_current_user),
):
    """
    Generate slide structure using AI.

    **Parameters:**
    - `topic`: Course topic (e.g., "Introducción a Python")
    - `num_slides`: Number of slides to generate (1-20, default: 5)

    **Returns:** List of slides with title and content bullets
    """
    try:
        client = _get_openai_client()

        prompt = f"""Genera {num_slides} slides para un curso sobre: {topic}

Formato JSON:
[
  {{"title": "Título del slide", "content": ["punto 1", "punto 2", "punto 3"]}},
  ...
]

Reglas:
- Solo devuelve JSON válido, sin explicación ni markdown
- Cada slide debe tener 2-4 bullets en content
- Títulos concisos y claros
- Content en español, práctico y educativo
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Más rápido y barato para esta tarea
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )

        content = response.choices[0].message.content.strip()

        # Parse JSON response
        try:
            slides_data = json.loads(content)
        except json.JSONDecodeError:
            # Si el modelo devuelve markdown, intentar extraer el JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
                slides_data = json.loads(content)
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
                slides_data = json.loads(content)
            else:
                raise

        # Validate structure
        slides = [SlideContent(**slide) for slide in slides_data]

        return GenerateSlidesResponse(slides=slides)

    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Failed to parse AI response as JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Failed to generate slides: {str(e)}")
