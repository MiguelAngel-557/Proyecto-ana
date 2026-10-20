"""
ana.models — Estructuras de datos del núcleo.

Sin pydantic a propósito: estos modelos los usan el cargador, el validador y
el CLI, que deben poder correr en CI sin levantar la API. La capa FastAPI
(ana/api.py) define sus propios esquemas pydantic para entrada/salida.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from .expr import extraer_variables


class Estado(str, Enum):
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    REVISION_HUMANA = "REVISION_HUMANA"
    INFORMACION_INSUFICIENTE = "INFORMACION_INSUFICIENTE"
    CONFLICTO = "CONFLICTO"
    SIN_AXIOMAS = "SIN_AXIOMAS"


# Estados que un axioma puede producir como consecuencia
ESTADOS_CONSECUENCIA = {
    Estado.APROBADO.value,
    Estado.RECHAZADO.value,
    Estado.REVISION_HUMANA.value,
}
class EstadoRevision(str, Enum):
    """Ciclo de revisión humana de un axioma (aparte de `activo`)."""
    PENDIENTE = "PENDIENTE_REVISION"
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"

# Orden de severidad: gana el más severo cuando varios axiomas se activan
SEVERIDAD = {
    Estado.APROBADO.value: 0,
    Estado.REVISION_HUMANA.value: 1,
    Estado.RECHAZADO.value: 2,
}


@dataclass
class Axioma:
    axioma_id: str
    dominio: str
    condicion_expr: str
    consecuencia: Dict[str, Any]
    descripcion: str = ""
    prioridad: int = 50
    variables_requeridas: List[str] = field(default_factory=list)
    fuente: str = "MERA"
    version: str = "1.0"
    vigencia_desde: Optional[str] = None
    vigencia_hasta: Optional[str] = None
    etiquetas: List[str] = field(default_factory=list)
    activo: bool = True
    # países donde aplica; ["*"] = aplica a todos (valor por defecto)
    paises: List[str] = field(default_factory=lambda: ["*"])
        # revisión humana y trazabilidad de la fuente. El valor por defecto es
    # APROBADO para que los axiomas existentes (YAML/manuales) no cambien.
    estado_revision: str = EstadoRevision.APROBADO.value
    origen: str = "archivo"    # archivo | manual | ia_generado | bulk_import
    fuente_url: str = ""
    fuente_documento: str = ""
    fuente_referencia: str = ""
    fuente_fragmento: str = ""
    generado_por: str = ""
    revisado_por: str = ""
    revisado_en: Optional[str] = None
    motivo_revision: str = ""
    # metadatos de trazabilidad, los llena el cargador
    archivo_origen: str = ""
    hash: str = ""

    # ---------------------------------------------------------------- utilidades

    def normalizar(self) -> "Axioma":
        """
        Completa lo que se puede deducir solo. En particular
        `variables_requeridas`, que ya no hace falta escribir a mano.
        """
        deducidas = extraer_variables(self.condicion_expr)
        if not self.variables_requeridas:
            self.variables_requeridas = deducidas
        else:
            # unión: respeta lo declarado (puede haber variables que el autor
            # exige aunque la expresión no las toque) y agrega las deducidas
            self.variables_requeridas = sorted(
                set(self.variables_requeridas) | set(deducidas)
            )
        self.hash = self.calcular_hash()
        return self

    def calcular_hash(self) -> str:
        """Hash del contenido semántico: permite sincronización incremental."""
        payload = json.dumps(
            {
                "id": self.axioma_id,
                "dominio": self.dominio,
                "expr": self.condicion_expr,
                "cons": self.consecuencia,
                "prio": self.prioridad,
                "vars": sorted(self.variables_requeridas),
                "ver": self.version,
                "desde": self.vigencia_desde,
                "hasta": self.vigencia_hasta,
                "activo": self.activo,
                "paises": sorted(self.paises),
            },
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def vigente(self, momento: Optional[date] = None) -> bool:
        hoy = momento or datetime.now().date()
        if not self.activo or self.estado_revision != EstadoRevision.APROBADO.value:
            return False
        if self.vigencia_desde and hoy < date.fromisoformat(self.vigencia_desde):
            return False
        if self.vigencia_hasta and hoy > date.fromisoformat(self.vigencia_hasta):
            return False
        return True

    @property
    def estado_consecuencia(self) -> str:
        return self.consecuencia.get("estado", Estado.APROBADO.value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Axioma":
        conocidos = {f for f in cls.__dataclass_fields__}
        limpio = {k: v for k, v in d.items() if k in conocidos}
        faltantes = {"axioma_id", "dominio", "condicion_expr", "consecuencia"} - set(limpio)
        if faltantes:
            raise ValueError(f"Faltan campos obligatorios: {sorted(faltantes)}")
        return cls(**limpio)


@dataclass
class Hallazgo:
    """Resultado de una comprobación del validador."""
    nivel: str          # ERROR | ADVERTENCIA | INFO
    codigo: str
    mensaje: str
    axioma_id: str = ""
    archivo: str = ""

    def __str__(self) -> str:
        ubic = f" [{self.axioma_id}]" if self.axioma_id else ""
        arch = f" ({self.archivo})" if self.archivo else ""
        return f"{self.nivel:11} {self.codigo:22}{ubic}{arch} {self.mensaje}"


@dataclass
class Traza:
    """Una línea del decision trace."""
    axioma_id: str
    resultado: str      # ACTIVADO | NO_ACTIVADO | NO_EVALUABLE | ERROR | OMITIDO_VIGENCIA
    prioridad: int = 0
    detalle: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Resultado:
    estado: str
    decision: Optional[Dict[str, Any]] = None
    axiomas_evaluados: List[str] = field(default_factory=list)
    axiomas_activados: List[str] = field(default_factory=list)
    variables_faltantes: List[str] = field(default_factory=list)
    conflictos: List[Dict[str, Any]] = field(default_factory=list)
    explicacion: str = ""
    trace: List[Traza] = field(default_factory=list)
    version_base_conocimiento: str = ""
    ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d
