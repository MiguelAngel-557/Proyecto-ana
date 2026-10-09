"""
ana.repository_duckdb — Persistencia en DuckDB.

El repositorio más ligero: cero infraestructura, archivo local, OLAP nativo.
Ideal para desarrollo, CI/CD y como primer paso antes de PostgreSQL.

Requiere: pip install duckdb

Nota de implementación: se usan parámetros posicionales (?) en vez de
parámetros con nombre ($x), y se evita RETURNING, porque ambas cosas variaron
entre versiones de DuckDB. Esto es más verboso pero funciona igual en
cualquier versión reciente.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import Axioma

log = logging.getLogger("ana.repo.duckdb")

try:
    import duckdb
    HAY_DUCKDB = True
except ImportError:
    HAY_DUCKDB = False


def _filas_a_dicts(cursor) -> List[Dict[str, Any]]:
    """Convierte el resultado de una consulta a lista de diccionarios,
    usando cursor.description para los nombres de columna. fetchall()
    en DuckDB devuelve tuplas, no dicts, así que esto es necesario."""
    columnas = [c[0] for c in cursor.description]
    return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


class RepositorioDuckDB:
    nombre = "duckdb"

    def __init__(self, ruta: str | Path = "build/ana.duckdb"):
        if not HAY_DUCKDB:
            raise RuntimeError(
                "DuckDB no está instalado. Ejecuta: pip install duckdb"
            )
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.ruta))
        self._crear_esquema()

    def _crear_esquema(self) -> None:
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS axiomas (
            axioma_id TEXT PRIMARY KEY,
            dominio TEXT NOT NULL,
            descripcion TEXT,
            condicion_expr TEXT NOT NULL,
            consecuencia JSON NOT NULL,
            prioridad INT NOT NULL,
            variables_requeridas TEXT[],
            fuente TEXT,
            version TEXT,
            vigencia_desde DATE,
            vigencia_hasta DATE,
            etiquetas TEXT[],
            activo BOOLEAN DEFAULT true,
            hash TEXT NOT NULL,
            archivo_origen TEXT,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        self.conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_decisiones START 1
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS decisiones (
            decision_id BIGINT PRIMARY KEY DEFAULT nextval('seq_decisiones'),
            momento TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            dominio TEXT NOT NULL,
            entrada JSON NOT NULL,
            estado TEXT NOT NULL,
            axioma_decisorio TEXT,
            axiomas_evaluados TEXT[],
            axiomas_activados TEXT[],
            decision JSON,
            explicacion TEXT,
            version_base TEXT,
            ms_inferencia DOUBLE,
            trace JSON
        )
        """)
        log.info("Esquema DuckDB listo (%s)", self.ruta)

    # ---------------------------------------------------------------- lectura

    def listar(self, dominio: Optional[str] = None,
               solo_activos: bool = True) -> List[Axioma]:
        filtros, params = [], []
        if dominio:
            filtros.append("dominio = ?")
            params.append(dominio)
        if solo_activos:
            filtros.append("activo = true")
        where = " AND ".join(filtros) if filtros else "true"

        cur = self.conn.execute(
            f"SELECT * FROM axiomas WHERE {where} ORDER BY prioridad DESC, axioma_id",
            params,
        )
        salida = []
        for d in _filas_a_dicts(cur):
            if isinstance(d.get("consecuencia"), str):
                d["consecuencia"] = json.loads(d["consecuencia"])
            salida.append(Axioma.from_dict(d))
        return salida

    def version(self) -> str:
        fila = self.conn.execute(
            "SELECT hash FROM axiomas WHERE activo = true ORDER BY axioma_id"
        ).fetchall()
        hashes = [r[0] for r in fila]
        if not hashes:
            return "vacio"
        return hashlib.sha256("|".join(hashes).encode()).hexdigest()[:16]

    # ---------------------------------------------------------------- escritura

    def sincronizar(self, axiomas: Sequence[Axioma]) -> Dict[str, Any]:
        actuales = self.conn.execute(
            "SELECT axioma_id, hash FROM axiomas WHERE activo = true"
        ).fetchall()
        existentes = {r[0]: r[1] for r in actuales}

        nuevos, actualizados, sin_cambio = [], [], 0
        ids_origen = set()

        for ax in axiomas:
            ids_origen.add(ax.axioma_id)
            if ax.axioma_id not in existentes:
                nuevos.append(ax.axioma_id)
                self._insertar_axioma(ax)
            elif existentes[ax.axioma_id] != ax.hash:
                actualizados.append(ax.axioma_id)
                self._actualizar_axioma(ax)
            else:
                sin_cambio += 1

        desactivados: List[str] = []
        if ids_origen:
            marcador = ", ".join("?" for _ in ids_origen)
            filas = self.conn.execute(
                f"SELECT axioma_id FROM axiomas "
                f"WHERE activo = true AND axioma_id NOT IN ({marcador})",
                list(ids_origen),
            ).fetchall()
            desactivados = [r[0] for r in filas]
            if desactivados:
                marcador2 = ", ".join("?" for _ in desactivados)
                self.conn.execute(
                    f"UPDATE axiomas SET activo = false, "
                    f"actualizado_en = CURRENT_TIMESTAMP "
                    f"WHERE axioma_id IN ({marcador2})",
                    desactivados,
                )

        return {
            "destino": self.nombre,
            "nuevos": nuevos,
            "actualizados": actualizados,
            "sin_cambio": sin_cambio,
            "desactivados": desactivados,
            "version": self.version(),
        }

    def _insertar_axioma(self, ax: Axioma) -> None:
        self.conn.execute(
            """
            INSERT INTO axiomas (
                axioma_id, dominio, descripcion, condicion_expr,
                consecuencia, prioridad, variables_requeridas,
                fuente, version, vigencia_desde, vigencia_hasta,
                etiquetas, activo, hash, archivo_origen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ax.axioma_id, ax.dominio, ax.descripcion, ax.condicion_expr,
                json.dumps(ax.consecuencia, ensure_ascii=False), ax.prioridad,
                ax.variables_requeridas, ax.fuente, ax.version,
                ax.vigencia_desde, ax.vigencia_hasta, ax.etiquetas,
                ax.activo, ax.hash, ax.archivo_origen,
            ],
        )

    def _actualizar_axioma(self, ax: Axioma) -> None:
        self.conn.execute(
            """
            UPDATE axiomas SET
                dominio = ?, descripcion = ?, condicion_expr = ?,
                consecuencia = ?, prioridad = ?, variables_requeridas = ?,
                fuente = ?, version = ?, vigencia_desde = ?, vigencia_hasta = ?,
                etiquetas = ?, activo = ?, hash = ?, archivo_origen = ?,
                actualizado_en = CURRENT_TIMESTAMP
            WHERE axioma_id = ?
            """,
            [
                ax.dominio, ax.descripcion, ax.condicion_expr,
                json.dumps(ax.consecuencia, ensure_ascii=False), ax.prioridad,
                ax.variables_requeridas, ax.fuente, ax.version,
                ax.vigencia_desde, ax.vigencia_hasta, ax.etiquetas,
                ax.activo, ax.hash, ax.archivo_origen, ax.axioma_id,
            ],
        )

    def registrar_decision(self, dominio: str, entrada: Dict[str, Any],
                          estado: str, axioma_decisorio: Optional[str],
                          axiomas_evaluados: List[str],
                          axiomas_activados: List[str],
                          decision: Optional[Dict[str, Any]],
                          explicacion: str, version_base: str,
                          ms_inferencia: float,
                          trace: Optional[List[Dict[str, Any]]] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO decisiones (
                dominio, entrada, estado, axioma_decisorio,
                axiomas_evaluados, axiomas_activados, decision,
                explicacion, version_base, ms_inferencia, trace
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                dominio,
                json.dumps(entrada, ensure_ascii=False, default=str),
                estado, axioma_decisorio, axiomas_evaluados, axiomas_activados,
                json.dumps(decision, ensure_ascii=False, default=str) if decision else None,
                explicacion, version_base, ms_inferencia,
                json.dumps(trace, ensure_ascii=False, default=str) if trace else None,
            ],
        )

    def obtener_auditoria(self, dominio: Optional[str] = None,
                         limite: int = 50) -> List[Dict[str, Any]]:
        if dominio:
            cur = self.conn.execute(
                "SELECT * FROM decisiones WHERE dominio = ? "
                "ORDER BY momento DESC LIMIT ?",
                [dominio, limite],
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM decisiones ORDER BY momento DESC LIMIT ?",
                [limite],
            )
        return _filas_a_dicts(cur)

    def cerrar(self) -> None:
        self.conn.close()
