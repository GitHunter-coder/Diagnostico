#!/usr/bin/env python3
"""Genera la carpeta 'sitio/' lista para publicar en GitHub Pages.

Uso:
    python construir_sitio.py --origen https://TU-USUARIO.github.io --ruta /NOMBRE-DEL-REPO/

  --origen  Origen (esquema + dominio) donde se publicará la página. Se incrusta en el agente: SOLO esa página
            podrá controlarlo. Para pruebas locales usa http://localhost:8080
  --ruta    Ruta de la página dentro del dominio (p. ej. /diagnostico/). Por defecto '/'.
"""
import argparse, hashlib, json, os, re, shutil, sys, zipfile

AQUI = os.path.dirname(os.path.abspath(__file__))
AGENTE_FUENTE = os.path.join(AQUI, "import_os_V3.py")
PLANTILLA = os.path.join(AQUI, "sitio_src", "index.template.html")
SALIDA = os.path.join(AQUI, "sitio")
FECHA_FIJA = (2026, 1, 1, 0, 0, 0)          # zips reproducibles: la misma entrada da el mismo SHA-256

LEEME = {
"windows": """DIAGNÓSTICO DE ALMACENAMIENTO - Windows
========================================
1) Descomprime esta carpeta (clic derecho > Extraer todo).
2) Elige UNA opción:
   - Iniciar_SIN_SMART.bat  : mide velocidad, capacidad y categorías de archivos. No pide permisos.
   - Iniciar_CON_SMART.bat  : además lee datos SMART de salud (temperatura, horas, desgaste). Windows pedirá permiso de Administrador.
3) La primera vez se abre una ventana con un PIN de 6 dígitos: escríbelo en la página web.
   Además se registra (solo para tu usuario, sin administrador) un acceso para que la página pueda abrir el programa con un clic la próxima vez.
   Para quitar ese acceso: Quitar_acceso_navegador.bat
4) Al terminar, cierra la ventana.

Requisito: Python 3. Si no lo tienes: winget install Python.Python.3.12  (o https://www.python.org/downloads/ marcando "Add Python to PATH").
Privacidad: el programa solo escucha en tu propio equipo (127.0.0.1) y solo obedece a la página autorizada. No envía nada a internet.
Se cierra solo a las 3 horas.
""",
"macos": """DIAGNÓSTICO DE ALMACENAMIENTO - macOS
======================================
1) Descomprime esta carpeta.
2) Abre Terminal en la carpeta y ejecuta UNA opción:
     bash Iniciar_SIN_SMART.command     (velocidad, capacidad y categorías)
     bash Iniciar_CON_SMART.command     (además el estado SMART del disco; pedirá tu contraseña)
   (Si haces doble clic y macOS lo bloquea: clic derecho > Abrir.)
3) Escribe en la página web el PIN de 6 dígitos que aparece en la terminal.

Requisito: Python 3 (macOS puede ofrecer instalar las "Command Line Tools"; acepta).
Nota: en macOS el análisis cubre el disco principal. Privacidad: solo escucha en tu equipo y no envía nada a internet.
""",
"linux": """DIAGNÓSTICO DE ALMACENAMIENTO - Linux
======================================
1) Descomprime esta carpeta y abre una terminal en ella.
2) Ejecuta UNA opción:
     bash iniciar_sin_smart.sh     (velocidad, capacidad y categorías)
     bash iniciar_con_smart.sh     (además datos SMART/temperatura; usa sudo y pedirá tu contraseña)
3) Escribe en la página web el PIN de 6 dígitos que aparece en la terminal.

Requisito: python3 (casi siempre ya instalado). Opcional para más detalle SATA: sudo apt install smartmontools
Privacidad: solo escucha en tu equipo (127.0.0.1) y no envía nada a internet.
""",
}

LANZADORES = {
"windows": {
 "Iniciar_SIN_SMART.bat": '@echo off\r\ncd /d "%~dp0"\r\nwhere py >nul 2>nul && (py -3 diagnostico_agente.py --cliente --registrar & exit /b)\r\nwhere python >nul 2>nul && (python diagnostico_agente.py --cliente --registrar & exit /b)\r\necho No se encontro Python. Instalalo con: winget install Python.Python.3.12\r\npause\r\n',
 "Iniciar_CON_SMART.bat": '@echo off\r\ncd /d "%~dp0"\r\nwhere py >nul 2>nul && (py -3 diagnostico_agente.py --cliente --smart --registrar & exit /b)\r\nwhere python >nul 2>nul && (python diagnostico_agente.py --cliente --smart --registrar & exit /b)\r\necho No se encontro Python. Instalalo con: winget install Python.Python.3.12\r\npause\r\n',
 "Quitar_acceso_navegador.bat": '@echo off\r\ncd /d "%~dp0"\r\nwhere py >nul 2>nul && (py -3 diagnostico_agente.py --desinstalar-protocolo & exit /b)\r\nwhere python >nul 2>nul && (python diagnostico_agente.py --desinstalar-protocolo & exit /b)\r\necho No se encontro Python.\r\npause\r\n',
},
"macos": {
 "Iniciar_SIN_SMART.command": '#!/bin/bash\ncd "$(dirname "$0")"\npython3 diagnostico_agente.py --cliente\n',
 "Iniciar_CON_SMART.command": '#!/bin/bash\ncd "$(dirname "$0")"\nsudo python3 diagnostico_agente.py --cliente --smart\n',
},
"linux": {
 "iniciar_sin_smart.sh": '#!/bin/bash\ncd "$(dirname "$0")"\npython3 diagnostico_agente.py --cliente\n',
 "iniciar_con_smart.sh": '#!/bin/bash\ncd "$(dirname "$0")"\nsudo python3 diagnostico_agente.py --cliente --smart\n',
},
}

LANZADORES_EXE = {
 "Iniciar_SIN_SMART.bat": '@echo off\r\ncd /d "%~dp0"\r\ndiagnostico_agente.exe --cliente --registrar\r\nif errorlevel 1 pause\r\n',
 "Iniciar_CON_SMART.bat": '@echo off\r\ncd /d "%~dp0"\r\ndiagnostico_agente.exe --cliente --smart --registrar\r\nif errorlevel 1 pause\r\n',
 "Quitar_acceso_navegador.bat": '@echo off\r\ncd /d "%~dp0"\r\ndiagnostico_agente.exe --desinstalar-protocolo\r\npause\r\n',
}

def leeme_exe(texto):
    """Con .exe ya no hace falta Python."""
    return re.sub(r"Requisito: Python 3\..*?\n", "No necesitas instalar Python. Si Windows muestra «Windows protegió su PC», elige «Más información > Ejecutar de todas formas» (el programa no tiene firma digital).\n", texto, count=1)

def hornear_agente(origen, url_sitio):
    codigo = open(AGENTE_FUENTE, encoding="utf-8").read()
    for patron, nuevo in ((r'^ORIGENES_WEB_DEFECTO = \[\].*$', f'ORIGENES_WEB_DEFECTO = [{json.dumps(origen)}]'),
                           (r'^URL_SITIO_DEFECTO = "".*$', f'URL_SITIO_DEFECTO = {json.dumps(url_sitio)}')):
        codigo, n = re.subn(patron, nuevo, codigo, count=1, flags=re.M)
        if n != 1:
            sys.exit(f"No se encontró la constante a configurar: {patron}")
    return codigo

def agregar(z, nombre, contenido, ejecutable=False):
    zi = zipfile.ZipInfo(nombre, FECHA_FIJA)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o755 if ejecutable else 0o644) << 16
    z.writestr(zi, contenido if isinstance(contenido, bytes) else contenido.encode("utf-8"))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origen", required=True, help="p. ej. https://usuario.github.io")
    ap.add_argument("--ruta", default="/", help="p. ej. /mi-repo/")
    ap.add_argument("--solo-agente", metavar="ARCHIVO", help="solo escribe el agente con el origen incrustado (para compilarlo a .exe) y termina")
    ap.add_argument("--exe", metavar="RUTA", help="diagnostico_agente.exe compilado: el paquete de Windows lo usa en lugar de Python")
    a = ap.parse_args()
    origen = a.origen.rstrip("/")
    if not re.match(r"^(https://[A-Za-z0-9.-]+|http://(localhost|127\.0\.0\.1)(:\d+)?)$", origen):
        sys.exit("--origen debe ser https://dominio (o http://localhost:PUERTO para pruebas).")
    ruta = "/" + a.ruta.strip("/") + "/" if a.ruta.strip("/") else "/"
    url_sitio = origen + ruta

    agente = hornear_agente(origen, url_sitio)
    if a.solo_agente:
        open(a.solo_agente, "w", encoding="utf-8", newline="\n").write(agente)
        print(f"Agente escrito en {a.solo_agente}")
        return
    exe = open(a.exe, "rb").read() if a.exe else None
    version = re.search(r'^VERSION_SCRIPT = "([^"]+)"', agente, re.M).group(1)
    if os.path.isdir(SALIDA):
        shutil.rmtree(SALIDA)
    os.makedirs(os.path.join(SALIDA, "descargas"))

    paquetes = {}
    for so, lanz in LANZADORES.items():
        nombre = f"diagnostico_{so}.zip"
        destino = os.path.join(SALIDA, "descargas", nombre)
        con_exe = bool(exe) and so == "windows"
        if con_exe:
            lanz = LANZADORES_EXE
        with zipfile.ZipFile(destino, "w") as z:
            if con_exe:
                agregar(z, "Diagnostico/diagnostico_agente.exe", exe, ejecutable=True)
            else:
                agregar(z, "Diagnostico/diagnostico_agente.py", agente)
            for archivo, cuerpo in lanz.items():
                agregar(z, f"Diagnostico/{archivo}", cuerpo, ejecutable=not archivo.endswith(".bat"))
            agregar(z, "Diagnostico/LEEME.txt", leeme_exe(LEEME[so]) if con_exe else LEEME[so])
        datos = open(destino, "rb").read()
        paquetes[so] = {"archivo": f"descargas/{nombre}", "tam": len(datos), "sha256": hashlib.sha256(datos).hexdigest(),
                        "lanzadores": {"sin": [k for k in lanz if k.lower().startswith("iniciar") and "sin" in k.lower()][0], "con": [k for k in lanz if k.lower().startswith("iniciar") and "con" in k.lower()][0]}}

    datos_sitio = {"version": version, "origen": origen, "paquetes": paquetes}
    html = open(PLANTILLA, encoding="utf-8").read().replace("__DATOS_SITIO__", json.dumps(datos_sitio, ensure_ascii=False))
    open(os.path.join(SALIDA, "index.html"), "w", encoding="utf-8").write(html)
    open(os.path.join(SALIDA, ".nojekyll"), "w").close()
    print(f"Sitio generado en: {SALIDA}")
    print(f"  Página autorizada en el agente: {origen}   (URL: {url_sitio})")
    for so, p in paquetes.items():
        print(f"  {p['archivo']:34} {p['tam']:>8} bytes   sha256 {p['sha256'][:16]}…")

if __name__ == "__main__":
    main()
