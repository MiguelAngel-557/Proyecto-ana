"""
ana.api — Capa REST.

Al arrancar sincroniza sola el directorio de axiomas, así que basta con
`uvicorn ana.api:app --reload` para tener el sistema en marcha.

Endpoints:
  POST /evaluar                 evaluación con traza y explicación
  GET  /axiomas                 listado (filtrable por dominio)
  POST /axiomas/sincronizar     recarga desde disco sin reiniciar
  GET  /axiomas/validar         informe de validación en vivo
  GET  /axiomas/conflictos      contradicciones detectadas
  GET  /auditoria               últimas decisiones registradas
  GET  /salud                   estado del repositorio y versión de la base
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .engine import MotorANA
from .expr import ExprError, validar_expr
from .loader import cargar_directorio
from .models import Axioma
from .repository import obtener_repositorio
from .revision_web import render_detalle, render_lista
from .validator import hay_errores, validar

log = logging.getLogger("ana.api")

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


_cargar_env()

DIR_AXIOMAS = os.getenv("ANA_DIR_AXIOMAS", "axiomas")
RUTA_AUDITORIA = Path(os.getenv("ANA_AUDITORIA", "build/auditoria.jsonl"))

PAISES_DISPONIBLES = {"MX": "México", "US": "Estados Unidos",
                      "CN": "China", "RU": "Rusia"}
estado: Dict[str, Any] = {"motor": None, "repo": None}


def _autosync() -> Dict[str, Any]:
    axiomas, hallazgos_carga = cargar_directorio(DIR_AXIOMAS)
    hallazgos_val, _ = validar(axiomas, profundo=False)
    todos = hallazgos_carga + hallazgos_val
    if hay_errores(todos):
        errores = [str(h) for h in todos if h.nivel == "ERROR"]
        log.error("Axiomas con errores, no se sincroniza:\n%s", "\n".join(errores))
        return {"sincronizado": False, "errores": errores}
    r = estado["repo"].sincronizar(axiomas)
    return {"sincronizado": True, **r}


@asynccontextmanager
async def lifespan(app: FastAPI):
    estado["repo"] = obtener_repositorio()
    resultado = _autosync()
    log.info("Autosincronización: %s", resultado)
    estado["motor"] = MotorANA(estado["repo"])
    yield
    estado["repo"].cerrar()


app = FastAPI(
    title="ANA Core",
    description="Motor axiomático de decisiones explicables para logística.",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- esquemas

class PeticionEvaluacion(BaseModel):
    dominio: str = Field(examples=["transporte"])
    datos: Dict[str, Any] = Field(examples=[{
        "mercancia": {"es_peligrosa": True, "peso_kg": 1200},
        "transporte": {"hazmat": False, "capacidad_max_kg": 5000,
                       "autonomia_km": 800, "peso_vacio_ton": 5.0},
        "ruta": {"distancia_km": 400, "peso_max_permitido_ton": 20.0},
    }])


# ---------------------------------------------------------------- auditoría

def _auditar(peticion: PeticionEvaluacion, resultado) -> None:
    RUTA_AUDITORIA.parent.mkdir(parents=True, exist_ok=True)
    registro = {
        "momento": datetime.now(timezone.utc).isoformat(),
        "dominio": peticion.dominio,
        "entrada": peticion.datos,
        "estado": resultado.estado,
        "decision": resultado.decision,
        "axiomas_activados": resultado.axiomas_activados,
        "version_base": resultado.version_base_conocimiento,
        "explicacion": resultado.explicacion,
        "trace": [t.__dict__ for t in resultado.trace],
    }
    # JSONL: escritura O(1), no hay que releer todo el archivo en cada decisión
    with RUTA_AUDITORIA.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- endpoints

@app.get("/salud", tags=["General"])
def salud():
    motor: MotorANA = estado["motor"]
    return {
        "estado": "ok",
        "repositorio": estado["repo"].nombre,
        "version_base_conocimiento": motor.version,
        "directorio_axiomas": DIR_AXIOMAS,
    }


@app.post("/evaluar", tags=["Inferencia"])
def evaluar(peticion: PeticionEvaluacion):
    motor: MotorANA = estado["motor"]
    try:
        resultado = motor.evaluar(peticion.dominio, peticion.datos)
    except Exception as e:
        log.exception("Fallo de evaluación")
        raise HTTPException(500, f"Error durante la inferencia: {e}")
    _auditar(peticion, resultado)
    return resultado.to_dict()


@app.get("/axiomas", tags=["Axiomas"])
def listar_axiomas(dominio: Optional[str] = None, incluir_inactivos: bool = False):
    axiomas = estado["repo"].listar(dominio=dominio,
                                    solo_activos=not incluir_inactivos)
    return {"total": len(axiomas), "axiomas": [a.to_dict() for a in axiomas]}


@app.post("/axiomas/sincronizar", tags=["Axiomas"])
def sincronizar():
    """Relee el directorio de axiomas y refresca el motor sin reiniciar."""
    resultado = _autosync()
    if not resultado["sincronizado"]:
        raise HTTPException(422, {"mensaje": "Axiomas con errores",
                                  "errores": resultado["errores"]})
    version = estado["motor"].recargar()
    return {**resultado, "version_activa": version}


@app.get("/axiomas/validar", tags=["Axiomas"])
def validar_axiomas(profundo: bool = Query(True)):
    axiomas, hallazgos_carga = cargar_directorio(DIR_AXIOMAS)
    hallazgos_val, resumen = validar(axiomas, profundo=profundo)
    todos = hallazgos_carga + hallazgos_val
    return {
        "axiomas": len(axiomas),
        "errores": [h.__dict__ for h in todos if h.nivel == "ERROR"],
        "advertencias": [h.__dict__ for h in todos if h.nivel == "ADVERTENCIA"],
        "cobertura": resumen,
    }


@app.get("/axiomas/conflictos", tags=["Axiomas"])
def conflictos():
    axiomas, _ = cargar_directorio(DIR_AXIOMAS)
    hallazgos, resumen = validar(axiomas, profundo=True)
    return {
        "contradicciones": [h.__dict__ for h in hallazgos
                            if h.codigo.startswith("C01")],
        "resumen": resumen,
    }

@app.get("/auditoria", tags=["Auditoría"])
def auditoria(limite: int = Query(50, le=1000)):
    if not RUTA_AUDITORIA.exists():
        return {"total": 0, "registros": []}
    lineas = RUTA_AUDITORIA.read_text(encoding="utf-8").strip().splitlines()
    ultimos = [json.loads(l) for l in lineas[-limite:]]
    return {"total": len(lineas), "registros": list(reversed(ultimos))}


# ---------------------------------------------------------------- carga manual (por país)

_FORM_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Nuevo axioma — ANA Core</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }}
    label {{ display: block; margin-top: 14px; font-weight: 600; }}
    input, select, textarea {{ width: 100%; padding: 8px; font-size: 14px; box-sizing: border-box; }}
    textarea {{ font-family: monospace; height: 70px; }}
    .paises label {{ display: inline-block; font-weight: 400; margin-right: 14px; }}
    .paises input {{ width: auto; margin-right: 4px; }}
    button {{ margin-top: 20px; padding: 10px 20px; font-size: 15px; cursor: pointer; }}
    .msg {{ padding: 10px; margin-top: 16px; border-radius: 4px; }}
    .ok {{ background: #d4edda; color: #155724; }}
    .error {{ background: #f8d7da; color: #721c24; }}
  </style>
</head>
<body>
  <h2>Nuevo axioma (carga directa a la base)</h2>
  {mensaje}
  <form method="post" action="/axiomas/nuevo">
    <label>ID del axioma (único, ej. AX-MX-001)</label>
    <input name="axioma_id" required>

    <label>Dominio</label>
    <input name="dominio" required placeholder="transporte, aduanas, riesgos, documentacion...">

    <label>Descripción</label>
    <input name="descripcion" required>

    <label>Condición (expresión Python, ej. mercancia['peso_kg'] > 5000)</label>
    <textarea name="condicion_expr" required></textarea>

    <label>Estado resultante</label>
    <select name="estado_resultante">
      <option value="RECHAZADO">RECHAZADO</option>
      <option value="REVISION_HUMANA">REVISION_HUMANA</option>
      <option value="APROBADO">APROBADO</option>
    </select>

    <label>Motivo (mensaje explicativo)</label>
    <input name="motivo" required>

    <label>Prioridad (0-100, más alto se evalúa primero en caso de empate)</label>
    <input name="prioridad" type="number" value="50" min="0" max="100">

    <label>Países donde aplica</label>
    <div class="paises">
      <label><input type="checkbox" name="paises" value="*"> Todos los países</label>
      {checkboxes_pais}
    </div>

    <button type="submit">Crear axioma</button>
  </form>
</body>
</html>
"""


def _checkboxes_paises() -> str:
    return "".join(
        f'<label><input type="checkbox" name="paises" value="{cod}"> {nombre}</label>'
        for cod, nombre in PAISES_DISPONIBLES.items()
    )


@app.get("/axiomas/nuevo", tags=["Axiomas"], response_class=HTMLResponse)
def formulario_nuevo_axioma():
    return _FORM_HTML.format(mensaje="", checkboxes_pais=_checkboxes_paises())


@app.post("/axiomas/nuevo", tags=["Axiomas"], response_class=HTMLResponse)
def crear_axioma_manual(
    axioma_id: str = Form(...),
    dominio: str = Form(...),
    descripcion: str = Form(...),
    condicion_expr: str = Form(...),
    estado_resultante: str = Form(...),
    motivo: str = Form(...),
    prioridad: int = Form(50),
    paises: List[str] = Form([]),
):
    checkboxes = _checkboxes_paises()
    repo = estado["repo"]

    if not hasattr(repo, "crear_manual"):
        mensaje = ('<div class="msg error">El repositorio activo '
                   f'({repo.nombre}) no soporta carga manual; usa Postgres.</div>')
        return _FORM_HTML.format(mensaje=mensaje, checkboxes_pais=checkboxes)

    try:
        validar_expr(condicion_expr)
    except ExprError as e:
        mensaje = f'<div class="msg error">Condición inválida: {e}</div>'
        return _FORM_HTML.format(mensaje=mensaje, checkboxes_pais=checkboxes)

    if not paises:
        paises = ["*"]

    ax = Axioma(
        axioma_id=axioma_id.strip(),
        dominio=dominio.strip(),
        condicion_expr=condicion_expr.strip(),
        consecuencia={"estado": estado_resultante, "motivo": motivo.strip()},
        descripcion=descripcion.strip(),
        prioridad=prioridad,
        paises=paises,
        archivo_origen="manual",
    ).normalizar()

    try:
        repo.crear_manual(ax)
    except ValueError as e:
        mensaje = f'<div class="msg error">{e}</div>'
        return _FORM_HTML.format(mensaje=mensaje, checkboxes_pais=checkboxes)

    estado["motor"].recargar()
    mensaje = (f'<div class="msg ok">Axioma <b>{ax.axioma_id}</b> creado y activo '
               f'(países: {", ".join(paises)}).</div>')
    return _FORM_HTML.format(mensaje=mensaje, checkboxes_pais=checkboxes)



# ---------------------------------------------------------------- revisión de propuestas

class RevisionAprobar(BaseModel):
    revisado_por: str = Field(min_length=1, examples=["miguel"])


class RevisionRechazar(BaseModel):
    revisado_por: str = Field(min_length=1, examples=["miguel"])
    motivo: str = Field(min_length=3, examples=["La regla no aplica a este dominio."])


def _repo_con_propuestas():
    repo = estado["repo"]
    if not hasattr(repo, "listar_propuestas"):
        raise HTTPException(
            501, f"El repositorio activo ({repo.nombre}) no soporta propuestas; usa Postgres.")
    return repo


@app.get("/axiomas/propuestas", tags=["Revisión"])
def listar_propuestas():
    """Candidatos pendientes de revisión humana (nunca los ve el motor)."""
    props = _repo_con_propuestas().listar_propuestas()
    return {
        "total": len(props),
        "propuestas": [
            {
                "axioma_id": a.axioma_id, "dominio": a.dominio, "paises": a.paises,
                "descripcion": a.descripcion, "prioridad": a.prioridad,
                "fuente_documento": a.fuente_documento,
                "fuente_referencia": a.fuente_referencia,
                "origen": a.origen, "generado_por": a.generado_por,
            }
            for a in props
        ],
    }


@app.get("/axiomas/propuestas/{axioma_id}", tags=["Revisión"])
def detalle_propuesta(axioma_id: str):
    """Todo lo que un humano necesita para decidir: regla, fuente y estado."""
    ax = _repo_con_propuestas().obtener(axioma_id)
    if ax is None:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    return ax.to_dict()


@app.post("/axiomas/propuestas/{axioma_id}/aprobar", tags=["Revisión"])
def aprobar_propuesta(axioma_id: str, cuerpo: RevisionAprobar):
    repo = _repo_con_propuestas()
    try:
        ax = repo.aprobar(axioma_id, cuerpo.revisado_por.strip())
    except KeyError:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    except ExprError as e:
        raise HTTPException(422, f"La condición ya no es válida: {e}")
    except ValueError as e:
        raise HTTPException(409, str(e))
    version = estado["motor"].recargar()   # el motor toma el axioma de inmediato
    return {"axioma_id": ax.axioma_id, "estado_revision": ax.estado_revision,
            "activo": ax.activo, "version_activa": version}


@app.post("/axiomas/propuestas/{axioma_id}/rechazar", tags=["Revisión"])
def rechazar_propuesta(axioma_id: str, cuerpo: RevisionRechazar):
    repo = _repo_con_propuestas()
    try:
        ax = repo.rechazar(axioma_id, cuerpo.revisado_por.strip(), cuerpo.motivo.strip())
    except KeyError:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"axioma_id": ax.axioma_id, "estado_revision": ax.estado_revision,
            "activo": ax.activo}



# ---------------------------------------------------------------- pantalla de revisión (HTML)

@app.get("/revision", tags=["Pantalla de revisión"], response_class=HTMLResponse)
def pantalla_revision(aprobada: Optional[str] = None, rechazada: Optional[str] = None):
    """Lista de propuestas pendientes de revisión humana."""
    props = _repo_con_propuestas().listar_propuestas()
    return render_lista(props, aprobada=aprobada, rechazada=rechazada)


@app.get("/revision/{axioma_id}", tags=["Pantalla de revisión"], response_class=HTMLResponse)
def pantalla_detalle(axioma_id: str):
    ax = _repo_con_propuestas().obtener(axioma_id)
    if ax is None:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    return render_detalle(ax)


@app.post("/revision/{axioma_id}/aprobar", tags=["Pantalla de revisión"],
          response_class=HTMLResponse)
def pantalla_aprobar(axioma_id: str, revisado_por: str = Form(...)):
    repo = _repo_con_propuestas()
    nombre = revisado_por.strip()
    ax = repo.obtener(axioma_id)
    if ax is None:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    if not nombre:
        return HTMLResponse(render_detalle(ax, error="Escribe quién revisa."),
                            status_code=422)
    try:
        repo.aprobar(axioma_id, nombre)
    except ValueError as e:  # incluye ExprError y "ya fue revisada"
        return HTMLResponse(render_detalle(repo.obtener(axioma_id), error=str(e)),
                            status_code=409)
    estado["motor"].recargar()   # el motor toma la regla de inmediato
    return RedirectResponse(f"/revision?aprobada={quote(axioma_id, safe='')}",
                            status_code=303)


@app.post("/revision/{axioma_id}/rechazar", tags=["Pantalla de revisión"],
          response_class=HTMLResponse)
def pantalla_rechazar(axioma_id: str, revisado_por: str = Form(...),
                      motivo: str = Form(...)):
    repo = _repo_con_propuestas()
    nombre, razon = revisado_por.strip(), motivo.strip()
    ax = repo.obtener(axioma_id)
    if ax is None:
        raise HTTPException(404, f"No existe el axioma '{axioma_id}'.")
    if not nombre or len(razon) < 3:
        return HTMLResponse(
            render_detalle(ax, error="Indica quién revisa y un motivo (mínimo 3 caracteres)."),
            status_code=422)
    try:
        repo.rechazar(axioma_id, nombre, razon)
    except ValueError as e:
        return HTMLResponse(render_detalle(repo.obtener(axioma_id), error=str(e)),
                            status_code=409)
    return RedirectResponse(f"/revision?rechazada={quote(axioma_id, safe='')}",
                            status_code=303)