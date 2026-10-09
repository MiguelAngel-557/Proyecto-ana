"""
ana.engine — Motor de inferencia.

Diferencias respecto al motor anterior:

  * Recarga sola. Cada evaluación comprueba la versión de la base (operación
    barata) y refresca la caché si cambió. No hay que reiniciar el servicio
    al tocar un axioma.
  * Un axioma sin datos ya no aborta toda la evaluación. Se marca NO_EVALUABLE
    y se sigue; sólo se devuelve INFORMACION_INSUFICIENTE si sin esos datos no
    se puede llegar a ninguna decisión. Antes, un axioma irrelevante al que le
    faltaba una variable tumbaba la petición completa.
  * Evalúa todos los axiomas vigentes y resuelve por severidad y prioridad, en
    vez de cortocircuitar en el primer rechazo. Así se detectan conflictos en
    tiempo de ejecución y la explicación queda completa.
  * Genera la explicación en lenguaje natural desde la traza.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .expr import EVALUADOR_GLOBAL, ExprError, variables_faltantes
from .models import SEVERIDAD, Axioma, Estado, Resultado, Traza

log = logging.getLogger("ana.engine")


class MotorANA:
    def __init__(self, repositorio, ttl_version_s: float = 2.0,
                 autorecarga: bool = True):
        self.repo = repositorio
        self.autorecarga = autorecarga
        self.ttl_version_s = ttl_version_s
        self._cache: Dict[str, List[Axioma]] = {}
        self._version: str = ""
        self._ultima_revision: float = 0.0
        self.recargar()

    # ------------------------------------------------------------------ caché

    def recargar(self) -> str:
        self._cache.clear()
        self._version = self.repo.version()
        self._ultima_revision = time.monotonic()
        log.info("Base de conocimiento cargada. Versión %s", self._version)
        return self._version

    def _comprobar_version(self) -> None:
        if not self.autorecarga:
            return
        if time.monotonic() - self._ultima_revision < self.ttl_version_s:
            return
        self._ultima_revision = time.monotonic()
        actual = self.repo.version()
        if actual != self._version:
            log.info("Cambio detectado (%s -> %s). Recargando.",
                     self._version, actual)
            self._cache.clear()
            self._version = actual

    def axiomas_de(self, dominio: str) -> List[Axioma]:
        self._comprobar_version()
        if dominio not in self._cache:
            self._cache[dominio] = self.repo.listar(dominio=dominio)
        return self._cache[dominio]

    @property
    def version(self) -> str:
        return self._version

    # -------------------------------------------------------------- inferencia

    def evaluar(self, dominio: str, datos: Dict[str, Any],
                momento: Optional[date] = None) -> Resultado:
        inicio = time.perf_counter()
        axiomas = self.axiomas_de(dominio)

        pais = datos.get("pais")
        axiomas = [ax for ax in axiomas
                   if "*" in ax.paises or (pais and pais in ax.paises)]

        res = Resultado(estado=Estado.SIN_AXIOMAS.value,
                        version_base_conocimiento=self._version)

        res = Resultado(estado=Estado.SIN_AXIOMAS.value,
                        version_base_conocimiento=self._version)

        if not axiomas:
            res.explicacion = f"No hay axiomas cargados para el dominio '{dominio}'."
            res.ms = (time.perf_counter() - inicio) * 1000
            return res

        activados: List[Axioma] = []
        faltantes_global: List[str] = []

        for ax in axiomas:
            res.axiomas_evaluados.append(ax.axioma_id)

            if not ax.vigente(momento):
                res.trace.append(Traza(ax.axioma_id, "OMITIDO_VIGENCIA",
                                       ax.prioridad,
                                       {"vigencia_hasta": ax.vigencia_hasta}))
                continue

            faltan = variables_faltantes(ax.variables_requeridas, datos)
            if faltan:
                faltantes_global.extend(faltan)
                res.trace.append(Traza(ax.axioma_id, "NO_EVALUABLE", ax.prioridad,
                                       {"variables_faltantes": faltan}))
                continue

            try:
                activo = bool(EVALUADOR_GLOBAL.evaluar(ax.condicion_expr, datos))
            except ExprError as e:
                log.error("Axioma %s: %s", ax.axioma_id, e)
                res.trace.append(Traza(ax.axioma_id, "ERROR", ax.prioridad,
                                       {"detalle": str(e)}))
                continue

            if activo:
                activados.append(ax)
                res.axiomas_activados.append(ax.axioma_id)
                res.trace.append(Traza(ax.axioma_id, "ACTIVADO", ax.prioridad, {
                    "descripcion": ax.descripcion,
                    "condicion": ax.condicion_expr,
                    "evidencia": {v: _valor(datos, v) for v in ax.variables_requeridas},
                    "consecuencia": ax.consecuencia,
                }))
            else:
                res.trace.append(Traza(ax.axioma_id, "NO_ACTIVADO", ax.prioridad,
                                       {"condicion": ax.condicion_expr}))

        res.variables_faltantes = sorted(set(faltantes_global))
        self._decidir(res, activados)
        res.ms = (time.perf_counter() - inicio) * 1000
        return res

    # ------------------------------------------------------------------ decisión

    def _decidir(self, res: Resultado, activados: Sequence[Axioma]) -> None:
        if not activados:
            if res.variables_faltantes:
                res.estado = Estado.INFORMACION_INSUFICIENTE.value
                res.explicacion = (
                    "No se puede emitir una decisión: faltan datos para evaluar "
                    "los axiomas del dominio (" +
                    ", ".join(res.variables_faltantes) + ")."
                )
            else:
                res.estado = Estado.SIN_AXIOMAS.value
                res.explicacion = (
                    "Ningún axioma se activó con los datos proporcionados. "
                    "ANA se abstiene de decidir en lugar de asumir un resultado."
                )
            return

        # ordenar por severidad y luego prioridad
        ordenados = sorted(
            activados,
            key=lambda a: (SEVERIDAD.get(a.estado_consecuencia, 0), a.prioridad),
            reverse=True,
        )
        ganador = ordenados[0]

        # conflicto: misma prioridad y misma severidad máxima, estados distintos
        empatados = [a for a in ordenados
                     if a.prioridad == ganador.prioridad
                     and a.estado_consecuencia != ganador.estado_consecuencia
                     and SEVERIDAD.get(a.estado_consecuencia, 0)
                     == SEVERIDAD.get(ganador.estado_consecuencia, 0)]
        if empatados:
            res.estado = Estado.CONFLICTO.value
            res.conflictos = [{
                "axiomas": [ganador.axioma_id] + [a.axioma_id for a in empatados],
                "estados": sorted({ganador.estado_consecuencia} |
                                  {a.estado_consecuencia for a in empatados}),
                "prioridad": ganador.prioridad,
            }]
            res.explicacion = (
                "Axiomas contradictorios con la misma prioridad: "
                + ", ".join(res.conflictos[0]["axiomas"])
                + ". Se requiere intervención humana."
            )
            return

        res.estado = ganador.estado_consecuencia
        res.decision = dict(ganador.consecuencia, axioma_decisorio=ganador.axioma_id)
        res.explicacion = self._explicar(res, ganador, activados)

    @staticmethod
    def _explicar(res: Resultado, ganador: Axioma,
                  activados: Sequence[Axioma]) -> str:
        motivo = ganador.consecuencia.get("motivo") or ganador.descripcion
        lineas = [
            f"Decisión: {res.estado}. {motivo}".strip(),
            f"Axioma decisorio: {ganador.axioma_id} "
            f"(prioridad {ganador.prioridad}) — {ganador.descripcion}".strip(),
            f"Condición cumplida: {ganador.condicion_expr}",
        ]
        evidencia = next((t.detalle.get("evidencia", {}) for t in res.trace
                          if t.axioma_id == ganador.axioma_id), {})
        if evidencia:
            lineas.append("Evidencia: " + ", ".join(
                f"{k}={v!r}" for k, v in evidencia.items()))
        otros = [a.axioma_id for a in activados if a is not ganador]
        if otros:
            lineas.append("También se activaron: " + ", ".join(otros))
        if res.variables_faltantes:
            lineas.append(
                "Axiomas no evaluados por falta de datos: "
                + ", ".join(res.variables_faltantes))
        return "\n".join(lineas)


def _valor(datos: Dict[str, Any], ruta: str) -> Any:
    from .expr import resolver
    _, v = resolver(datos, ruta)
    return v
