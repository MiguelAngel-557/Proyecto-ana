"""
ana.loader — Descubrimiento y carga automática de axiomas.

En vez de un único `axiomas.json` que se edita a mano, el conocimiento vive en
un árbol de archivos:

    axiomas/
      transporte/
        hazmat.yaml
        pesos.yaml
        autonomia.yaml
      aduanas/
        documentacion.yaml

Reglas de automatización:
  * El dominio se deduce del nombre de la carpeta si el archivo no lo declara.
  * Cada archivo puede traer un bloque `defaults:` que heredan todos sus axiomas
    (dominio, fuente, prioridad, vigencia, etiquetas...).
  * El `axioma_id` se autogenera con el prefijo del archivo si no se escribe.
  * `variables_requeridas` se deduce de la expresión (ver ana.expr).
  * Formatos soportados: .yaml/.yml, .json, .csv.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import Axioma, Hallazgo

try:
    import yaml  # type: ignore
    HAY_YAML = True
except ImportError:  # pragma: no cover
    HAY_YAML = False

EXTENSIONES = {".yaml", ".yml", ".json", ".csv"}
_CAMPOS_BOOL_CSV = {"activo"}
_CAMPOS_LISTA_CSV = {"variables_requeridas", "etiquetas"}


# --------------------------------------------------------------------------

def _leer_archivo(ruta: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Devuelve (defaults, lista_de_axiomas_crudos) para cualquier formato."""
    texto = ruta.read_text(encoding="utf-8-sig")

    if ruta.suffix.lower() in (".yaml", ".yml"):
        if not HAY_YAML:
            raise RuntimeError(
                f"{ruta.name} es YAML pero PyYAML no está instalado. "
                "Ejecuta: uv add pyyaml"
            )
        data = yaml.safe_load(texto) or {}
    elif ruta.suffix.lower() == ".json":
        data = json.loads(texto) if texto.strip() else []
    elif ruta.suffix.lower() == ".csv":
        filas = list(csv.DictReader(texto.splitlines()))
        return {}, [_normalizar_fila_csv(f) for f in filas]
    else:
        raise RuntimeError(f"Extensión no soportada: {ruta.suffix}")

    if isinstance(data, list):
        return {}, data
    if isinstance(data, dict):
        defaults = data.get("defaults", {}) or {}
        axiomas = data.get("axiomas", []) or []
        if not axiomas and "axioma_id" in data:   # archivo con un solo axioma
            axiomas = [data]
            defaults = {}
        return defaults, axiomas
    raise RuntimeError(f"Estructura no reconocida en {ruta.name}")


def _normalizar_fila_csv(fila: Dict[str, str]) -> Dict[str, Any]:
    salida: Dict[str, Any] = {}
    consecuencia: Dict[str, Any] = {}
    for clave, valor in fila.items():
        if clave is None or valor is None or valor == "":
            continue
        clave = clave.strip()
        if clave.startswith("consecuencia."):
            consecuencia[clave.split(".", 1)[1]] = valor
        elif clave in _CAMPOS_LISTA_CSV:
            salida[clave] = [v.strip() for v in valor.split("|") if v.strip()]
        elif clave in _CAMPOS_BOOL_CSV:
            salida[clave] = valor.strip().lower() in ("1", "true", "si", "sí", "yes")
        elif clave == "prioridad":
            salida[clave] = int(valor)
        else:
            salida[clave] = valor
    if consecuencia:
        salida["consecuencia"] = consecuencia
    return salida


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", texto.upper())[:6] or "AX"


# --------------------------------------------------------------------------

def cargar_directorio(
    raiz: str | Path = "axiomas",
) -> Tuple[List[Axioma], List[Hallazgo]]:
    """
    Recorre el árbol y devuelve los axiomas normalizados junto con los
    hallazgos de carga (archivos ilegibles, axiomas mal formados...).
    Nunca lanza excepción por un archivo roto: lo reporta y sigue.
    """
    raiz = Path(raiz)
    axiomas: List[Axioma] = []
    hallazgos: List[Hallazgo] = []

    if not raiz.exists():
        hallazgos.append(Hallazgo(
            "ERROR", "DIRECTORIO_INEXISTENTE",
            f"No existe el directorio de axiomas: {raiz}",
        ))
        return axiomas, hallazgos

    archivos = sorted(
        p for p in raiz.rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONES
        and not p.name.startswith((".", "_"))
    )

    if not archivos:
        hallazgos.append(Hallazgo(
            "ADVERTENCIA", "SIN_ARCHIVOS",
            f"No se encontró ningún archivo de axiomas bajo {raiz}",
        ))

    for archivo in archivos:
        rel = str(archivo.relative_to(raiz))
        try:
            defaults, crudos = _leer_archivo(archivo)
        except Exception as e:
            hallazgos.append(Hallazgo(
                "ERROR", "ARCHIVO_ILEGIBLE", str(e), archivo=rel))
            continue

        # dominio implícito = carpeta contenedora
        dominio_carpeta = (
            archivo.parent.name if archivo.parent != raiz else None
        )
        prefijo = defaults.get("prefijo_id") or _slug(archivo.stem)

        for i, crudo in enumerate(crudos, start=1):
            if not isinstance(crudo, dict):
                hallazgos.append(Hallazgo(
                    "ERROR", "AXIOMA_MAL_FORMADO",
                    f"El elemento #{i} no es un objeto.", archivo=rel))
                continue

            fusionado: Dict[str, Any] = {
                k: v for k, v in defaults.items() if k != "prefijo_id"
            }
            fusionado.update(crudo)

            fusionado.setdefault("dominio", dominio_carpeta or "general")
            fusionado.setdefault("axioma_id", f"AX-{prefijo}-{i:03d}")

            # azúcar sintáctico: `estado:` y `motivo:` sueltos -> consecuencia
            if "consecuencia" not in fusionado and "estado" in fusionado:
                fusionado["consecuencia"] = {
                    "estado": fusionado.pop("estado"),
                    "motivo": fusionado.pop("motivo", ""),
                }

            try:
                ax = Axioma.from_dict(fusionado)
                ax.archivo_origen = rel
                ax.normalizar()
                axiomas.append(ax)
            except Exception as e:
                hallazgos.append(Hallazgo(
                    "ERROR", "AXIOMA_INVALIDO", str(e),
                    axioma_id=str(fusionado.get("axioma_id", f"#{i}")),
                    archivo=rel))

    return axiomas, hallazgos


def huella_directorio(raiz: str | Path = "axiomas") -> str:
    """
    Huella barata del estado del directorio (ruta + mtime + tamaño).
    El motor la consulta para detectar cambios y recargar sin reiniciar.
    """
    import hashlib
    raiz = Path(raiz)
    if not raiz.exists():
        return "vacio"
    h = hashlib.sha256()
    for p in sorted(raiz.rglob("*")):
        if p.is_file() and p.suffix.lower() in EXTENSIONES:
            st = p.stat()
            h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode())
    return h.hexdigest()[:16]


def migrar_axiomas_json(
    origen: str | Path = "axiomas.json",
    destino_dir: str | Path = "axiomas",
) -> List[Path]:
    """
    Convierte el `axiomas.json` plano actual al árbol de archivos YAML,
    agrupando por dominio. Ejecutar una sola vez.
    """
    origen, destino_dir = Path(origen), Path(destino_dir)
    data = json.loads(origen.read_text(encoding="utf-8-sig"))
    por_dominio: Dict[str, List[Dict[str, Any]]] = {}
    for item in data:
        por_dominio.setdefault(item.get("dominio", "general"), []).append(item)

    escritos: List[Path] = []
    for dominio, items in por_dominio.items():
        carpeta = destino_dir / dominio
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / "migrados.yaml"
        cuerpo = {
            "defaults": {"dominio": dominio, "fuente": "migración axiomas.json"},
            "axiomas": [
                {k: v for k, v in it.items()
                 if k not in ("dominio", "variables_requeridas")}
                for it in items
            ],
        }
        if HAY_YAML:
            ruta.write_text(
                yaml.safe_dump(cuerpo, allow_unicode=True, sort_keys=False),
                encoding="utf-8")
        else:
            ruta = ruta.with_suffix(".json")
            ruta.write_text(json.dumps(cuerpo, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        escritos.append(ruta)
    return escritos
