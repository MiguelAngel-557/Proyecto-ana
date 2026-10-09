"""
ana.expr — Analizador de expresiones de axiomas.

Tres funciones sobre la misma base (el AST de Python):

1. extraer_variables(expr)  -> deduce solas las `variables_requeridas`
2. validar_expr(expr)       -> rechaza sintaxis peligrosa o no permitida
3. Evaluador().evaluar(...) -> evalúa sin `eval()` y sin dependencias externas

Al compartir la misma lista blanca, es imposible que el validador acepte algo
que el evaluador no sepa ejecutar (que es el bug clásico de estos motores).
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Dict, List, Set, Tuple


class ExprError(ValueError):
    """Expresión inválida, insegura o no evaluable."""


# --------------------------------------------------------------------------
# Lista blanca
# --------------------------------------------------------------------------

NODOS_PERMITIDOS = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.Subscript, ast.Attribute,
    ast.IfExp, ast.Call, ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Slice,
)

FUNCIONES_PERMITIDAS: Dict[str, Any] = {
    "len": len, "abs": abs, "min": min, "max": max, "round": round,
    "sum": sum, "any": any, "all": all,
    "int": int, "float": float, "str": str, "bool": bool,
    "sorted": sorted, "set": set, "list": list,
}

CONSTANTES_PERMITIDAS: Dict[str, Any] = {
    "True": True, "False": False, "None": None,
}

_OPS_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_OPS_CMP = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
_OPS_UN = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}


# --------------------------------------------------------------------------
# 1. Parseo y validación estática
# --------------------------------------------------------------------------

def parsear(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise ExprError("La expresión está vacía.")
    try:
        arbol = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"Error de sintaxis en la posición {e.offset}: {e.msg}") from e
    return arbol


def validar_expr(expr: str) -> ast.Expression:
    """Parsea y verifica que sólo se usen nodos y funciones de la lista blanca."""
    arbol = parsear(expr)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, NODOS_PERMITIDOS):
            raise ExprError(
                f"Construcción no permitida en un axioma: {type(nodo).__name__}. "
                "Sólo se admiten comparaciones, operaciones aritméticas y lógicas."
            )
        if isinstance(nodo, ast.Call):
            if not isinstance(nodo.func, ast.Name):
                raise ExprError("Sólo se permiten llamadas a funciones simples.")
            if nodo.func.id not in FUNCIONES_PERMITIDAS:
                raise ExprError(
                    f"Función no permitida: '{nodo.func.id}'. "
                    f"Permitidas: {', '.join(sorted(FUNCIONES_PERMITIDAS))}"
                )
        if isinstance(nodo, ast.Attribute) and nodo.attr.startswith("_"):
            raise ExprError(f"Acceso a atributo privado no permitido: '{nodo.attr}'")
    return arbol


# --------------------------------------------------------------------------
# 2. Extracción automática de variables requeridas
# --------------------------------------------------------------------------

def _ruta_de(nodo: ast.AST) -> Tuple[str, ...] | None:
    """Reconstruye mercancia['peso_kg'] o mercancia.peso_kg -> ('mercancia','peso_kg')."""
    if isinstance(nodo, ast.Name):
        return (nodo.id,)
    if isinstance(nodo, ast.Attribute):
        base = _ruta_de(nodo.value)
        return base + (nodo.attr,) if base else None
    if isinstance(nodo, ast.Subscript):
        base = _ruta_de(nodo.value)
        if base is None:
            return None
        idx = nodo.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
            return base + (idx.value,)
        return base  # índice dinámico: sólo exigimos el contenedor
    return None


def extraer_variables(expr: str) -> List[str]:
    """
    Deduce las variables que el axioma necesita. Esto sustituye el campo
    `variables_requeridas` escrito a mano, que es la fuente #1 de errores:
    si no coincide con la expresión, el motor pide datos que no usa o revienta
    por datos que sí usaba y nadie declaró.
    """
    arbol = validar_expr(expr)
    rutas: Set[Tuple[str, ...]] = set()
    nombres_funcion = {
        n.func.id for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }

    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Subscript, ast.Attribute, ast.Name)):
            ruta = _ruta_de(nodo)
            if not ruta:
                continue
            if ruta[0] in nombres_funcion or ruta[0] in CONSTANTES_PERMITIDAS:
                continue
            rutas.add(ruta)

    # Quedarse sólo con las rutas más específicas:
    # si existe ('mercancia','peso_kg') se descarta ('mercancia',)
    finales = {
        r for r in rutas
        if not any(otra != r and otra[: len(r)] == r for otra in rutas)
    }
    return sorted(".".join(r) for r in finales)


def resolver(datos: Dict[str, Any], ruta: str) -> Tuple[bool, Any]:
    """Resuelve 'mercancia.peso_kg' dentro del diccionario de contexto."""
    actual: Any = datos
    for parte in ruta.split("."):
        if isinstance(actual, dict) and parte in actual:
            actual = actual[parte]
        elif hasattr(actual, parte):
            actual = getattr(actual, parte)
        else:
            return False, None
    return True, actual


def variables_faltantes(requeridas: List[str], datos: Dict[str, Any]) -> List[str]:
    faltan = []
    for ruta in requeridas:
        existe, valor = resolver(datos, ruta)
        if not existe or valor is None:
            faltan.append(ruta)
    return faltan


# --------------------------------------------------------------------------
# 3. Evaluador seguro
# --------------------------------------------------------------------------

class Evaluador:
    """
    Intérprete de sólo-lectura sobre el AST. No usa eval(), no permite
    asignaciones, imports, atributos privados ni funciones fuera de la lista.
    Cachea el AST compilado por expresión.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, ast.Expression] = {}

    def compilar(self, expr: str) -> ast.Expression:
        if expr not in self._cache:
            self._cache[expr] = validar_expr(expr)
        return self._cache[expr]

    def evaluar(self, expr: str, datos: Dict[str, Any]) -> Any:
        arbol = self.compilar(expr)
        return self._ev(arbol.body, datos)

    def _ev(self, nodo: ast.AST, ent: Dict[str, Any]) -> Any:
        if isinstance(nodo, ast.Constant):
            return nodo.value

        if isinstance(nodo, ast.Name):
            if nodo.id in ent:
                return ent[nodo.id]
            if nodo.id in CONSTANTES_PERMITIDAS:
                return CONSTANTES_PERMITIDAS[nodo.id]
            if nodo.id in FUNCIONES_PERMITIDAS:
                return FUNCIONES_PERMITIDAS[nodo.id]
            raise ExprError(f"Variable no definida en el contexto: '{nodo.id}'")

        if isinstance(nodo, ast.BoolOp):
            if isinstance(nodo.op, ast.And):
                resultado = True
                for v in nodo.values:               # cortocircuito real
                    resultado = self._ev(v, ent)
                    if not resultado:
                        return resultado
                return resultado
            resultado = False
            for v in nodo.values:
                resultado = self._ev(v, ent)
                if resultado:
                    return resultado
            return resultado

        if isinstance(nodo, ast.UnaryOp):
            return _OPS_UN[type(nodo.op)](self._ev(nodo.operand, ent))

        if isinstance(nodo, ast.BinOp):
            return _OPS_BIN[type(nodo.op)](
                self._ev(nodo.left, ent), self._ev(nodo.right, ent)
            )

        if isinstance(nodo, ast.Compare):
            izq = self._ev(nodo.left, ent)
            for op, comp in zip(nodo.ops, nodo.comparators):
                der = self._ev(comp, ent)
                if not _OPS_CMP[type(op)](izq, der):
                    return False
                izq = der
            return True

        if isinstance(nodo, ast.IfExp):
            return (self._ev(nodo.body, ent) if self._ev(nodo.test, ent)
                    else self._ev(nodo.orelse, ent))

        if isinstance(nodo, ast.Subscript):
            base = self._ev(nodo.value, ent)
            clave = self._ev(nodo.slice, ent)
            try:
                return base[clave]
            except (KeyError, IndexError, TypeError) as e:
                raise ExprError(f"No existe la clave {clave!r} en el contexto") from e

        if isinstance(nodo, ast.Attribute):
            base = self._ev(nodo.value, ent)
            if isinstance(base, dict):
                if nodo.attr not in base:
                    raise ExprError(f"No existe la clave '{nodo.attr}' en el contexto")
                return base[nodo.attr]
            return getattr(base, nodo.attr)

        if isinstance(nodo, ast.Call):
            fn = FUNCIONES_PERMITIDAS[nodo.func.id]  # type: ignore[union-attr]
            return fn(*[self._ev(a, ent) for a in nodo.args])

        if isinstance(nodo, ast.List):
            return [self._ev(e, ent) for e in nodo.elts]
        if isinstance(nodo, ast.Tuple):
            return tuple(self._ev(e, ent) for e in nodo.elts)
        if isinstance(nodo, ast.Set):
            return {self._ev(e, ent) for e in nodo.elts}
        if isinstance(nodo, ast.Dict):
            return {self._ev(k, ent): self._ev(v, ent)
                    for k, v in zip(nodo.keys, nodo.values)}

        raise ExprError(f"Nodo no soportado: {type(nodo).__name__}")


EVALUADOR_GLOBAL = Evaluador()
