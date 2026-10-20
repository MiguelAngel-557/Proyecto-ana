"""
ana.repository — Persistencia de la base axiomática.

Dos implementaciones con la misma interfaz:

  RepositorioArchivo : compila los axiomas a build/axiomas.compilados.json.
                       Cero dependencias, arranca siempre. Es el respaldo.
  RepositorioNeo4j   : sincronización incremental por hash sobre el grafo.

`obtener_repositorio()` elige Neo4j si está configurado y responde; si no,
cae al de archivo y lo dice en el log. Así el proyecto nunca queda bloqueado
porque la base esté caída, y el modo local sirve para CI y para los tests.

La sincronización es incremental y no destructiva: los axiomas que
desaparecen del origen se marcan `activo=false` en lugar de borrarse, para
que la auditoría histórica siga resolviendo.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import Axioma

log = logging.getLogger("ana.repo")


class ResultadoSync(dict):
    """dict con los contadores de la sincronización."""


# --------------------------------------------------------------------------

class RepositorioArchivo:
    nombre = "archivo"

    def __init__(self, ruta: str | Path = "build/axiomas.compilados.json"):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)

    # ---- lectura

    def _leer(self) -> Dict[str, Dict[str, Any]]:
        if not self.ruta.exists():
            return {}
        try:
            data = json.loads(self.ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return {a["axioma_id"]: a for a in data.get("axiomas", [])}

    def listar(self, dominio: Optional[str] = None,
               solo_activos: bool = True) -> List[Axioma]:
        salida = []
        for crudo in self._leer().values():
            if dominio and crudo.get("dominio") != dominio:
                continue
            if solo_activos and not crudo.get("activo", True):
                continue
            salida.append(Axioma.from_dict(crudo))
        salida.sort(key=lambda a: (-a.prioridad, a.axioma_id))
        return salida

    def version(self) -> str:
        if not self.ruta.exists():
            return "vacio"
        try:
            return json.loads(self.ruta.read_text(encoding="utf-8")).get("version", "")
        except json.JSONDecodeError:
            return "corrupto"

    # ---- escritura

    def sincronizar(self, axiomas: Sequence[Axioma]) -> ResultadoSync:
        existentes = self._leer()
        nuevos, actualizados, sin_cambio = [], [], []
        ids_origen = {a.axioma_id for a in axiomas}

        fusion: Dict[str, Dict[str, Any]] = dict(existentes)
        for ax in axiomas:
            previo = existentes.get(ax.axioma_id)
            if previo is None:
                nuevos.append(ax.axioma_id)
            elif previo.get("hash") != ax.hash:
                actualizados.append(ax.axioma_id)
            else:
                sin_cambio.append(ax.axioma_id)
                continue
            d = ax.to_dict()
            d["actualizado_en"] = datetime.now(timezone.utc).isoformat()
            fusion[ax.axioma_id] = d

        desactivados = []
        for aid, crudo in fusion.items():
            if aid not in ids_origen and crudo.get("activo", True):
                crudo["activo"] = False
                crudo["desactivado_en"] = datetime.now(timezone.utc).isoformat()
                desactivados.append(aid)

        version = _huella([a.hash for a in axiomas])
        self.ruta.write_text(json.dumps(
            {"version": version,
             "generado_en": datetime.now(timezone.utc).isoformat(),
             "axiomas": list(fusion.values())},
            ensure_ascii=False, indent=2), encoding="utf-8")

        return ResultadoSync(destino=self.nombre, nuevos=nuevos,
                             actualizados=actualizados, sin_cambio=len(sin_cambio),
                             desactivados=desactivados, version=version)

    def cerrar(self) -> None:
        pass


# --------------------------------------------------------------------------

class RepositorioNeo4j:
    nombre = "neo4j"

    def __init__(self, uri: str, usuario: str, password: str):
        from neo4j import GraphDatabase  # import diferido
        self.driver = GraphDatabase.driver(uri, auth=(usuario, password))
        self.driver.verify_connectivity()
        self._asegurar_esquema()

    def _asegurar_esquema(self) -> None:
        self.driver.execute_query(
            "CREATE CONSTRAINT axioma_id_unico IF NOT EXISTS "
            "FOR (a:Axioma) REQUIRE a.axioma_id IS UNIQUE")
        self.driver.execute_query(
            "CREATE INDEX axioma_dominio IF NOT EXISTS "
            "FOR (a:Axioma) ON (a.dominio)")

    # ---- lectura

    def listar(self, dominio: Optional[str] = None,
               solo_activos: bool = True) -> List[Axioma]:
        consulta = """
        MATCH (a:Axioma)
        WHERE ($dominio IS NULL OR a.dominio = $dominio)
          AND ($solo_activos = false OR a.activo = true)
        RETURN a AS a
        ORDER BY a.prioridad DESC, a.axioma_id
        """
        registros, _, _ = self.driver.execute_query(
            consulta, dominio=dominio, solo_activos=solo_activos)
        salida = []
        for r in registros:
            crudo = dict(r["a"])
            crudo["consecuencia"] = json.loads(crudo.pop("consecuencia_json", "{}"))
            salida.append(Axioma.from_dict(crudo))
        return salida

    def version(self) -> str:
        registros, _, _ = self.driver.execute_query(
            "MATCH (a:Axioma) WHERE a.activo = true "
            "RETURN collect(a.hash) AS hashes")
        return _huella(registros[0]["hashes"] if registros else [])

    # ---- escritura

    def sincronizar(self, axiomas: Sequence[Axioma]) -> ResultadoSync:
        """Sólo escribe los axiomas cuyo hash cambió (sincronización incremental)."""
        registros, _, _ = self.driver.execute_query(
            "MATCH (a:Axioma) RETURN a.axioma_id AS id, a.hash AS hash")
        actuales = {r["id"]: r["hash"] for r in registros}

        lote, nuevos, actualizados, sin_cambio = [], [], [], 0
        for ax in axiomas:
            if ax.axioma_id not in actuales:
                nuevos.append(ax.axioma_id)
            elif actuales[ax.axioma_id] != ax.hash:
                actualizados.append(ax.axioma_id)
            else:
                sin_cambio += 1
                continue
            d = ax.to_dict()
            d["consecuencia_json"] = json.dumps(d.pop("consecuencia"),
                                                ensure_ascii=False)
            lote.append(d)

        if lote:
            self.driver.execute_query("""
            UNWIND $lote AS ax
            MERGE (a:Axioma {axioma_id: ax.axioma_id})
            SET a += ax, a.actualizado_en = datetime()
            WITH a, ax
            MERGE (d:Dominio {nombre: ax.dominio})
            MERGE (a)-[:PERTENECE_A]->(d)
            """, lote=lote)

        ids_origen = [a.axioma_id for a in axiomas]
        desact, _, _ = self.driver.execute_query("""
        MATCH (a:Axioma)
        WHERE NOT a.axioma_id IN $ids AND a.activo = true
        SET a.activo = false, a.desactivado_en = datetime()
        RETURN collect(a.axioma_id) AS ids
        """, ids=ids_origen)

        return ResultadoSync(
            destino=self.nombre, nuevos=nuevos, actualizados=actualizados,
            sin_cambio=sin_cambio,
            desactivados=desact[0]["ids"] if desact else [],
            version=_huella([a.hash for a in axiomas]))

    def cerrar(self) -> None:
        self.driver.close()


# --------------------------------------------------------------------------

def _huella(hashes: Sequence[str]) -> str:
    import hashlib
    return hashlib.sha256("|".join(sorted(hashes)).encode()).hexdigest()[:16]


def obtener_repositorio(forzar: Optional[str] = None):
    """
    forzar: None (auto) | 'neo4j' | 'postgres' | 'duckdb' | 'archivo'

    Auto intenta en orden: postgres → duckdb → archivo
    """
    modo = forzar or os.getenv("ANA_REPO", "auto")

    if modo in ("auto", "postgres"):
        dsn = os.getenv("DATABASE_URL")
        if dsn:
            try:
                from .repository_postgres import RepositorioPostgres
                repo = RepositorioPostgres(dsn)
                log.info("Repositorio: PostgreSQL")
                return repo
            except Exception as e:
                if modo == "postgres":
                    raise
                log.warning("PostgreSQL no disponible (%s). Intentando siguiente...", e)
        elif modo == "postgres":
            raise RuntimeError("Falta DATABASE_URL en el entorno.")

    if modo in ("auto", "duckdb"):
        try:
            from .repository_duckdb import RepositorioDuckDB
            repo = RepositorioDuckDB(os.getenv("ANA_DUCKDB_RUTA", "build/ana.duckdb"))
            log.info("Repositorio: DuckDB")
            return repo
        except Exception as e:
            if modo == "duckdb":
                raise
            log.warning("DuckDB no disponible (%s). Usando respaldo en archivo.", e)

    # Neo4j (si forzas explícitamente)
    if modo == "neo4j":
        uri = os.getenv("NEO4J_URI")
        usuario = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD")
        if uri and password:
            try:
                repo = RepositorioNeo4j(uri, usuario, password)
                log.info("Repositorio: Neo4j (%s)", uri)
                return repo
            except Exception as e:
                raise RuntimeError(f"Neo4j no conecta: {e}")
        raise RuntimeError("Faltan NEO4J_URI / NEO4J_PASSWORD en el entorno.")

    log.info("Repositorio: archivo local (respaldo)")
    return RepositorioArchivo()
