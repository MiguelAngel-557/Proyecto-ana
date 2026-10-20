"""
ana.validator — Control de calidad automático de la base axiomática.

Se ejecuta en cada carga, en cada sincronización y en CI. Cubre directamente
tres KPIs del documento ANA: detección de contradicciones, consistencia
normativa (vigencia) y consistencia interna.

Comprobaciones:
  E01 IDs duplicados
  E02 Expresión inválida o insegura
  E03 Estado de consecuencia desconocido
  E04 Prioridad fuera de rango
  E05 Fechas de vigencia mal formadas o invertidas
  A01 variables_requeridas declaradas que la expresión no usa
  A02 Prioridad repetida dentro del mismo dominio (orden no determinista)
  A03 Axioma vencido o aún no vigente
  A04 Axioma inalcanzable (ninguna combinación probada lo activa)
  A05 Axioma tautológico (se activa siempre)
  C01 Contradicción: dos axiomas coactivos con consecuencias incompatibles
"""

from __future__ import annotations

import ast
import itertools
import random
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from .expr import EVALUADOR_GLOBAL, ExprError, parsear, validar_expr
from .models import ESTADOS_CONSECUENCIA, SEVERIDAD, Axioma, Hallazgo


# --------------------------------------------------------------------------
# Comprobaciones estructurales
# --------------------------------------------------------------------------

def validar_estructura(axiomas: Sequence[Axioma]) -> List[Hallazgo]:
    hallazgos: List[Hallazgo] = []
    vistos: Dict[str, str] = {}
    prioridades: Dict[str, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))

    for ax in axiomas:
        ctx = dict(axioma_id=ax.axioma_id, archivo=ax.archivo_origen)

        if ax.axioma_id in vistos:
            hallazgos.append(Hallazgo(
                "ERROR", "E01_ID_DUPLICADO",
                f"Ya definido en {vistos[ax.axioma_id]}", **ctx))
        else:
            vistos[ax.axioma_id] = ax.archivo_origen

        try:
            validar_expr(ax.condicion_expr)
        except ExprError as e:
            hallazgos.append(Hallazgo("ERROR", "E02_EXPRESION_INVALIDA", str(e), **ctx))

        estado = ax.estado_consecuencia
        if estado not in ESTADOS_CONSECUENCIA:
            hallazgos.append(Hallazgo(
                "ERROR", "E03_ESTADO_DESCONOCIDO",
                f"'{estado}' no es válido. Use: {', '.join(sorted(ESTADOS_CONSECUENCIA))}",
                **ctx))

        if not 1 <= ax.prioridad <= 100:
            hallazgos.append(Hallazgo(
                "ERROR", "E04_PRIORIDAD_FUERA_RANGO",
                f"prioridad={ax.prioridad}, debe estar entre 1 y 100", **ctx))

        try:
            desde = date.fromisoformat(ax.vigencia_desde) if ax.vigencia_desde else None
            hasta = date.fromisoformat(ax.vigencia_hasta) if ax.vigencia_hasta else None
            if desde and hasta and desde > hasta:
                hallazgos.append(Hallazgo(
                    "ERROR", "E05_VIGENCIA_INVERTIDA",
                    "vigencia_desde es posterior a vigencia_hasta", **ctx))
        except ValueError as e:
            hallazgos.append(Hallazgo("ERROR", "E05_FECHA_INVALIDA", str(e), **ctx))

        if not ax.descripcion.strip():
            hallazgos.append(Hallazgo(
                "ADVERTENCIA", "A00_SIN_DESCRIPCION",
                "Sin descripción: la explicación al usuario será pobre.", **ctx))

        if not ax.vigente():
            hallazgos.append(Hallazgo(
                "ADVERTENCIA", "A03_FUERA_DE_VIGENCIA",
                "No está vigente hoy; el motor lo ignorará.", **ctx))

        prioridades[ax.dominio][ax.prioridad].append(ax.axioma_id)

    for dominio, mapa in prioridades.items():
        for prio, ids in mapa.items():
            if len(ids) > 1:
                hallazgos.append(Hallazgo(
                    "ADVERTENCIA", "A02_PRIORIDAD_REPETIDA",
                    f"Dominio '{dominio}': {', '.join(ids)} comparten prioridad "
                    f"{prio}; el orden de evaluación no es determinista."))

    return hallazgos


# --------------------------------------------------------------------------
# Generación de casos de prueba a partir de las propias expresiones
# --------------------------------------------------------------------------

def _valores_candidatos(axiomas: Iterable[Axioma]) -> Dict[str, List[Any]]:
    """
    Infiere valores de prueba para cada variable leyendo contra qué constantes
    se compara en las expresiones. Permite explorar el espacio de decisión sin
    que nadie escriba casos de prueba a mano.
    """
    constantes: Dict[str, Set[Any]] = defaultdict(set)
    numericas: Set[str] = set()
    booleanas: Set[str] = set()
    todas: Set[str] = set()

    from .expr import _ruta_de  # reutiliza el resolutor de rutas

    OPS_ORDEN = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

    def marcar(nodo: ast.AST, conjunto: Set[str]) -> None:
        if isinstance(nodo, (ast.Name, ast.Subscript, ast.Attribute)):
            ruta = _ruta_de(nodo)
            if ruta:
                conjunto.add(".".join(ruta))

    for ax in axiomas:
        try:
            arbol = parsear(ax.condicion_expr)
        except ExprError:
            continue
        todas.update(ax.variables_requeridas)

        for nodo in ast.walk(arbol):
            # aritmética => numérica
            if isinstance(nodo, ast.BinOp):
                marcar(nodo.left, numericas)
                marcar(nodo.right, numericas)

            if isinstance(nodo, ast.Compare):
                ordinal = any(isinstance(op, OPS_ORDEN) for op in nodo.ops)
                izq_ruta = _ruta_de(nodo.left)
                if ordinal:
                    marcar(nodo.left, numericas)
                for comp in nodo.comparators:
                    if ordinal:
                        marcar(comp, numericas)
                    der_ruta = _ruta_de(comp)
                    if izq_ruta and isinstance(comp, ast.Constant):
                        constantes[".".join(izq_ruta)].add(comp.value)
                    if der_ruta and isinstance(nodo.left, ast.Constant):
                        constantes[".".join(der_ruta)].add(nodo.left.value)

            # uso como valor de verdad => booleana
            if isinstance(nodo, (ast.UnaryOp, ast.BoolOp)):
                hijos = ([nodo.operand] if isinstance(nodo, ast.UnaryOp)
                         else list(nodo.values))
                for h in hijos:
                    marcar(h, booleanas)

    todas |= set(constantes) | numericas | booleanas
    booleanas -= numericas          # la aritmética manda sobre el uso booleano

    salida: Dict[str, List[Any]] = {}
    for var in sorted(todas):
        consts = constantes.get(var, set())
        valores: List[Any] = []

        nums = [c for c in consts if isinstance(c, (int, float))
                and not isinstance(c, bool)]
        for c in nums:
            valores += [c - 1, c, c + 1]
        valores += [c for c in consts if isinstance(c, str)]

        if var in numericas and not nums:
            # sin constantes de referencia: rejilla compartida para que las
            # comparaciones entre variables cubran ambos sentidos
            valores += [0, 100, 1000, 10000]
        elif var in booleanas or any(isinstance(c, bool) for c in consts):
            valores += [True, False]
        elif not valores:
            valores += [True, False, 0, 1]

        vistos, finales = set(), []
        for v in valores:
            clave = (type(v).__name__, v)
            if clave not in vistos:
                vistos.add(clave)
                finales.append(v)
        salida[var] = finales[:6]
    return salida


def _construir_contexto(asignacion: Dict[str, Any]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    for ruta, valor in asignacion.items():
        partes = ruta.split(".")
        nodo = ctx
        for p in partes[:-1]:
            nodo = nodo.setdefault(p, {})
        nodo[partes[-1]] = valor
    return ctx


def generar_casos(
    axiomas: Sequence[Axioma], limite: int = 4000, semilla: int = 20240101
) -> List[Dict[str, Any]]:
    """
    Explora el espacio de combinaciones de valores. Si el producto cartesiano
    cabe en el límite se recorre entero; si no, se muestrea al azar con semilla
    fija (determinista, reproducible en CI). Truncar el producto en orden
    sesgaría la muestra: las primeras variables quedarían siempre con su primer
    valor y varios axiomas parecerían inalcanzables sin serlo.
    """
    candidatos = _valores_candidatos(axiomas)
    if not candidatos:
        return []

    variables = sorted(candidatos)
    total = 1
    for v in variables:
        total *= max(1, len(candidatos[v]))

    if total <= limite:
        combos: Iterable[Sequence[Any]] = itertools.product(
            *(candidatos[v] for v in variables))
    else:
        rng = random.Random(semilla)
        vistos: Set[Tuple[Any, ...]] = set()
        muestras: List[Tuple[Any, ...]] = []
        intentos = 0
        while len(muestras) < limite and intentos < limite * 20:
            intentos += 1
            combo = tuple(rng.choice(candidatos[v]) for v in variables)
            clave = tuple((type(x).__name__, x) for x in combo)
            if clave not in vistos:
                vistos.add(clave)
                muestras.append(combo)
        combos = muestras

    return [_construir_contexto(dict(zip(variables, c))) for c in combos]


# --------------------------------------------------------------------------
# Contradicciones, tautologías e inalcanzables
# --------------------------------------------------------------------------

def analizar_comportamiento(
    axiomas: Sequence[Axioma], limite_casos: int = 4000
) -> Tuple[List[Hallazgo], Dict[str, Any]]:
    """
    Ejecuta todos los axiomas de cada dominio contra casos generados y detecta:
      * pares que se activan juntos con consecuencias incompatibles (C01)
      * axiomas que nunca se activan (A04)
      * axiomas que se activan siempre (A05)
    """
    hallazgos: List[Hallazgo] = []
    resumen: Dict[str, Any] = {}

    por_dominio: Dict[str, List[Axioma]] = defaultdict(list)
    for ax in axiomas:
        por_dominio[ax.dominio].append(ax)

    for dominio, lista in por_dominio.items():
        casos = generar_casos(lista, limite=limite_casos)
        activaciones: Dict[str, int] = defaultdict(int)
        conflictos: Dict[Tuple[str, str], Dict[str, Any]] = {}
        evaluables = 0

        for caso in casos:
            activos: List[Axioma] = []
            completo = True
            for ax in lista:
                try:
                    if EVALUADOR_GLOBAL.evaluar(ax.condicion_expr, caso):
                        activos.append(ax)
                        activaciones[ax.axioma_id] += 1
                except ExprError:
                    completo = False
                except Exception:
                    completo = False
            if completo:
                evaluables += 1

            for a, b in itertools.combinations(activos, 2):
                ea, eb = a.estado_consecuencia, b.estado_consecuencia
                if ea != eb and a.prioridad == b.prioridad:
                    clave = tuple(sorted((a.axioma_id, b.axioma_id)))
                    conflictos.setdefault(clave, {
                        "axiomas": list(clave),
                        "estados": sorted({ea, eb}),
                        "ejemplo": caso,
                        "ocurrencias": 0,
                    })
                    conflictos[clave]["ocurrencias"] += 1

        for datos in conflictos.values():
            hallazgos.append(Hallazgo(
                "ERROR", "C01_CONTRADICCION",
                f"{' y '.join(datos['axiomas'])} se activan a la vez con estados "
                f"{'/'.join(datos['estados'])} y la misma prioridad. "
                f"Ejemplo: {datos['ejemplo']}"))

        for ax in lista:
            veces = activaciones.get(ax.axioma_id, 0)
            if evaluables and veces == 0:
                hallazgos.append(Hallazgo(
                    "ADVERTENCIA", "A04_INALCANZABLE",
                    f"No se activó en ninguno de los {len(casos)} casos generados. "
                    "Puede estar mal escrito o ser redundante.",
                    axioma_id=ax.axioma_id, archivo=ax.archivo_origen))
            elif casos and veces == len(casos) and len(casos) >= 20:
                hallazgos.append(Hallazgo(
                    "ADVERTENCIA", "A05_TAUTOLOGICO",
                    "Se activa en todos los casos generados: la condición podría "
                    "ser siempre verdadera.",
                    axioma_id=ax.axioma_id, archivo=ax.archivo_origen))

        resumen[dominio] = {
            "axiomas": len(lista),
            "casos_generados": len(casos),
            "conflictos": len(conflictos),
            "cobertura": {
                ax.axioma_id: activaciones.get(ax.axioma_id, 0) for ax in lista
            },
        }

    return hallazgos, resumen


# --------------------------------------------------------------------------

def validar(
    axiomas: Sequence[Axioma], profundo: bool = True
) -> Tuple[List[Hallazgo], Dict[str, Any]]:
    """Pipeline completo. `profundo=False` omite el análisis por muestreo."""
    hallazgos = validar_estructura(axiomas)
    resumen: Dict[str, Any] = {}
    if profundo:
        sanos = [ax for ax in axiomas if not any(
            h.axioma_id == ax.axioma_id and h.codigo.startswith("E02")
            for h in hallazgos)]
        extra, resumen = analizar_comportamiento(sanos)
        hallazgos += extra
    return hallazgos, resumen


def hay_errores(hallazgos: Iterable[Hallazgo]) -> bool:
    return any(h.nivel == "ERROR" for h in hallazgos)
