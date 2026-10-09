"""
ana.cli — Automatización desde la terminal y desde CI.

  python -m ana.cli validar              # sólo valida, no escribe nada
  python -m ana.cli sync                 # valida y sincroniza al repositorio
  python -m ana.cli sync --seco          # simulacro: muestra qué cambiaría
  python -m ana.cli sync --repo archivo  # fuerza el respaldo local
  python -m ana.cli vigilar              # recarga sola al guardar un archivo
  python -m ana.cli migrar               # convierte axiomas.json al árbol nuevo
  python -m ana.cli probar --dominio transporte --datos caso.json

`validar` y `sync` devuelven código de salida 1 si hay errores, para poder
ponerlos como paso obligatorio en el pipeline de CI o en un pre-commit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .engine import MotorANA
from .loader import cargar_directorio, huella_directorio, migrar_axiomas_json
from .repository import obtener_repositorio
from .validator import hay_errores, validar

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")


def _cargar_env(ruta: str = ".env") -> None:
    p = Path(ruta)
    if not p.exists():
        return
    for linea in p.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"\''))


def _imprimir(hallazgos, resumen=None) -> None:
    errores = [h for h in hallazgos if h.nivel == "ERROR"]
    avisos = [h for h in hallazgos if h.nivel == "ADVERTENCIA"]
    for h in errores + avisos:
        print(" ", h)
    if resumen:
        print("\n  Cobertura por dominio:")
        for dominio, datos in resumen.items():
            print(f"    {dominio}: {datos['axiomas']} axiomas, "
                  f"{datos['casos_generados']} casos generados, "
                  f"{datos['conflictos']} conflictos")
            nunca = [k for k, v in datos["cobertura"].items() if v == 0]
            if nunca:
                print(f"      sin activarse: {', '.join(nunca)}")
    print(f"\n  {len(errores)} errores, {len(avisos)} advertencias.")


# --------------------------------------------------------------------------

def cmd_validar(args) -> int:
    axiomas, hallazgos_carga = cargar_directorio(args.dir)
    hallazgos_val, resumen = validar(axiomas, profundo=not args.rapido)
    todos = hallazgos_carga + hallazgos_val
    print(f"\n  {len(axiomas)} axiomas cargados desde {args.dir}/\n")
    _imprimir(todos, resumen if not args.rapido else None)
    return 1 if hay_errores(todos) else 0


def cmd_sync(args) -> int:
    _cargar_env()
    axiomas, hallazgos_carga = cargar_directorio(args.dir)
    hallazgos_val, resumen = validar(axiomas, profundo=not args.rapido)
    todos = hallazgos_carga + hallazgos_val

    print(f"\n  {len(axiomas)} axiomas cargados desde {args.dir}/\n")
    _imprimir(todos, resumen if not args.rapido else None)

    if hay_errores(todos):
        print("\n  Sincronización cancelada: corrige los errores primero.")
        return 1

    if args.seco:
        repo = obtener_repositorio(args.repo)
        actuales = {a.axioma_id: a.hash for a in repo.listar(solo_activos=False)}
        nuevos = [a.axioma_id for a in axiomas if a.axioma_id not in actuales]
        cambia = [a.axioma_id for a in axiomas
                  if a.axioma_id in actuales and actuales[a.axioma_id] != a.hash]
        fuera = [i for i in actuales if i not in {a.axioma_id for a in axiomas}]
        print(f"\n  SIMULACRO ({repo.nombre}): "
              f"{len(nuevos)} nuevos, {len(cambia)} actualizados, "
              f"{len(fuera)} a desactivar")
        for lista, etiqueta in ((nuevos, "+"), (cambia, "~"), (fuera, "-")):
            for i in lista:
                print(f"    {etiqueta} {i}")
        repo.cerrar()
        return 0

    repo = obtener_repositorio(args.repo)
    r = repo.sincronizar(axiomas)
    repo.cerrar()
    print(f"\n  Sincronizado en '{r['destino']}': "
          f"{len(r['nuevos'])} nuevos, {len(r['actualizados'])} actualizados, "
          f"{r['sin_cambio']} sin cambio, {len(r['desactivados'])} desactivados.")
    print(f"  Versión de la base: {r['version']}")
    return 0


def cmd_vigilar(args) -> int:
    _cargar_env()
    print(f"  Vigilando {args.dir}/ — Ctrl+C para salir.")
    ultima = None
    while True:
        actual = huella_directorio(args.dir)
        if actual != ultima:
            if ultima is not None:
                print(f"\n  [{time.strftime('%H:%M:%S')}] Cambio detectado.")
            ns = argparse.Namespace(dir=args.dir, repo=args.repo,
                                    rapido=True, seco=False)
            cmd_sync(ns)
            ultima = actual
        time.sleep(args.intervalo)


def cmd_migrar(args) -> int:
    escritos = migrar_axiomas_json(args.origen, args.dir)
    for p in escritos:
        print(f"  escrito: {p}")
    print(f"\n  Listo. Revisa los archivos y ejecuta: python -m ana.cli validar")
    return 0


def cmd_probar(args) -> int:
    _cargar_env()
    datos = json.loads(Path(args.datos).read_text(encoding="utf-8"))
    motor = MotorANA(obtener_repositorio(args.repo))
    res = motor.evaluar(args.dominio, datos)
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    print("\n" + res.explicacion)
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ana", description="Utilidades ANA Core")
    p.add_argument("--dir", default="axiomas", help="directorio de axiomas")
    p.add_argument("--repo", choices=["auto", "neo4j", "postgres", "duckdb", "archivo"], default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validar", help="valida sin escribir")
    v.add_argument("--rapido", action="store_true",
                   help="omite el análisis de contradicciones")
    v.set_defaults(func=cmd_validar)

    s = sub.add_parser("sync", help="valida y sincroniza")
    s.add_argument("--seco", action="store_true", help="simulacro")
    s.add_argument("--rapido", action="store_true")
    s.set_defaults(func=cmd_sync)

    w = sub.add_parser("vigilar", help="sincroniza al detectar cambios")
    w.add_argument("--intervalo", type=float, default=1.5)
    w.set_defaults(func=cmd_vigilar)

    m = sub.add_parser("migrar", help="convierte axiomas.json al árbol nuevo")
    m.add_argument("--origen", default="axiomas.json")
    m.set_defaults(func=cmd_migrar)

    t = sub.add_parser("probar", help="evalúa un caso desde un JSON")
    t.add_argument("--dominio", required=True)
    t.add_argument("--datos", required=True)
    t.set_defaults(func=cmd_probar)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
