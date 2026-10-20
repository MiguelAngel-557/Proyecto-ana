# Guía paso a paso — arrancar desde cero en VS Code

Esta guía asume que no tienes nada configurado todavía. Vas a arrancar con
**DuckDB** (un archivo local, sin cuentas ni servidores) y, sólo cuando lo
necesites, subes a **PostgreSQL** con una cuenta propia y gratuita.

No necesitas Neo4j para nada de esto.

---

## Parte 1 — Dejar el proyecto funcionando (10 minutos)

### Paso 1. Descomprime el proyecto

Descomprime `ana_core.zip`. Vas a tener una carpeta `ana_build/` con esto
dentro:

```
ana_build/
  ana/                  ← el código del núcleo
  axiomas/transporte/   ← axiomas de ejemplo, ya funcionando
  tests/                ← pruebas
  casos/ejemplo.json    ← un caso de prueba
  .vscode/               ← si añadiste el paquete de VS Code, va aquí
  pyproject.toml
  README.md
```

Abre esa carpeta en VS Code: **Archivo → Abrir carpeta…** → selecciona
`ana_build`.

### Paso 2. Crea el entorno virtual

Abre una terminal en VS Code (**Terminal → Nueva terminal**, o `` Ctrl+ñ `` /
`` Ctrl+` ``) y ejecuta:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Vas a ver `(.venv)` al inicio de la línea de la terminal. Eso confirma que
está activado. Si usas `uv` en vez de `venv`, salta este paso: `uv sync` ya
crea el entorno solo.

### Paso 3. Dile a VS Code cuál Python usar

`Ctrl+Shift+P` → escribe **"Python: Select Interpreter"** → elige el que
diga `.venv` (o la ruta que termine en `ana_build\.venv\Scripts\python.exe`
en Windows).

Esto es importante: si no lo haces, VS Code puede seguir usando tu Python
global y no vas a ver los paquetes que instales en el siguiente paso.

### Paso 4. Instala las dependencias

Con el entorno activado:

```bash
pip install pyyaml duckdb fastapi uvicorn pydantic
```

(Si prefieres `uv`: `uv add pyyaml duckdb fastapi uvicorn pydantic`)

No instales `psycopg2-binary` ni `neo4j` todavía. Los añades sólo si llegas
a la Parte 3.

### Paso 5. Verifica que los axiomas de ejemplo son válidos

```bash
python -m ana.cli validar
```

Deberías ver algo como:

```
  7 axiomas cargados desde axiomas/

  Cobertura por dominio:
    transporte: 7 axiomas, 4000 casos generados, 0 conflictos

  0 errores, 0 advertencias.
```

Si ves esto, el núcleo está sano. Si ves un error de `ModuleNotFoundError:
No module named 'yaml'`, es que el paso 4 no se instaló en el entorno
activo — revisa el paso 3.

### Paso 6. Corre las pruebas

```bash
python tests/test_ana.py
```

Debe terminar en `Todo correcto.` con 25 pruebas en `ok`. Esto confirma que
todo el motor de inferencia funciona en tu máquina, no sólo en la mía.

### Paso 7. Sincroniza a DuckDB

```bash
python -m ana.cli --repo duckdb sync
```

Esto crea el archivo `build/ana.duckdb` con los 7 axiomas de ejemplo
cargados. Ábrelo con el explorador de archivos de VS Code para confirmar que
existe — no hace falta que lo abras con nada especial, sólo que veas que se
creó.

### Paso 8. Evalúa un caso de prueba

```bash
python -m ana.cli --repo duckdb probar --dominio transporte --datos casos/ejemplo.json
```

Vas a ver un JSON con la traza completa de la decisión, y al final la
explicación en texto. Ese caso de ejemplo es una carga peligrosa sin
certificación HAZMAT, así que el resultado esperado es `RECHAZADO`.

**Si llegaste hasta aquí sin errores, el sistema está funcionando de punta a
punta con DuckDB.** Ya puedes empezar a cargar tus propios axiomas.

---

## Parte 2 — Trabajar con tus propios axiomas

### Escribir un axioma nuevo

Crea un archivo en `axiomas/<tu_dominio>/lo-que-sea.yaml`. Por ejemplo,
`axiomas/aduanas/documentacion.yaml`:

```yaml
defaults:
  fuente: "Política MERA — Documentación aduanera"
  version: "1.0"

axiomas:
  - axioma_id: AX-DOC-001
    descripcion: "Rechazar si falta la factura comercial."
    condicion_expr: "not documentos['factura_comercial']"
    prioridad: 90
    estado: RECHAZADO
    motivo: "Falta la factura comercial para el despacho aduanero."
```

El dominio (`aduanas`) lo toma del nombre de la carpeta. No necesitas
escribir `variables_requeridas`: se deduce solo de `condicion_expr`.

### Validar y sincronizar después de cada cambio

```bash
python -m ana.cli --repo duckdb sync
```

Si hay un error (sintaxis inválida, contradicción con otro axioma, ID
repetido), te lo va a decir antes de guardar nada en la base. Si quieres ver
qué cambiaría sin aplicarlo todavía:

```bash
python -m ana.cli --repo duckdb sync --seco
```

### Levantar la API

```bash
uvicorn ana.api:app --reload
```

Abre `http://localhost:8000/docs` en el navegador: ahí tienes una interfaz
para probar todos los endpoints (`/evaluar`, `/axiomas`, `/salud`, etc.) sin
escribir código.

Si instalaste el paquete de VS Code (`.vscode/`), en vez del comando de
arriba puedes presionar `F5` y elegir **"ANA: API (uvicorn con recarga)"**.

---

## Parte 3 — Subir a PostgreSQL (sólo cuando lo necesites)

No hace falta que hagas esto ahora. DuckDB te sirve para todo el desarrollo
y para las pruebas. Pasa a PostgreSQL cuando quieras que varias personas
escriban a la misma base a la vez, o cuando vayas a producción.

### Paso 1. Crea una cuenta propia (no compartida) en Neon

Ve a **https://neon.tech**, crea una cuenta gratuita con tu propio correo.
El plan gratuito te alcanza sin problema para el MVP de cuatro meses. Esto
evita el problema que tuviste con la cuenta compartida de Neo4j Aura: la
base es tuya, tú controlas el acceso.

### Paso 2. Copia la cadena de conexión

En el panel de Neon, copia el **Connection string**. Se ve así:

```
postgresql://usuario:contraseña@ep-xxxxx.us-east-2.aws.neon.tech/neondb
```

### Paso 3. Guárdala en tu `.env`

Crea (si no existe) un archivo `.env` en la raíz del proyecto:

```
DATABASE_URL=postgresql://usuario:contraseña@ep-xxxxx.us-east-2.aws.neon.tech/neondb
ANA_REPO=postgres
```

**Nunca subas este archivo a GitHub.** El `.gitignore` incluido ya lo
excluye.

### Paso 4. Instala el driver

```bash
pip install psycopg2-binary
```

### Paso 5. Sincroniza

```bash
python -m ana.cli sync
```

Al no pasar `--repo`, toma el valor de `ANA_REPO` de tu `.env` (`postgres`).
Esto crea las tablas automáticamente en Neon y sube tus axiomas.

### Paso 6. Verifica

```bash
python -m ana.cli validar
uvicorn ana.api:app --reload
```

Abre `http://localhost:8000/salud` y confirma que dice `"repositorio":
"postgresql"`.

---

## Resumen de comandos que vas a usar todos los días

```bash
# Validar sin tocar la base
python -m ana.cli validar

# Sincronizar (usa la BD que digas en --repo, o ANA_REPO del .env)
python -m ana.cli --repo duckdb sync

# Ver un caso de prueba
python -m ana.cli --repo duckdb probar --dominio transporte --datos casos/ejemplo.json

# Levantar la API
uvicorn ana.api:app --reload

# Correr las pruebas
python tests/test_ana.py
```

---

## Si algo falla

**`ModuleNotFoundError: No module named 'ana'`**
Estás corriendo el comando desde una carpeta distinta a `ana_build/`. Usa
`cd` para pararte en la carpeta raíz del proyecto (donde está
`pyproject.toml`) antes de correr cualquier comando.

**`ModuleNotFoundError: No module named 'yaml'` / `'duckdb'` / etc.**
El entorno virtual no está activado, o VS Code está usando otro intérprete.
Repite el Paso 3 de la Parte 1.

**`DuckDB no está instalado`**
`pip install duckdb` dentro del entorno activado.

**`PostgreSQL no instalado`**
`pip install psycopg2-binary` dentro del entorno activado.

**El comando `sync` dice "Sincronización cancelada: corrige los errores
primero"**
Corre `python -m ana.cli validar` para ver exactamente qué axioma tiene el
problema y en qué archivo está.

**Cambié un axioma pero la API sigue devolviendo lo viejo**
La API sincroniza sola al arrancar, pero si la dejaste corriendo desde antes
de tu cambio, pega un `POST` a `http://localhost:8000/axiomas/sincronizar`
(puedes hacerlo desde `/docs`) o reinícala.

---

## Qué NO necesitas hacer

- No necesitas Neo4j para nada de este flujo.
- No necesitas Docker.
- No necesitas configurar nada en la nube hasta la Parte 3, y aun ahí es
  sólo crear una cuenta gratuita.
- No necesitas tocar `ana/*.py` para cargar axiomas nuevos — sólo escribes
  archivos `.yaml` en `axiomas/`.
