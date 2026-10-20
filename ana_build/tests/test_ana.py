"""
Pruebas del núcleo ANA. Corren sin Neo4j (repositorio de archivo),
así que sirven tal cual en CI.

    python -m pytest tests -q
    python tests/test_ana.py        # sin pytest instalado
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ana.engine import MotorANA
from ana.expr import EVALUADOR_GLOBAL, ExprError, extraer_variables
from ana.loader import cargar_directorio
from ana.models import Axioma, Estado
from ana.repository import RepositorioArchivo
from ana.validator import hay_errores, validar

RAIZ = Path(__file__).resolve().parents[1]
DIR_AXIOMAS = RAIZ / "axiomas"


# ------------------------------------------------------------- extracción

def test_extrae_variables_de_subindices():
    expr = "mercancia['es_peligrosa'] and not transporte['hazmat']"
    assert extraer_variables(expr) == ["mercancia.es_peligrosa", "transporte.hazmat"]


def test_extrae_variables_con_aritmetica():
    expr = "(transporte['peso_vacio_ton'] + mercancia['peso_kg'] / 1000.0) > ruta['max']"
    assert extraer_variables(expr) == [
        "mercancia.peso_kg", "ruta.max", "transporte.peso_vacio_ton"]


def test_notacion_de_punto_equivale_a_subindice():
    assert extraer_variables("mercancia.peso_kg > 10") == ["mercancia.peso_kg"]


def test_descarta_contenedor_si_hay_ruta_mas_especifica():
    expr = "mercancia['peso_kg'] > 0 and len(mercancia) > 1"
    assert "mercancia" not in extraer_variables(expr)


# ------------------------------------------------------------- seguridad

def test_rechaza_ejecucion_arbitraria():
    for malicioso in [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "(1).__class__.__mro__",
        "[x for x in range(10)]",
    ]:
        try:
            extraer_variables(malicioso)
        except ExprError:
            continue
        raise AssertionError(f"No se bloqueó: {malicioso}")


def test_rechaza_sintaxis_invalida():
    try:
        extraer_variables("a >>> b")
    except ExprError:
        return
    raise AssertionError("Debió fallar")


# ------------------------------------------------------------- evaluador

def test_evaluador_basico():
    datos = {"m": {"peso": 120}, "t": {"cap": 100}}
    assert EVALUADOR_GLOBAL.evaluar("m['peso'] > t['cap']", datos) is True
    assert EVALUADOR_GLOBAL.evaluar("m['peso'] <= t['cap']", datos) is False
    assert EVALUADOR_GLOBAL.evaluar("m['peso'] / 2 == 60", datos) is True


def test_comparacion_encadenada():
    assert EVALUADOR_GLOBAL.evaluar("0 < x < 10", {"x": 5}) is True
    assert EVALUADOR_GLOBAL.evaluar("0 < x < 10", {"x": 50}) is False


# ------------------------------------------------------------- carga

def test_carga_directorio_sin_errores():
    axiomas, hallazgos = cargar_directorio(DIR_AXIOMAS)
    assert axiomas, "no se cargó ningún axioma"
    assert not hay_errores(hallazgos)
    # el dominio se deduce de la carpeta
    assert {a.dominio for a in axiomas} <= {"transporte", "aduanas", "riesgos", "documentacion"}
    # las variables se dedujeron solas
    assert all(a.variables_requeridas for a in axiomas)


def test_hash_estable_e_sensible():
    ax = Axioma(axioma_id="A", dominio="d", condicion_expr="x > 1",
                consecuencia={"estado": "RECHAZADO"}).normalizar()
    h1 = ax.hash
    assert ax.calcular_hash() == h1
    ax.prioridad = 99
    assert ax.calcular_hash() != h1


def test_base_actual_valida():
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    hallazgos, _ = validar(axiomas, profundo=True)
    errores = [h for h in hallazgos if h.nivel == "ERROR"]
    assert not errores, "\n".join(str(h) for h in errores)


# ------------------------------------------------------------- motor

def _motor(tmp="build/test_axiomas.json"):
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    repo = RepositorioArchivo(RAIZ / tmp)
    repo.sincronizar(axiomas)
    return MotorANA(repo, autorecarga=False)


CASO_OK = {
    "mercancia": {"es_peligrosa": False, "peso_kg": 1500},
    "transporte": {"hazmat": False, "capacidad_max_kg": 3000,
                   "autonomia_km": 800, "peso_vacio_ton": 5.0,
                   "poliza_vigente": True},
    "ruta": {"distancia_km": 400, "peso_max_permitido_ton": 20.0},
}


def test_aprobado():
    r = _motor().evaluar("transporte", CASO_OK)
    assert r.estado == Estado.APROBADO.value
    assert "AX-OK-001" in r.axiomas_activados
    assert r.explicacion


def test_rechazo_hazmat_gana_por_severidad():
    datos = {**CASO_OK, "mercancia": {"es_peligrosa": True, "peso_kg": 1500}}
    r = _motor().evaluar("transporte", datos)
    assert r.estado == Estado.RECHAZADO.value
    assert r.decision["axioma_decisorio"] == "AX-HAZ-001"


def test_revision_humana_gana_a_aprobado():
    datos = {**CASO_OK, "ruta": {"distancia_km": 760, "peso_max_permitido_ton": 20.0}}
    r = _motor().evaluar("transporte", datos)
    assert r.estado == Estado.REVISION_HUMANA.value
    assert "AX-OK-001" in r.axiomas_activados  # se activó, pero no decide


def test_informacion_insuficiente():
    r = _motor().evaluar("transporte", {"mercancia": {"es_peligrosa": False}})
    assert r.estado == Estado.INFORMACION_INSUFICIENTE.value
    assert r.variables_faltantes
    assert r.decision is None


def test_un_axioma_sin_datos_no_tumba_la_evaluacion():
    """
    Regresión del motor anterior: la falta de una variable de un axioma
    irrelevante abortaba la petición completa. Aquí sobra información para
    decidir el rechazo por HAZMAT aunque falten datos de ruta.
    """
    datos = {
        "mercancia": {"es_peligrosa": True, "peso_kg": 100},
        "transporte": {"hazmat": False},
    }
    r = _motor().evaluar("transporte", datos)
    assert r.estado == Estado.RECHAZADO.value
    assert r.variables_faltantes          # se reportan, pero no bloquean


def test_trazabilidad_completa():
    r = _motor().evaluar("transporte", CASO_OK)
    evaluados = {t.axioma_id for t in r.trace}
    assert evaluados == set(r.axiomas_evaluados)
    activado = next(t for t in r.trace if t.resultado == "ACTIVADO")
    assert activado.detalle["evidencia"]
    assert activado.detalle["condicion"]
    assert r.version_base_conocimiento


def test_dominio_inexistente_se_abstiene():
    r = _motor().evaluar("dominio_que_no_existe", CASO_OK)
    assert r.estado == Estado.SIN_AXIOMAS.value
    assert r.decision is None


# ------------------------------------------------------------- repositorio

def test_sincronizacion_incremental():
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    repo = RepositorioArchivo(RAIZ / "build/test_incremental.json")
    r1 = repo.sincronizar(axiomas)
    assert len(r1["nuevos"]) == len(axiomas)

    r2 = repo.sincronizar(axiomas)
    assert r2["sin_cambio"] == len(axiomas)
    assert not r2["nuevos"] and not r2["actualizados"]
    assert r1["version"] == r2["version"]

    axiomas[0].prioridad = 7
    axiomas[0].normalizar()
    r3 = repo.sincronizar(axiomas)
    assert r3["actualizados"] == [axiomas[0].axioma_id]
    assert r3["version"] != r2["version"]


def test_desactivacion_no_destructiva():
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    repo = RepositorioArchivo(RAIZ / "build/test_desactivar.json")
    repo.sincronizar(axiomas)
    repo.sincronizar(axiomas[:-1])
    activos = {a.axioma_id for a in repo.listar()}
    todos = {a.axioma_id for a in repo.listar(solo_activos=False)}
    assert axiomas[-1].axioma_id not in activos
    assert axiomas[-1].axioma_id in todos   # sigue existiendo para auditoría


def test_recarga_automatica():
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    repo = RepositorioArchivo(RAIZ / "build/test_recarga.json")
    repo.sincronizar(axiomas)
    motor = MotorANA(repo, ttl_version_s=0.0)
    v1 = motor.version

    nuevo = Axioma(
        axioma_id="AX-TMP-999", dominio="transporte",
        descripcion="temporal", condicion_expr="mercancia['peso_kg'] > 999999",
        consecuencia={"estado": "RECHAZADO", "motivo": "temporal"},
        prioridad=5).normalizar()
    repo.sincronizar(list(axiomas) + [nuevo])

    motor.evaluar("transporte", CASO_OK)     # dispara la comprobación
    assert motor.version != v1
    assert "AX-TMP-999" in {a.axioma_id for a in motor.axiomas_de("transporte")}


# ------------------------------------------------------------- validador

def test_detecta_contradiccion():
    a = Axioma(axioma_id="A", dominio="t", descripcion="a",
               condicion_expr="m['peligrosa']",
               consecuencia={"estado": "RECHAZADO"}, prioridad=50).normalizar()
    b = Axioma(axioma_id="B", dominio="t", descripcion="b",
               condicion_expr="m['peligrosa'] and m['peso'] < 500",
               consecuencia={"estado": "APROBADO"}, prioridad=50).normalizar()
    hallazgos, _ = validar([a, b], profundo=True)
    assert any(h.codigo.startswith("C01") for h in hallazgos)


def test_detecta_id_duplicado():
    a = Axioma(axioma_id="X", dominio="t", descripcion="a", condicion_expr="p > 1",
               consecuencia={"estado": "RECHAZADO"}).normalizar()
    b = Axioma(axioma_id="X", dominio="t", descripcion="b", condicion_expr="p > 2",
               consecuencia={"estado": "RECHAZADO"}).normalizar()
    hallazgos, _ = validar([a, b], profundo=False)
    assert any(h.codigo.startswith("E01") for h in hallazgos)


def test_detecta_estado_invalido():
    a = Axioma(axioma_id="Y", dominio="t", descripcion="a", condicion_expr="p > 1",
               consecuencia={"estado": "QUIZAS"}).normalizar()
    hallazgos, _ = validar([a], profundo=False)
    assert any(h.codigo.startswith("E03") for h in hallazgos)


def test_axioma_vencido_no_se_aplica():
    a = Axioma(axioma_id="Z", dominio="t", descripcion="a", condicion_expr="p > 1",
               consecuencia={"estado": "RECHAZADO"},
               vigencia_hasta="2020-01-01").normalizar()
    assert not a.vigente()
    hallazgos, _ = validar([a], profundo=False)
    assert any(h.codigo.startswith("A03") for h in hallazgos)


# -------------------------------------------------------------------------

if __name__ == "__main__":
    fallos = 0
    for nombre, fn in sorted(globals().items()):
        if nombre.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {nombre}")
            except Exception as e:
                fallos += 1
                print(f"  FALLO {nombre}: {type(e).__name__}: {e}")
    print(f"\n{'Todo correcto.' if not fallos else str(fallos) + ' pruebas fallidas.'}")
    sys.exit(1 if fallos else 0)
