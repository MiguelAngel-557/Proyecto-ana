# ANA Core — carga y gobierno automático de axiomas

**¿Primera vez con este proyecto? Empieza por [`EMPEZAR_AQUI.md`](./EMPEZAR_AQUI.md)** —
guía paso a paso desde cero en VS Code, sin Neo4j, arrancando con DuckDB.

Este documento es la referencia técnica completa. La guía de arranque es la
otra.

---

Reemplaza el flujo manual (`axiomas.json` a mano → correr `database.py` →
reiniciar la API) por uno automático: escribes un axioma en YAML y el sistema
lo valida, deduce sus variables, detecta contradicciones, lo sincroniza de
forma incremental y recarga el motor sin reiniciar.

---

## Qué se automatizó

| Antes | Ahora |
|---|---|
| `variables_requeridas` escrito a mano en cada axioma | Se deduce del AST de la expresión (`ana/expr.py`) |
| Un solo `axiomas.json` plano | Árbol `axiomas/<dominio>/*.yaml` con `defaults` heredados |
| `dominio` repetido en cada axioma | Se deduce del nombre de la carpeta |
| Carga destructiva (`MATCH (n) DETACH DELETE n`) | Sincronización incremental por hash, no destructiva |
| Reiniciar el servicio tras cada cambio | El motor detecta el cambio de versión y recarga solo |
| Sin validación | 12 comprobaciones automáticas + detección de contradicciones por muestreo |
| Neo4j Aura (límite de 170K nodos, cuenta compartida) | PostgreSQL / DuckDB, cuenta propia gratuita |
| Errores de sintaxis descubiertos en producción | `python -m ana.cli validar` corta el pipeline en CI |

---

## Instalación

```bash
pip install pyyaml duckdb fastapi uvicorn pydantic
```

Para producción con PostgreSQL, añade además:

```bash
pip install psycopg2-binary
```

`asteval` ya no hace falta: el evaluador seguro está en `ana/expr.py` y
comparte la lista blanca con el validador, así que es imposible que el
validador acepte algo que el motor no sepa ejecutar. Neo4j tampoco hace
falta salvo que decidas usarlo explícitamente (ver más abajo).

## Bases de datos

Tres opciones (elige una o usa el respaldo automático):

| BD | Ideal para | Setup |
|---|---|---|
| **DuckDB** | Desarrollo / CI (empieza aquí) | `pip install duckdb`, cero cuentas |
| **PostgreSQL** | Producción | `pip install psycopg2-binary` + cuenta propia (p. ej. Neon, gratis) |
| **Archivo** | Respaldo | Cero dependencias, siempre funciona |

Auto-fallback: si no configuras nada (`ANA_REPO=auto`), intenta
PostgreSQL → DuckDB → archivo, en ese orden, y usa la primera que funcione.

```bash
# Desarrollo (recomendado para empezar)
python -m ana.cli --repo duckdb sync

# Producción, con DATABASE_URL en el .env
export ANA_REPO=postgres
python -m ana.cli sync

# Simular sin ninguna BD real
python -m ana.cli --repo archivo sync
```

Guía completa paso a paso, incluida la creación de una cuenta propia y
gratuita en Neon para PostgreSQL: [`EMPEZAR_AQUI.md`](./EMPEZAR_AQUI.md).

Neo4j ya no es la opción por defecto (el límite de 170K nodos del tier
gratuito de Aura y la fricción de compartir cuenta lo hacían poco práctico
para el MVP), pero el código sigue soportándolo si en algún momento tienes
una instancia propia: `python -m ana.cli --repo neo4j sync` con
`NEO4J_URI`, `NEO4J_USER` y `NEO4J_PASSWORD` en el `.env`.

## Puesta en marcha

```bash
# 1. Convertir el axiomas.json actual al árbol nuevo (una sola vez)
python -m ana.cli migrar

# 2. Validar
python -m ana.cli validar

# 3. Sincronizar al repositorio (Neo4j si está configurado, si no archivo)
python -m ana.cli sync

# 4. Levantar la API (sincroniza sola al arrancar)
uvicorn ana.api:app --reload
```

## Cómo se escribe un axioma ahora

`axiomas/transporte/seguridad.yaml`:

```yaml
defaults:                      # heredado por todos los axiomas del archivo
  fuente: "Política MERA — Seguridad de carga"
  version: "1.0"
  etiquetas: [seguridad, hazmat]

axiomas:
  - axioma_id: AX-HAZ-001
    descripcion: "Rechazar carga peligrosa sin certificación HAZMAT."
    condicion_expr: "mercancia['es_peligrosa'] and not transporte['hazmat']"
    prioridad: 100
    estado: RECHAZADO
    motivo: "Transporte sin certificación HAZMAT para carga peligrosa."
```

No se escribe `dominio` (lo da la carpeta), ni `variables_requeridas` (se
deducen), ni `consecuencia` anidada (`estado` + `motivo` bastan). Si omites
`axioma_id` se autogenera con el prefijo del archivo.

Campos opcionales: `vigencia_desde`, `vigencia_hasta` (el motor ignora los
axiomas fuera de vigencia y el validador avisa), `activo`, `etiquetas`,
`fuente`, `version`.

También se aceptan `.json` y `.csv` en el mismo árbol (el CSV admite columnas
`consecuencia.estado`, `consecuencia.motivo` y listas separadas por `|`),
útil si el especialista logístico prefiere capturar en Excel.

## Comandos

```bash
python -m ana.cli validar              # informe completo, no escribe nada
python -m ana.cli validar --rapido     # omite el análisis por muestreo
python -m ana.cli sync                 # valida y sincroniza
python -m ana.cli sync --seco          # simulacro: qué cambiaría
python -m ana.cli sync --repo archivo  # fuerza el respaldo local
python -m ana.cli vigilar              # resincroniza al guardar un archivo
python -m ana.cli probar --dominio transporte --datos caso.json
```

`validar` y `sync` devuelven código de salida 1 si hay errores, para usarlos
como paso obligatorio en CI o en un hook de pre-commit.

## Endpoints

| Método | Ruta | Para qué |
|---|---|---|
| POST | `/evaluar` | Inferencia con traza y explicación |
| GET | `/axiomas` | Listado, filtrable por dominio |
| POST | `/axiomas/sincronizar` | Recarga desde disco sin reiniciar |
| GET | `/axiomas/validar` | Informe de validación en vivo |
| GET | `/axiomas/conflictos` | Contradicciones detectadas |
| GET | `/auditoria` | Últimas decisiones registradas |
| GET | `/salud` | Repositorio activo y versión de la base |

---

## Validaciones automáticas

**Errores** (bloquean la sincronización)

- `E01` ID duplicado
- `E02` Expresión inválida o insegura (bloquea `__import__`, `open`, accesos a
  atributos privados, comprensiones de lista…)
- `E03` Estado de consecuencia desconocido
- `E04` Prioridad fuera del rango 1–100
- `E05` Fechas de vigencia mal formadas o invertidas
- `C01` **Contradicción**: dos axiomas que se activan a la vez con
  consecuencias incompatibles y la misma prioridad

**Advertencias**

- `A00` Axioma sin descripción (la explicación al usuario sale pobre)
- `A02` Prioridad repetida en el mismo dominio → orden no determinista
- `A03` Axioma vencido o aún no vigente
- `A04` Inalcanzable: no se activó en ningún caso generado
- `A05` Tautológico: se activa siempre

### Cómo se detectan las contradicciones

El validador lee las expresiones, infiere el tipo de cada variable (numérica
si participa en aritmética o comparaciones de orden; booleana si se usa como
valor de verdad) y los valores frontera contra los que se compara. Con eso
genera hasta 4 000 casos —el producto cartesiano completo si cabe, o una
muestra aleatoria con semilla fija si no— y ejecuta todos los axiomas del
dominio contra cada caso.

Cuando dos axiomas con la misma prioridad se activan en el mismo caso con
estados distintos, lo reporta **con el caso concreto que lo reproduce**. Ese
caso se puede pegar directamente en `/evaluar` para verlo.

Esto cubre de forma automática los KPIs de "detección de contradicciones" y
"consistencia" del documento ANA, y da además la cobertura por axioma
(cuántos casos activan cada uno), que es evidencia directa para el
experimento comparativo del capítulo 14.

---

## Cambios de comportamiento del motor

Tres correcciones respecto al motor anterior:

**1. Un axioma sin datos ya no tumba la petición completa.** Antes, el primer
axioma al que le faltaba una variable devolvía `INSUFFICIENT_DATA` y abortaba
todo, aunque fuera irrelevante para el caso. Ahora se marca `NO_EVALUABLE`,
se sigue evaluando, y sólo se devuelve `INFORMACION_INSUFICIENTE` si con lo
que hay no se llega a ninguna decisión. Los datos faltantes se reportan igual.

**2. No hay cortocircuito al primer rechazo.** Se evalúan todos los axiomas
vigentes y se resuelve por severidad (`RECHAZADO` > `REVISION_HUMANA` >
`APROBADO`) y luego por prioridad. Así la traza queda completa y los
conflictos se detectan en tiempo de ejecución, no sólo en validación.

**3. Estado `CONFLICTO` real.** Antes se devolvía `CONFLICT` cuando *ningún*
axioma se activaba, que es lo contrario de un conflicto. Ahora "ningún axioma
aplica" es `SIN_AXIOMAS` (abstención explícita) y `CONFLICTO` se reserva para
axiomas contradictorios con la misma prioridad, que es el caso que pide
intervención humana.

## Auditoría

Pasó de JSON a JSONL (`build/auditoria.jsonl`): una línea por decisión,
escritura O(1). El archivo anterior se releía y reescribía entero en cada
petición, lo que lo volvía inservible a partir de unos miles de registros.

Cada registro incluye la versión de la base de conocimiento vigente en el
momento de decidir, así que una decisión pasada se puede reproducir con
exactamente los axiomas que estaban activos entonces.

---

## Pendientes conocidos

- `evaluate_shipment` y `evaluate_route_path` (las rutas `/evaluar-envio-neo4j`
  y `/buscar-camino` del `main.py` original) no existían en `engine.py`; esos
  endpoints estaban rotos. Las consultas de grafo de `database.py` siguen
  siendo válidas: hay que decidir si el motor consulta el grafo para rellenar
  el contexto antes de evaluar, o si eso lo hace la capa de aplicación.
- La detección de contradicciones es por muestreo, no una prueba formal. Para
  el artículo científico conviene complementarla con un SMT solver (z3) sobre
  las expresiones lineales. La infraestructura ya está: `_valores_candidatos`
  extrae exactamente lo que z3 necesitaría.
- Falta el repositorio de casos (capítulo 4 del documento) para las pruebas de
  regresión: los casos generados sirven para validar consistencia, pero no
  sustituyen casos históricos etiquetados por el especialista logístico.

## Seguridad

Dos cosas del ZIP que reviso aquí porque son urgentes:

1. El `.env` con la contraseña real de Neo4j venía dentro del paquete.
   **Rota esa credencial.**
2. El `.venv` completo estaba versionado. Se incluye un `.gitignore` que
   excluye ambos, junto con `build/` y `__pycache__/`.
