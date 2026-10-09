"""
ana.revision_web — HTML de la pantalla de revisión humana (Fase 3).

Son funciones puras: reciben objetos Axioma y devuelven texto HTML, sin
depender de FastAPI ni de la base. Así se pueden probar solas.

SEGURIDAD: el fragmento, la URL y los textos de una propuesta vienen de
documentos externos o de una IA, es decir, son datos NO confiables. Todo
valor dinámico pasa por escape, y las URLs sólo se vuelven enlace si
empiezan con http:// o https://.
"""

from __future__ import annotations

from html import escape as _e
from typing import Iterable, Optional
from urllib.parse import quote

from .expr import ExprError, validar_expr
from .models import Axioma, EstadoRevision

_CSS = """
  body { font-family: system-ui, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 16px; color: #222; }
  h2 { margin-bottom: 4px; }
  h3 { margin: 28px 0 8px; border-bottom: 1px solid #ddd; padding-bottom: 4px; font-size: 14px; letter-spacing: .06em; color: #555; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; font-size: 14px; }
  th { width: 190px; color: #555; font-weight: 600; }
  .lista th { width: auto; background: #fafafa; }
  pre { background: #f6f8fa; padding: 10px; border-radius: 4px; white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 13px; }
  blockquote { margin: 0; padding: 10px 14px; background: #fffbea; border-left: 4px solid #e0c200; white-space: pre-wrap; font-size: 14px; }
  .badge { display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 700; }
  .PENDIENTE_REVISION { background: #fff3cd; color: #856404; }
  .APROBADO { background: #d4edda; color: #155724; }
  .RECHAZADO { background: #f8d7da; color: #721c24; }
  .msg { padding: 10px; margin: 16px 0; border-radius: 4px; }
  .ok { background: #d4edda; color: #155724; }
  .error { background: #f8d7da; color: #721c24; }
  .ok-txt { color: #155724; font-weight: 600; }
  .err-txt { color: #721c24; font-weight: 600; }
  form { margin-top: 14px; padding: 14px; border: 1px solid #ddd; border-radius: 6px; }
  label { display: block; margin-top: 8px; font-weight: 600; font-size: 14px; }
  input, textarea { width: 100%; padding: 8px; font-size: 14px; box-sizing: border-box; }
  textarea { height: 70px; }
  button { margin-top: 12px; padding: 10px 20px; font-size: 15px; cursor: pointer; border: 0; border-radius: 4px; color: #fff; }
  button.aprobar { background: #28a745; }
  button.rechazar { background: #dc3545; }
  button:disabled { background: #aaa; cursor: not-allowed; }
  a { color: #0b5ed7; }
"""

_ETIQUETA_ESTADO = {
    EstadoRevision.PENDIENTE.value: "PENDIENTE DE REVISIÓN",
    EstadoRevision.APROBADO.value: "APROBADA",
    EstadoRevision.RECHAZADO.value: "RECHAZADA",
}


def _pagina(titulo: str, cuerpo: str) -> str:
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        f"<title>{_e(titulo)} — ANA Core</title><style>{_CSS}</style></head>"
        f"<body>{cuerpo}</body></html>"
    )


def _aviso(texto: str, tipo: str) -> str:
    return f'<div class="msg {tipo}">{_e(texto)}</div>'


def _fila(etiqueta: str, valor_html: str) -> str:
    return f"<tr><th>{_e(etiqueta)}</th><td>{valor_html}</td></tr>"


def _texto(valor: Optional[str]) -> str:
    """Texto escapado, con un guion si viene vacío."""
    return _e(valor) if valor else "—"


def _enlace_o_texto(url: str) -> str:
    if not url:
        return "—"
    if url.lower().startswith(("http://", "https://")):
        u = _e(url, quote=True)
        return f'<a href="{u}" target="_blank" rel="noopener noreferrer">{u}</a>'
    return _e(url)  # cualquier otro esquema (javascript:, data:...) NO es enlace


def _validez(expr: str) -> tuple[str, bool]:
    try:
        validar_expr(expr)
        return '<span class="ok-txt">Válida: pasa validar_expr()</span>', True
    except ExprError as e:
        return f'<span class="err-txt">INVÁLIDA: {_e(str(e))}</span>', False


# ---------------------------------------------------------------- lista

def render_lista(propuestas: Iterable[Axioma], aprobada: Optional[str] = None,
                 rechazada: Optional[str] = None) -> str:
    propuestas = list(propuestas)
    avisos = ""
    if aprobada:
        avisos += _aviso(f"Axioma {aprobada} aprobado y activo.", "ok")
    if rechazada:
        avisos += _aviso(f"Axioma {rechazada} rechazado.", "ok")

    if not propuestas:
        tabla = "<p>No hay propuestas pendientes de revisión.</p>"
    else:
        filas = "".join(
            "<tr>"
            f"<td>{_e(a.axioma_id)}</td>"
            f"<td>{_e(a.dominio)}</td>"
            f"<td>{_e(', '.join(a.paises))}</td>"
            f"<td>{_texto(a.descripcion)}</td>"
            f"<td>{_texto(a.fuente_documento)}<br><small>{_texto(a.fuente_referencia)}</small></td>"
            f'<td><a href="/revision/{quote(a.axioma_id, safe="")}">Revisar</a></td>'
            "</tr>"
            for a in propuestas
        )
        tabla = (
            '<table class="lista"><tr><th>ID</th><th>Dominio</th><th>País</th>'
            "<th>Descripción</th><th>Fuente</th><th></th></tr>"
            f"{filas}</table>"
        )
    cuerpo = (
        f"<h2>Propuestas pendientes ({len(propuestas)})</h2>"
        "<p>Ninguna propuesta se usa en decisiones hasta que una persona la apruebe.</p>"
        f"{avisos}{tabla}"
    )
    return _pagina("Revisión de propuestas", cuerpo)


# ---------------------------------------------------------------- detalle

def render_detalle(ax: Axioma, error: Optional[str] = None) -> str:
    validez_html, es_valida = _validez(ax.condicion_expr)
    estado = ax.estado_revision
    etiqueta = _ETIQUETA_ESTADO.get(estado, estado)

    axioma = (
        _fila("ID", _e(ax.axioma_id))
        + _fila("País", _e(", ".join(ax.paises)))
        + _fila("Dominio", _e(ax.dominio))
        + _fila("Descripción", _texto(ax.descripcion))
        + _fila("Condición", f"<pre>{_e(ax.condicion_expr)}</pre>")
        + _fila("Validación", validez_html)
        + _fila("Consecuencia",
                f"<b>{_e(str(ax.consecuencia.get('estado', '')))}</b> — "
                f"{_texto(str(ax.consecuencia.get('motivo', '')))}")
        + _fila("Prioridad", _e(str(ax.prioridad)))
        + _fila("Variables requeridas", _texto(", ".join(ax.variables_requeridas)))
    )
    fuente = (
        _fila("URL", _enlace_o_texto(ax.fuente_url))
        + _fila("Documento", _texto(ax.fuente_documento))
        + _fila("Artículo / Regla", _texto(ax.fuente_referencia))
        + _fila("Fragmento original",
                f"<blockquote>{_e(ax.fuente_fragmento)}</blockquote>"
                if ax.fuente_fragmento else "—")
        + _fila("Origen", _texto(ax.origen))
        + _fila("Generado por", _texto(ax.generado_por))
    )
    estado_html = _fila(
        "Estado", f'<span class="badge {_e(estado)}">{_e(etiqueta)}</span>')
    if estado != EstadoRevision.PENDIENTE.value:
        estado_html += (
            _fila("Revisado por", _texto(ax.revisado_por))
            + _fila("Fecha de revisión (UTC)", _texto(ax.revisado_en))
            + _fila("Motivo", _texto(ax.motivo_revision))
        )

    aviso = _aviso(error, "error") if error else ""
    id_url = quote(ax.axioma_id, safe="")

    if estado == EstadoRevision.PENDIENTE.value:
        deshabilitar = "" if es_valida else " disabled"
        formularios = (
            "<h3>DECISIÓN</h3>"
            f'<form method="post" action="/revision/{id_url}/aprobar">'
            "<label>Revisado por</label>"
            '<input name="revisado_por" required placeholder="tu nombre">'
            f'<button class="aprobar" type="submit"{deshabilitar} '
            "onclick=\"return confirm('¿Aprobar y activar esta regla en producción?')\">"
            "APROBAR</button></form>"
            f'<form method="post" action="/revision/{id_url}/rechazar">'
            "<label>Revisado por</label>"
            '<input name="revisado_por" required placeholder="tu nombre">'
            "<label>Motivo del rechazo</label>"
            '<textarea name="motivo" required minlength="3"></textarea>'
            '<button class="rechazar" type="submit">RECHAZAR</button></form>'
        )
    else:
        formularios = "<p>Esta propuesta ya fue revisada; no admite más cambios.</p>"

    cuerpo = (
        '<p><a href="/revision">&larr; Volver a la lista</a></p>'
        "<h2>Revisar propuesta</h2>"
        f"{aviso}"
        f"<h3>AXIOMA</h3><table>{axioma}</table>"
        f"<h3>FUENTE</h3><table>{fuente}</table>"
        f"<h3>ESTADO</h3><table>{estado_html}</table>"
        f"{formularios}"
    )
    return _pagina(f"Revisar {ax.axioma_id}", cuerpo)