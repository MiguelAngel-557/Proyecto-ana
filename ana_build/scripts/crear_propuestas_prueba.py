"""
Fase 2: crea 3 propuestas de prueba PENDIENTE_REVISION (no las ve el motor).

Ejecutar desde la carpeta ana_build/:
    python scripts/crear_propuestas_prueba.py
"""
from ana.cli import _cargar_env

_cargar_env()  # lee .env (ANA_REPO, DATABASE_URL)

from ana.models import Axioma  # noqa: E402
from ana.repository import obtener_repositorio  # noqa: E402


def propuesta(axioma_id: str, descripcion: str) -> Axioma:
    return Axioma(
        axioma_id=axioma_id,
        dominio="aduanas",
        paises=["MX"],
        descripcion=descripcion,
        condicion_expr="aduanas['valor_declarado'] > 100000",
        consecuencia={
            "estado": "REVISION_HUMANA",
            "motivo": "Valor declarado alto: requiere revisión (propuesta de prueba).",
        },
        prioridad=75,
        origen="ia_generado",
        generado_por="prueba_manual_fase2",
        fuente_documento="Documento de prueba",
        fuente_referencia="Regla de prueba 1",
        fuente_fragmento="Texto de ejemplo para probar el flujo de revisión.",
    ).normalizar()


PRUEBAS = [
    ("PROP-MX-TEST-001", "Prueba: esta se APRUEBA"),
    ("PROP-MX-TEST-002", "Prueba: esta se RECHAZA"),
    ("PROP-MX-TEST-003", "Prueba: defensa en profundidad (activo=true a la fuerza)"),
]

repo = obtener_repositorio()
for id_, desc in PRUEBAS:
    try:
        repo.crear_propuesta(propuesta(id_, desc))
        print(f"creada: {id_}")
    except ValueError as e:
        print(f"omitida: {e}")
repo.cerrar()