"""E-Bike Doctor AI API starter. Deploy behind HTTPS; never ship API keys in the Android APK."""
import os
from typing import Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from openai import OpenAI

app = FastAPI(title="E-Bike Doctor AI API", version="0.1.0")
API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
client = OpenAI(api_key=API_KEY) if API_KEY else None

class DiagnosisRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    brand: str = Field(default="", max_length=120)
    motor_model: str = Field(default="", max_length=160)
    display_model: str = Field(default="", max_length=160)
    controller_model: str = Field(default="", max_length=160)
    battery_voltage_class: str = Field(default="", max_length=60)
    error_code: str = Field(default="", max_length=80)
    symptom: str = Field(min_length=3, max_length=3000)
    measurements: str = Field(default="", max_length=3000)
    skill_level: Literal["beginner", "technician"] = "beginner"

@app.get("/health")
def health():
    return {"status": "ok", "ai_configured": bool(client)}

@app.post("/v1/diagnose")
def diagnose(req: DiagnosisRequest):
    if client is None:
        raise HTTPException(status_code=503, detail="AI service is not configured on the server")
    lang = {"hu": "Hungarian", "en": "English", "de": "German"}[req.language]
    prompt = f"""You are the E-Bike Doctor diagnostic assistant for electric bicycles and light electric vehicles. Respond in {lang}.
Use careful technician-grade reasoning. Separate confirmed facts, likely causes, and unknowns. Never invent a manufacturer fault-code definition, pinout, wire colour, or voltage specification. If exact model documentation is unavailable, explicitly say so and give only safe generic checks. Ask for missing information when needed. Provide a numbered next-step sequence; for each measurement specify meter mode, safe measurement points, expected range only when well-supported, and interpretation. Warn before live electrical tests. Never recommend bypassing a BMS, defeating brake cutoffs, shorting battery cells, or applying pack voltage to signal/data pins. For swollen, hot, damaged, hissing or smoking batteries, tell the user to stop, isolate safely, and seek qualified service. Do not claim to control or read the bike electronically; this is text-based advice only. Avoid requesting names, addresses, phone numbers or other customer personal data.

System facts:
Brand: {req.brand}
Motor: {req.motor_model}
Display: {req.display_model}
Controller: {req.controller_model}
Battery voltage class: {req.battery_voltage_class}
Error code: {req.error_code}
Symptom: {req.symptom}
Measurements: {req.measurements}
User skill: {req.skill_level}

Return: (1) brief assessment and confidence, (2) safety warning if relevant, (3) next best test, (4) possible interpretations, (5) information needed to confirm. Do not imply a diagnosis is certain without evidence."""
    try:
        response = client.responses.create(model=MODEL, input=prompt)
        return {"answer": response.output_text, "model": MODEL, "disclaimer": "AI advice can be wrong. Confirm values with the exact manufacturer's service documentation."}
    except Exception as exc:
        # Do not return provider details or secrets to the client.
        raise HTTPException(status_code=502, detail="AI provider request failed; retry later") from exc
