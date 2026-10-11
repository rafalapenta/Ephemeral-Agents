import math
import re
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

engine = create_engine("sqlite:///agency_agents.db")
queries = [
    (
        "Tech",
        "Preciso criar uma migration SQL para PostgreSQL e otimizar indices de busca.",
    ),
    (
        "Product",
        "Desenhar o fluxo de onboarding e UX para um aplicativo de realidade espacial no visionOS.",
    ),
    (
        "Growth",
        "Montar uma estrategia de Outbound B2B e precificacao de contratos corporativos.",
    ),
    (
        "Business",
        "Elaborar o modelo financeiro de FP&A e analise de DRE para o proximo trimestre.",
    ),
    (
        "Research",
        "Analisar conformidade de dados clinicos de pacientes segundo as normas da LGPD.",
    ),
]

with Session(engine) as s:
    for domain, q_text in queries:
        raw_tokens = re.findall(r"\b[a-zA-Z0-9_]{2,}\b", q_text)
        stopwords = {
            "preciso",
            "criar",
            "uma",
            "para",
            "otimizar",
            "busca",
            "desenhar",
            "fluxo",
            "aplicativo",
            "montar",
            "proximo",
            "dados",
            "segundo",
            "normas",
            "analisar",
        }
        clean_terms = [t for t in raw_tokens if t.lower() not in stopwords]
        fts_q = " OR ".join(f'"{t}"' for t in clean_terms)
        rows = s.execute(
            text(
                "SELECT agent_id, rank FROM agents_fts WHERE agents_fts MATCH :q ORDER BY rank LIMIT 3"
            ),
            {"q": fts_q},
        ).fetchall()
        print(f"[{domain}] Query: {q_text}")
        print(f"  Clean terms: {clean_terms}")
        for r in rows:
            abs_rank = abs(float(r[1]))
            score = 1.0 - math.exp(-abs_rank / 5.0)
            print(f"   MATCH: {r[0]} | score: {score:.4f} (abs_rank: {abs_rank:.4f})")
