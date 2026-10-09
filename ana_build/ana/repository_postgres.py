"""
ana.repository_postgres — Persistencia en PostgreSQL + pgvector.

Reemplaza Neo4j. Ventajas:

  * JSON nativo: contextos de decisión sin serializar
  * pgvector: preparado para embeddings luego (búsqueda semántica de axiomas)
  * SQL plano: queries complejas sin aprender Cypher
  * Auditoría integrada: registro de decisiones en BD, consultas OLAP directas
  * Transacciones: garantías ACID que un grafo no ofrece tan fácil
  * Hosting: barato (Neon, Vercel, Railway), backups triviales

Requiere: pip install psycopg2-binary
(pgvector es opcional, sólo para búsqueda semántica de axiomas más adelante)

La interfaz es idéntica a RepositorioNeo4j, así que el motor no se entera.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .expr import validar_expr
from .models import ESTADOS_CONSECUENCIA, Axioma, EstadoRevision

log = logging.getLogger("ana.repo.postgres")

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    HAY_POSTGRES = True
except ImportError:
    HAY_POSTGRES = False


def _intentar_pgvector(conn) -> None:
    """
    Intenta habilitar pgvector para uso futuro (embeddings de axiomas para
    búsqueda semántica). No es necesaria para nada de lo que hace el sistema
    hoy, así que si la extensión no está disponible en el servidor (algunos
    planes gratuitos la restringen) no debe impedir que ANA funcione.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.info(
            "Extensión pgvector no disponible (%s). No es necesaria por ahora; "
            "ANA funciona igual sin ella.", e)


class RepositorioPostgres:
    nombre = "postgresql"

    def __init__(self, dsn: Optional[str] = None):
        if not HAY_POSTGRES:
            raise RuntimeError(
                "PostgreSQL no instalado. Ejecuta: pip install psycopg2-binary"
            )
        if not dsn:
            dsn = os.getenv("DATABASE_URL") or (
                "postgresql://postgres:postgres@localhost:5432/ana"
            )
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self.conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        _intentar_pgvector(self.conn)
        self._crear_esquema()

    def _crear_esquema(self) -> None:
        """Crea tablas si no existen."""
        with self.conn.cursor() as cur:
            # Tabla de axiomas: JSON completo + hash para sync incremental
            cur.execute("""
            CREATE TABLE IF NOT EXISTS axiomas (
                axioma_id TEXT PRIMARY KEY,
                dominio TEXT NOT NULL,
                descripcion TEXT,
                condicion_expr TEXT NOT NULL,
                consecuencia JSONB NOT NULL,
                prioridad INT NOT NULL,
                variables_requeridas TEXT[] DEFAULT '{}',
                fuente TEXT,
                version TEXT,
                vigencia_desde DATE,
                vigencia_hasta DATE,
                etiquetas TEXT[] DEFAULT '{}',
                activo BOOLEAN DEFAULT true,
                hash TEXT NOT NULL,
                archivo_origen TEXT,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # Índices para las queries comunes
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_dominio ON axiomas(dominio)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_activo ON axiomas(activo)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_hash ON axiomas(hash)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_vigencia ON axiomas(vigencia_desde, vigencia_hasta)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_prioridad ON axiomas(dominio, prioridad DESC, axioma_id)"
            )

            # Migración: países aplicables y origen del registro (archivo vs. manual)
            cur.execute(
                "ALTER TABLE axiomas ADD COLUMN IF NOT EXISTS paises TEXT[] DEFAULT '{\"*\"}'"
            )
            cur.execute(
                "ALTER TABLE axiomas ADD COLUMN IF NOT EXISTS origen TEXT DEFAULT 'archivo'"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_paises ON axiomas USING GIN (paises)"
            )

            # Migración Fase 1: revisión humana y trazabilidad de la fuente.
            # Las filas existentes quedan como APROBADO (comportamiento actual).
            cur.execute(
                "ALTER TABLE axiomas ADD COLUMN IF NOT EXISTS "
                "estado_revision TEXT NOT NULL DEFAULT 'APROBADO'"
            )
            for col in ("fuente_url", "fuente_documento", "fuente_referencia",
                        "fuente_fragmento", "generado_por", "revisado_por",
                        "motivo_revision"):
                cur.execute(f"ALTER TABLE axiomas ADD COLUMN IF NOT EXISTS {col} TEXT")
            cur.execute(
                "ALTER TABLE axiomas ADD COLUMN IF NOT EXISTS revisado_en TIMESTAMP"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_axiomas_revision ON axiomas(estado_revision)"
            )
            # Tabla de decisiones: auditoría integrada
            cur.execute("""
            CREATE TABLE IF NOT EXISTS decisiones (
                decision_id SERIAL PRIMARY KEY,
                momento TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dominio TEXT NOT NULL,
                entrada JSONB NOT NULL,
                estado TEXT NOT NULL,
                axioma_decisorio TEXT,
                axiomas_evaluados TEXT[],
                axiomas_activados TEXT[],
                decision JSONB,
                explicacion TEXT,
                version_base TEXT,
                ms_inferencia FLOAT,
                trace JSONB
            );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_decisiones_momento ON decisiones(momento DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_decisiones_dominio ON decisiones(dominio)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_decisiones_estado ON decisiones(estado)"
            )

            # Tabla de relaciones del grafo (opcional, para queries complejas)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS grafo_relaciones (
                origen_tipo TEXT NOT NULL,
                origen_id TEXT NOT NULL,
                relacion TEXT NOT NULL,
                destino_tipo TEXT NOT NULL,
                destino_id TEXT NOT NULL,
                datos JSONB,
                PRIMARY KEY (origen_tipo, origen_id, relacion, destino_tipo, destino_id)
            );
            """)

        self.conn.commit()
        log.info("Esquema PostgreSQL listo")

    # ---- lectura

    def listar(self, dominio: Optional[str] = None,
               solo_activos: bool = True) -> List[Axioma]:
        with self.conn.cursor() as cur:
            filtros = []
            params = []
            if dominio:
                filtros.append("dominio = %s")
                params.append(dominio)
            if solo_activos:
                # el motor sólo ve axiomas activos Y aprobados (defensa en profundidad)
                filtros.append("activo = true")
                filtros.append("estado_revision = 'APROBADO'")

            where = " AND ".join(filtros) if filtros else "true"
            cur.execute(
                f"""
                SELECT * FROM axiomas
                WHERE {where}
                ORDER BY prioridad DESC, axioma_id
                """,
                params,
            )
            return [self._fila_a_axioma(f) for f in cur.fetchall()]

    def version(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(STRING_AGG(hash, '|' ORDER BY axioma_id), '') "
                "FROM axiomas WHERE activo = true AND estado_revision = 'APROBADO'"
            )
            fila = cur.fetchone()
            hashes = fila["coalesce"] if fila else ""
        if not hashes:
            return "vacio"
        import hashlib

        return hashlib.sha256(hashes.encode()).hexdigest()[:16]

    # ---- escritura

    def sincronizar(self, axiomas: Sequence[Axioma]) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT axioma_id, hash FROM axiomas WHERE activo = true"
            )
            existentes = {r["axioma_id"]: r["hash"] for r in cur.fetchall()}

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

        # Desactivar los que desaparecieron del origen
        desactivados = []
        if ids_origen:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE axiomas SET activo = false, actualizado_en = CURRENT_TIMESTAMP
                    WHERE activo = true AND origen = 'archivo' AND axioma_id != ALL(%s)
                    RETURNING axioma_id
                    """,
                    (list(ids_origen),),
                )
                desactivados = [r[0] for r in cur.fetchall()]

        self.conn.commit()
        return {
            "destino": self.nombre,
            "nuevos": nuevos,
            "actualizados": actualizados,
            "sin_cambio": sin_cambio,
            "desactivados": desactivados,
            "version": self.version(),
        }

    def _insertar_axioma(self, ax: Axioma, origen: Optional[str] = None,
                         activo: bool = True) -> None:
        columnas = (
            "axioma_id", "dominio", "descripcion", "condicion_expr",
            "consecuencia", "prioridad", "variables_requeridas",
            "fuente", "version", "vigencia_desde", "vigencia_hasta",
            "etiquetas", "hash", "archivo_origen", "paises", "origen",
            "activo", "estado_revision", "fuente_url", "fuente_documento",
            "fuente_referencia", "fuente_fragmento", "generado_por",
        )
        valores = (
            ax.axioma_id, ax.dominio, ax.descripcion, ax.condicion_expr,
            json.dumps(ax.consecuencia, ensure_ascii=False), ax.prioridad,
            ax.variables_requeridas, ax.fuente, ax.version,
            ax.vigencia_desde, ax.vigencia_hasta, ax.etiquetas, ax.hash,
            ax.archivo_origen, ax.paises, origen or ax.origen,
            activo, ax.estado_revision, ax.fuente_url, ax.fuente_documento,
            ax.fuente_referencia, ax.fuente_fragmento, ax.generado_por,
        )
        sql = (f"INSERT INTO axiomas ({', '.join(columnas)}) "
               f"VALUES ({', '.join(['%s'] * len(columnas))})")
        with self.conn.cursor() as cur:
            cur.execute(sql, valores)


    def crear_manual(self, ax: Axioma) -> None:
        """Inserta un axioma directo a la base, sin pasar por YAML (formulario web)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM axiomas WHERE axioma_id = %s", (ax.axioma_id,))
            if cur.fetchone():
                raise ValueError(f"Ya existe un axioma con id '{ax.axioma_id}'.")
        self._insertar_axioma(ax, origen="manual")
        self.conn.commit()

    # ---- propuestas y revisión humana (Fase 1)

    @staticmethod
    def _fila_a_axioma(fila) -> Axioma:
        d = dict(fila)
        # fechas de Postgres -> texto ISO (el modelo trabaja con str)
        for k in ("vigencia_desde", "vigencia_hasta", "revisado_en"):
            if d.get(k) is not None and not isinstance(d[k], str):
                d[k] = d[k].isoformat()
        # columnas de texto nuevas: NULL -> ""
        for k in ("fuente_url", "fuente_documento", "fuente_referencia",
                  "fuente_fragmento", "generado_por", "revisado_por",
                  "motivo_revision"):
            d[k] = d.get(k) or ""
        return Axioma.from_dict(d)

    def obtener(self, axioma_id: str) -> Optional[Axioma]:
        """Devuelve un axioma en cualquier estado (o None si no existe)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM axiomas WHERE axioma_id = %s", (axioma_id,))
            fila = cur.fetchone()
        return self._fila_a_axioma(fila) if fila else None

    def crear_propuesta(self, ax: Axioma) -> None:
        """
        Inserta un candidato (p. ej. generado por IA). SIEMPRE queda
        PENDIENTE_REVISION e inactivo, sin importar lo que traiga el objeto.
        """
        ax.estado_revision = EstadoRevision.PENDIENTE.value
        ax.activo = False
        if ax.origen not in ("ia_generado", "bulk_import"):
            ax.origen = "ia_generado"
        ax.normalizar()  # recalcula variables y hash con activo=False
        if self.obtener(ax.axioma_id) is not None:
            raise ValueError(f"Ya existe un axioma con id '{ax.axioma_id}'.")
        self._insertar_axioma(ax, activo=False)

    def listar_propuestas(
        self, estado_revision: str = EstadoRevision.PENDIENTE.value
    ) -> List[Axioma]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM axiomas WHERE estado_revision = %s "
                "ORDER BY creado_en, axioma_id",
                (estado_revision,),
            )
            return [self._fila_a_axioma(f) for f in cur.fetchall()]

    def aprobar(self, axioma_id: str, revisado_por: str) -> Axioma:
        return self._revisar(axioma_id, revisado_por, aprobar=True)

    def rechazar(self, axioma_id: str, revisado_por: str, motivo: str) -> Axioma:
        return self._revisar(axioma_id, revisado_por, aprobar=False, motivo=motivo)

    def _revisar(self, axioma_id: str, revisado_por: str, aprobar: bool,
                 motivo: str = "") -> Axioma:
        ax = self.obtener(axioma_id)
        if ax is None:
            raise KeyError(axioma_id)
        if ax.estado_revision != EstadoRevision.PENDIENTE.value:
            raise ValueError(
                f"'{axioma_id}' no está pendiente de revisión "
                f"(estado actual: {ax.estado_revision})."
            )
        if aprobar:
            # revalida justo antes de activar: la fila pudo editarse a mano
            validar_expr(ax.condicion_expr)          # ExprError (es ValueError)
            if ax.estado_consecuencia not in ESTADOS_CONSECUENCIA:
                raise ValueError(
                    f"Consecuencia con estado inválido: '{ax.estado_consecuencia}'."
                )
        nuevo = (EstadoRevision.APROBADO if aprobar else EstadoRevision.RECHAZADO).value
        ax.activo = aprobar
        ax.estado_revision = nuevo
        ax.normalizar()  # el hash incluye `activo`
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE axiomas SET
                    estado_revision = %s, activo = %s, revisado_por = %s,
                    revisado_en = CURRENT_TIMESTAMP, motivo_revision = %s,
                    hash = %s, actualizado_en = CURRENT_TIMESTAMP
                WHERE axioma_id = %s AND estado_revision = %s
                """,
                (nuevo, aprobar, revisado_por, motivo, ax.hash,
                 axioma_id, EstadoRevision.PENDIENTE.value),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    f"'{axioma_id}' ya fue revisada por otra persona."
                )
        return self.obtener(axioma_id)
    
    def _actualizar_axioma(self, ax: Axioma) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE axiomas SET
                    dominio = %s,
                    descripcion = %s,
                    condicion_expr = %s,
                    consecuencia = %s,
                    prioridad = %s,
                    variables_requeridas = %s,
                    fuente = %s,
                    version = %s,
                    vigencia_desde = %s,
                    vigencia_hasta = %s,
                    etiquetas = %s,
                    hash = %s,
                    archivo_origen = %s,
                    actualizado_en = CURRENT_TIMESTAMP
                WHERE axioma_id = %s
                """,
                (
                    ax.dominio,
                    ax.descripcion,
                    ax.condicion_expr,
                    json.dumps(ax.consecuencia, ensure_ascii=False),
                    ax.prioridad,
                    ax.variables_requeridas,
                    ax.fuente,
                    ax.version,
                    ax.vigencia_desde,
                    ax.vigencia_hasta,
                    ax.etiquetas,
                    ax.hash,
                    ax.archivo_origen,
                    ax.axioma_id,
                ),
            )

    def registrar_decision(self, dominio: str, entrada: Dict[str, Any],
                          estado: str, axioma_decisorio: Optional[str],
                          axiomas_evaluados: List[str],
                          axiomas_activados: List[str],
                          decision: Optional[Dict[str, Any]],
                          explicacion: str, version_base: str,
                          ms_inferencia: float,
                          trace: Optional[List[Dict[str, Any]]] = None) -> None:
        """Registra una decisión en la auditoría."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisiones (
                    dominio, entrada, estado, axioma_decisorio,
                    axiomas_evaluados, axiomas_activados, decision,
                    explicacion, version_base, ms_inferencia, trace
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    dominio,
                    json.dumps(entrada, ensure_ascii=False, default=str),
                    estado,
                    axioma_decisorio,
                    axiomas_evaluados,
                    axiomas_activados,
                    json.dumps(decision, ensure_ascii=False, default=str)
                    if decision
                    else None,
                    explicacion,
                    version_base,
                    ms_inferencia,
                    json.dumps(trace, ensure_ascii=False, default=str)
                    if trace
                    else None,
                ),
            )
        self.conn.commit()

    def obtener_auditoria(self, dominio: Optional[str] = None,
                         limite: int = 50) -> List[Dict[str, Any]]:
        """Consulta decisiones recientes desde la BD."""
        with self.conn.cursor() as cur:
            filtro = "dominio = %s" if dominio else "true"
            params = [dominio] if dominio else []
            cur.execute(
                f"""
                SELECT * FROM decisiones
                WHERE {filtro}
                ORDER BY momento DESC
                LIMIT %s
                """,
                params + [limite],
            )
            return [dict(r) for r in cur.fetchall()]

    def cerrar(self) -> None:
        self.conn.close()
